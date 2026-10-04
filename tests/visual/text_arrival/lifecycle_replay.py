# ruff: noqa: RUF001
"""Opt-in production lifecycle replay; Sentinel never enters src or preferences.

Synthetic intact deltas, actual NeuroCodeApp/controller/view path. No network,
new variants or artificial character release. Hooks exist only in this process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from contextlib import ExitStack, suppress
from dataclasses import replace
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

from rich.style import Style
from tests.visual.showcases import _VisualFixtureRunner
from tests.visual.text_arrival.frame_pacing import Profile, load_recording

from neuro_code.interfaces.tui import text_arrival, widgets
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.terminal_palette import probe_terminal_palette
from neuro_code.interfaces.tui.widgets import AssistantMessage, PromptInput
from neuro_code.shared.ui_theme import UiTheme


def cold_tape():
    code = "".join(
        f"def function_{n}(value):\n    # comment\n    return value + {n}\n\n" for n in range(135)
    )
    return [
        {
            "at_ms": 0,
            "text": "```python\n"
            + code
            + "```\n\n"
            + "冷渲染后的新文字应该从首次呈现开始落墨。" * 4,
        },
        {"at_ms": 1000, "text": "\n\n继续输出中文正文，并保持既有文字的生命周期。"},
        {"at_ms": 1500, "text": "\n\nEnglish and 中文 should retain source identity."},
    ]


class LifecycleReplay(NeuroCodeApp):
    def __init__(self, args):
        super().__init__(
            _VisualFixtureRunner(),
            ui_theme=UiTheme(args.theme),
            provider_name="synthetic-lifecycle-replay",
            model_name="25ms view / 20fps ink",
            cwd=Path.cwd(),
            terminal_palette=None if args.headless else probe_terminal_palette(),
        )
        self.args = args
        self.profile = Profile()
        self.running_replay = False
        self._agent_preferences = replace(
            self._agent_preferences, text_arrival_animation=not args.off
        )

    def on_mount(self):
        super().on_mount()
        if self.args.auto_exit and not self.args.headless:
            self.call_after_refresh(self.start_replay)

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted):
        event.stop()
        event.prevent_default()
        event.input.clear()
        self.start_replay()

    def start_replay(self):
        if not self.running_replay:
            self.running_replay = True
            self.run_worker(self.replay(), exclusive=True)

    async def replay(self):
        args = self.args
        self._replace_transcript([])
        self._write_entry(
            "user",
            "Presentation lifecycle diagnostic · "
            + ("OFF" if args.off else "Sentinel" if args.sentinel else "Production"),
        )
        await asyncio.sleep(0.15)
        profile = self.profile
        profile.__init__()
        profile.app = self
        profile.start = perf_counter()
        profile.active = True
        started_cpu = process_time()
        frames, receives, lateness = [], [], []
        styled = Counter()
        parse_in_tick = [0]
        local_refreshes = Counter()
        tick_active = [False]

        async def monitor():
            target = perf_counter() + 0.02
            while True:
                await asyncio.sleep(max(0, target - perf_counter()))
                at = perf_counter()
                lateness.append(max(0, at - target) * 1000)
                target = at + 0.02

        monitor_task = asyncio.create_task(monitor())
        render = AssistantMessage.render_lines
        tick = AssistantMessage._arrival_tick
        refresh = AssistantMessage.refresh

        def painted(message, crop):
            strips = render(message, crop)
            samples = []
            for row, strip in enumerate(strips, crop.y):
                for segment in strip:
                    style = segment.style
                    if not style or text_arrival.ARRIVAL_META not in style.meta:
                        continue
                    source = tuple(style.meta[text_arrival.ARRIVAL_META])
                    age = message._arrival.age(source, widgets.monotonic())
                    if age is not None:
                        styled[source] += 1
                        samples.append(
                            {
                                "source": source,
                                "age_ms": round(age * 1000, 3),
                                "row": row,
                                "foreground": str(style.color),
                                "reverse": style.reverse,
                                "bold": style.bold,
                            }
                        )
            if samples:
                frames.append(
                    {
                        "ms": round((perf_counter() - profile.start) * 1000, 3),
                        "cached_sources": len(message.renderable.cached_sources),
                        "dirty_rows": sorted(message._arrival_rows),
                        "samples": samples[-4:],
                    }
                )
            return strips

        def ticked(message):
            before = profile.counts["markdown_parse"]
            tick_active[0] = True
            try:
                return tick(message)
            finally:
                tick_active[0] = False
                parse_in_tick[0] += profile.counts["markdown_parse"] - before

        def refreshed(message, *regions, **kwargs):
            local_refreshes["body_refresh"] += 1
            if tick_active[0]:
                local_refreshes["dirty_rows"] += sum(region.height for region in regions)
                local_refreshes["layout"] += bool(kwargs.get("layout"))
            return refresh(message, *regions, **kwargs)

        try:
            with ExitStack() as hooks:
                hooks.enter_context(profile.hooks())
                hooks.enter_context(patch.object(AssistantMessage, "render_lines", painted))
                # Capture the profiled tick, not its pre-profile version.
                tick = AssistantMessage._arrival_tick
                hooks.enter_context(patch.object(AssistantMessage, "_arrival_tick", ticked))
                hooks.enter_context(patch.object(AssistantMessage, "refresh", refreshed))
                if args.headless:
                    hooks.enter_context(
                        patch.object(AssistantMessage, "_motion_allowed", lambda self: True)
                    )
                if args.sentinel:
                    hooks.enter_context(patch.object(text_arrival, "DURATION_SECONDS", 0.5))
                    hooks.enter_context(
                        patch.object(
                            widgets,
                            "ink_style",
                            lambda style, age, rank, *colors: (
                                style + Style(reverse=True, bold=True) if 0 <= age < 0.5 else style
                            ),
                        )
                    )
                source = ""
                recording = load_recording(args.recording) if args.recording else cold_tape()
                for revision, item in enumerate(recording, 1):
                    await asyncio.sleep(
                        max(0, profile.start + item["at_ms"] / 1000 / args.speed - perf_counter())
                    )
                    profile.revision = revision
                    profile.event(
                        "delta_received", revision=revision, scheduled_ms=item["at_ms"] / args.speed
                    )
                    receives.append(
                        {
                            "ms": round((perf_counter() - profile.start) * 1000, 3),
                            "range": [len(source), len(source) + len(item["text"])],
                        }
                    )
                    source += item["text"]
                    self._update_pending_assistant(source)
                    assert self._pending_assistant.content == source
                await asyncio.sleep(0.9)
                message = self._pending_assistant
                assert message.renderable.markup == source
                before = Counter(profile.counts)
                body_refresh_before = local_refreshes["body_refresh"]
                await asyncio.sleep(0.3)
                idle = {
                    key: profile.counts[key] - before[key]
                    for key in ["animation_tick", "markdown_parse", "refresh_request"]
                }
                idle["body_refresh"] = local_refreshes["body_refresh"] - body_refresh_before
                # App pulse/status may refresh; isolate the text timer and parse.
                assert message._arrival_timer is message._stream_view_timer is None
                assert idle["animation_tick"] == idle["markdown_parse"] == 0
                assert idle["body_refresh"] == 0
                result = profile.summary(
                    process_time() - started_cpu,
                    perf_counter() - profile.start,
                    25,
                    "cold-code-prose",
                    "off" if args.off else "sentinel" if args.sentinel else "production",
                )
                from tests.visual.text_arrival.frame_pacing import distribution

                result.update(
                    {
                        "input_origin": "external-recording"
                        if args.recording
                        else "synthetic-540-line-code-plus-prose",
                        "color_system": self.console.color_system,
                        "terminal_size": list(self.size),
                        "receives": receives,
                        "frames": frames,
                        "styled_source_count": len(styled),
                        "styled_frame_count": distribution(list(styled.values())),
                        "animation_parse_count": parse_in_tick[0],
                        "animation_refresh": dict(local_refreshes),
                        "loop_lateness_ms": distribution(lateness),
                        "idle_after_settle": idle,
                    }
                )
                self._finish_pending_assistant(source)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        finally:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
            self.running_replay = False
        if args.auto_exit:
            self.exit()


async def headless(args):
    app = LifecycleReplay(args)
    async with app.run_test(size=(args.width, args.height)):
        await app.replay()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--sentinel", action="store_true")
    result.add_argument("--off", action="store_true")
    result.add_argument("--theme", choices=["graphite", "porcelain", "system"], default="graphite")
    result.add_argument("--speed", type=float, default=1)
    result.add_argument("--recording", type=Path)
    result.add_argument("--output", type=Path, default=Path("/tmp/neuro-lifecycle-replay.json"))
    result.add_argument("--headless", action="store_true")
    result.add_argument("--auto-exit", action="store_true")
    result.add_argument("--width", type=int, default=162)
    result.add_argument("--height", type=int, default=86)
    return result


if __name__ == "__main__":
    args = parser().parse_args()
    if args.speed <= 0:
        raise SystemExit("speed must be positive")
    if args.headless:
        asyncio.run(headless(args))
    else:
        # Probe the existing writer; do not create a second worker/clock.
        from tests.visual.text_arrival.native_pacing import MeasuredOutput
        from textual.drivers._writer_thread import WriterThread

        app = LifecycleReplay(args)
        constructor = WriterThread.__init__

        def writer_init(writer, file):
            constructor(writer, MeasuredOutput(file, app.profile))

        with patch.object(WriterThread, "__init__", writer_init):
            app.run()
