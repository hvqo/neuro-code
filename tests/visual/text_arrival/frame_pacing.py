# ruff: noqa: RUF001
"""Production-cadence replay benchmark; no provider, guessed terminal or typewriter queue.

Synthetic stress tapes are explicitly labelled. --recording accepts original
JSONL arrival times (at_ms / text), preserving each delta and timing intact.
Headless results serialize the real compositor output but do not measure terminal
emulator scanout. native_pacing runs the same replay in the native Textual driver.
All hooks are scoped to this explicit test tool; production has no profiling hooks.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

from rich.syntax import Syntax
from tests.visual.showcases import make_app
from textual._compositor import CompositorUpdate
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widget import Widget

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.interfaces.tui import widgets
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, AssistantMessage, TranscriptScroll
from neuro_code.shared.ui_theme import UiTheme

CJK = "当前需要检查模型输出、Markdown 解析、中文换行和终端刷新之间的关系。已经收到的正文必须立即成为完整的 canonical text。"
PYTHON = "".join(
    f'def inspect_{i}(value: int) -> str:\n    # 验证输出语义\n    return f"value={{value + {i}}}"\n\n'
    for i in range(90)
)
CASES = {
    "long-cjk": "\n\n".join(CJK * 4 for _ in range(18)),
    "mixed": "\n\n".join(
        (CJK + " Frame pacing should follow actual delta arrival without artificial delay. ") * 3
        for _ in range(14)
    ),
    "headings-lists": "".join(
        f"## 第 {i} 组证据\n\n{CJK}\n\n- evidence one\n- 第二项证据\n  - dependent detail\n\n"
        for i in range(24)
    ),
    "inline-code": "\n\n".join(
        (CJK + "检查 `AGENTS.md`、`src/app.py` 和 `uv run pytest`。") * 3 for _ in range(18)
    ),
    "large-code": "```python\n" + PYTHON + "```\n",
    "markdown-code": "# 分析结果\n\n"
    + CJK * 8
    + "\n\n```python\n"
    + PYTHON[: len(PYTHON) // 2]
    + "```\n\n"
    + CJK * 8,
    "diff": "```diff\n"
    + "".join(
        f"@@ -{i},2 +{i},2 @@\n-old_value_{i} = 1\n+new_value_{i} = 42\n context\n"
        for i in range(80)
    )
    + "```\n",
    "tool-output": "",
    "long-scroll": "\n\n".join(f"段落 {i}：" + CJK * 3 for i in range(36)),
}


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int((len(v) - 1) * p))], 3)


def distribution(values):
    return {
        name: percentile(values, p)
        for name, p in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1)]
    }


def tape(case, count=150):
    if case == "tool-output":
        return [{"at_ms": i * 60, "tool": i} for i in range(35)]
    source = CASES[case]
    # Existing long response, followed by a high-rate growing tail. Not a claim
    # about a real provider's rate. Both prefill and tail are intact deltas.
    split = len(source) // 2
    result = [{"at_ms": 0, "text": source[:split]}]
    tail = source[split:]
    t = 25
    for i in range(count):
        if i and i % 45 == 0:
            t += 80  # Deliberate source pause; do not call this a UI long frame.
        result.append(
            {"at_ms": t, "text": tail[len(tail) * i // count : len(tail) * (i + 1) // count]}
        )
        t += 10
    return result


class Profile:
    def __init__(self):
        self.active = False
        self.start = 0.0
        self.trace = []
        self.counts = Counter()
        self.cpu = Counter()
        self.durations = defaultdict(list)
        self.stack = []
        self.app = None
        self.revision = 0
        self.committed = 0
        self.painted = 0
        self.layout_requests = Counter()
        self.refresh_widgets = Counter()
        self.gc_started = None

    def event(self, phase, **data):
        if self.active:
            self.trace.append(
                {"ms": round((perf_counter() - self.start) * 1000, 4), "phase": phase, **data}
            )

    @contextmanager
    def span(self, label):
        if not self.active:
            yield
            return
        started, cpu = perf_counter(), process_time()
        frame = [0.0]
        self.stack.append(frame)
        try:
            yield
        finally:
            duration, elapsed_cpu = (perf_counter() - started) * 1000, process_time() - cpu
            self.stack.pop()
            self.counts[label] += 1
            self.cpu[label] += max(0, elapsed_cpu - frame[0])
            self.durations[label].append(duration)
            if self.stack:
                self.stack[-1][0] += elapsed_cpu
            self.event(label, duration_ms=round(duration, 4))

    def wrap(self, original, label):
        def wrapped(*args, **kwargs):
            with self.span(label):
                return original(*args, **kwargs)

        return wrapped

    def generator(self, original, label):
        def wrapped(*args, **kwargs):
            with self.span(label):
                yield from original(*args, **kwargs)

        return wrapped

    def hooks(self):
        stack = ExitStack()

        def gc_event(phase, info):
            if not self.active:
                return
            if phase == "start":
                self.gc_started = perf_counter()
            elif self.gc_started is not None:
                duration = (perf_counter() - self.gc_started) * 1000
                self.counts["gc_pause"] += 1
                self.durations["gc_pause"].append(duration)
                self.event(
                    "gc_pause", duration_ms=round(duration, 4), generation=info["generation"]
                )
                self.gc_started = None

        gc.callbacks.append(gc_event)
        stack.callback(gc.callbacks.remove, gc_event)
        for cls, method, label in [
            (AssistantMarkdown, "__init__", "markdown_parse"),
            (Screen, "_compositor_refresh", "compositor"),
            (AssistantMessage, "_arrival_tick", "animation_tick"),
            (VerticalScroll, "scroll_end", "scroll_api"),
            (TranscriptScroll, "_finish_stream_follow", "scroll_follow"),
            (NeuroCodeApp, "_refresh_turn_activity", "status_update"),
        ]:
            original = getattr(cls, method)
            stack.enter_context(patch.object(cls, method, self.wrap(original, label)))
        refresh = Widget.refresh

        def refreshed(widget, *regions, **kwargs):
            if self.active:
                name = f"#{widget.id}" if widget.id else type(widget).__name__
                self.refresh_widgets[name] += 1
                if kwargs.get("layout"):
                    self.layout_requests[name] += 1
            with self.span("refresh_request"):
                return refresh(widget, *regions, **kwargs)

        stack.enter_context(patch.object(Widget, "refresh", refreshed))
        for cls, method, label in [
            (widgets._MarkdownBody, "__rich_console__", "markdown_render"),
            (Syntax, "__rich_console__", "syntax_render"),
        ]:
            stack.enter_context(
                patch.object(cls, method, self.generator(getattr(cls, method), label))
            )
        layout = Screen._refresh_layout

        def reflow(screen, size=None, scroll=False):
            with self.span("scroll_layout" if scroll else "layout"):
                return layout(screen, size=size, scroll=scroll)

        stack.enter_context(patch.object(Screen, "_refresh_layout", reflow))
        commit = AssistantMessage._commit_stream_view

        def committed(view, now):
            with self.span("view_commit"):
                commit(view, now)
            if self.active:
                self.committed = self.revision
                self.event("commit", revision=self.committed, chars=len(view.content))

        stack.enter_context(patch.object(AssistantMessage, "_commit_stream_view", committed))
        setattr_original = AssistantMessage.__setattr__

        def assigned(view, name, value):
            setattr_original(view, name, value)
            if name == "content" and self.active and hasattr(view, "_stream_commit_at"):
                self.event("canonical", revision=self.revision, chars=len(value))

        stack.enter_context(patch.object(AssistantMessage, "__setattr__", assigned))
        display = NeuroCodeApp._display

        def painted(app, screen, renderable):
            if self.active and isinstance(renderable, CompositorUpdate):
                if app.is_headless:
                    with self.span("terminal_serialize"):
                        payload = renderable.render_segments(app.console)
                    self.counts["terminal_bytes"] += len(payload.encode())
                self.counts["terminal_updates"] += 1
                self.counts[type(renderable).__name__] += 1
                transcript = app.query_one("#transcript", TranscriptScroll)
                if self.committed != self.painted and transcript.is_vertical_scroll_end:
                    self.painted = self.committed
                    self.event(
                        "visible_commit", revision=self.painted, update=type(renderable).__name__
                    )
            display(app, screen, renderable)

        stack.enter_context(patch.object(NeuroCodeApp, "_display", painted))
        stack.enter_context(patch.object(AssistantMessage, "_motion_allowed", lambda view: True))
        return stack

    def summary(self, cpu_s, elapsed, cadence, case, mode):
        def intervals(phase):
            t = [e["ms"] for e in self.trace if e["phase"] == phase]
            return [b - a for a, b in pairwise(t)]

        received = {e["revision"]: e["ms"] for e in self.trace if e["phase"] == "delta_received"}
        ages = []
        for e in self.trace:
            if e["phase"] == "visible_commit" and e["revision"] in received:
                ages.append(e["ms"] - received[e["revision"]])
        # Pending age includes the oldest coalesced delta, not just newest.
        oldest = []
        last = 0
        for e in self.trace:
            if e["phase"] == "visible_commit":
                arrivals = [t for n, t in received.items() if last < n <= e["revision"]]
                if arrivals:
                    oldest.append(e["ms"] - min(arrivals))
                last = e["revision"]
        return {
            "case": case,
            "mode": mode,
            "cadence_ms": cadence,
            "wall_s": round(elapsed, 4),
            "cpu_s": round(cpu_s, 4),
            "counts": dict(self.counts),
            "layout_requests_by_widget": dict(self.layout_requests),
            "refresh_requests_by_widget": dict(self.refresh_widgets),
            "canonical_latency_ms": distribution(
                [
                    e["ms"] - received[e["revision"]]
                    for e in self.trace
                    if e["phase"] == "canonical" and e["revision"] in received
                ]
            ),
            "arrival_schedule_lateness_ms": distribution(
                [
                    max(0, e["ms"] - e["scheduled_ms"])
                    for e in self.trace
                    if e["phase"] == "delta_received"
                ]
            ),
            "cpu_exclusive_s": {k: round(v, 5) for k, v in self.cpu.items()},
            "method_ms": {k: distribution(v) for k, v in self.durations.items()},
            "commit_interval_ms": distribution(intervals("commit")),
            "visible_interval_ms": distribution(intervals("visible_commit")),
            "latest_delta_to_visible_ms": distribution(ages),
            "oldest_pending_to_visible_ms": distribution(oldest),
        }


async def deliver(app, profile, items):
    start = perf_counter()
    text = ""
    seq = 0
    for entry in items:
        await asyncio.sleep(max(0, start + entry["at_ms"] / 1000 - perf_counter()))
        if "text" in entry:
            seq += 1
            profile.revision = seq
            profile.event("delta_received", revision=seq, scheduled_ms=entry["at_ms"])
            text += entry["text"]
            await app._handle_event(
                AgentEvent.create(seq, AgentEventKind.TEXT_DELTA, {"text": entry["text"]})
            )
            assert app._pending_assistant.content == text
        else:
            i = entry["tool"]
            for kind, data in [
                (
                    AgentEventKind.TOOL_REQUESTED,
                    {"id": f"t{i}", "name": "read_file", "arguments": {"path": f"src/file_{i}.py"}},
                ),
                (AgentEventKind.TOOL_STARTED, {"id": f"t{i}", "name": "read_file"}),
                (
                    AgentEventKind.TOOL_COMPLETED,
                    {
                        "id": f"t{i}",
                        "name": "read_file",
                        "content": CJK * 4,
                        "metadata": {"total_lines": 90},
                    },
                ),
            ]:
                seq += 1
                await app._handle_event(AgentEvent.create(seq, kind, data))
    return text


async def measure(case, recording=None, animated=True):
    cadence = widgets.VIEW_COMMIT_SECONDS * 1000
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=animated)
    profile = Profile()
    profile.app = app
    with profile.hooks():
        async with app.run_test(size=(100, 32)) as pilot:
            app._write_entry("user", "Frame pacing repository review / 流式回放")
            await pilot.pause()
            profile.active = True
            profile.start = perf_counter()
            cpu = process_time()
            text = await deliver(app, profile, recording if recording is not None else tape(case))
            await asyncio.sleep(0.3)
            if app._pending_assistant is not None:
                assert app._pending_assistant.renderable.markup == text
                assert app._pending_assistant._arrival_timer is None
            elapsed = perf_counter() - profile.start
            cpu = process_time() - cpu
            profile.active = False
            result = profile.summary(cpu, elapsed, cadence, case, "production")
            transcript = app.query_one("#transcript", TranscriptScroll)
            result["followed_tail"] = transcript.is_vertical_scroll_end
            result["scroll_y"] = transcript.scroll_y
            result["max_scroll_y"] = transcript.max_scroll_y
            assert result["followed_tail"], (
                case,
                cadence,
                transcript.scroll_y,
                transcript.max_scroll_y,
            )
            result["trace"] = profile.trace
            result["animation"] = animated
            result["input_origin"] = "external-recording" if recording else "synthetic-stress-tape"
            return result


async def matrix(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    recording = load_recording(args.recording) if args.recording else None
    cases = args.cases or list(CASES)
    for repeat in range(args.repetitions):
        for case in cases:
            result = await measure(case, recording, not args.off)
            result["repeat"] = repeat + 1
            results.append(result)
            args.output.write_text(json.dumps(results, ensure_ascii=False) + "\n")
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in {"trace", "method_ms", "cpu_exclusive_s"}
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


def load_recording(path):
    items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not items or any(
        not isinstance(i.get("at_ms"), (float, int))
        or i["at_ms"] < 0
        or not isinstance(i.get("text"), str)
        for i in items
    ):
        raise ValueError("recording requires nonnegative at_ms and intact text for each delta")
    if any(b["at_ms"] < a["at_ms"] for a, b in pairwise(items)):
        raise ValueError("arrival times must be monotonic")
    return items


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=list(CASES))
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--off", action="store_true")
    args = parser.parse_args()
    asyncio.run(matrix(args))


if __name__ == "__main__":
    main()
