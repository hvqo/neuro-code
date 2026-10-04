"""Paired fixed-work and real TUI replay; synthetic timing is not Konsole latency."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

from rich.console import Console
from rich.syntax import Syntax
from tests.visual.code_flicker.replay import baseline_rendering
from tests.visual.showcases import make_app
from tests.visual.text_arrival.frame_pacing import Profile, deliver

from neuro_code.interfaces.tui.fence_cache import FenceRenderCache
from neuro_code.interfaces.tui.fence_presentation import FencePresentation
from neuro_code.interfaces.tui.widgets import AssistantMarkdown
from neuro_code.shared.ui_theme import UiTheme


def recordings():
    bodies = {
        "python": "".join(f"value_{i}: int = {i} * 2  # stage\n" for i in range(540)),
        "typescript": "".join(f"export const value_{i}: number = {i};\n" for i in range(540)),
        "rust": "".join(f"pub fn value_{i}() -> i32 {{ {i} }}\n" for i in range(540)),
        "json": "{\n" + ",\n".join(f'  "value_{i}": {i}' for i in range(540)) + "\n}\n",
        "shell": "".join(f"printf '%s\\n' 'line-{i}'\n" for i in range(540)),
        "diff": "".join(f"@@ -{i} +{i} @@\n-old_{i}\n+new_{i}\n" for i in range(180)),
    }
    cases = {
        language: f"```{language}\n{body}```\n\nAfter code." for language, body in bodies.items()
    }
    cases["multiple"] = cases["python"] + "\n\n" + cases["typescript"]
    cases["unclosed"] = "```python\n" + bodies["python"]
    return {
        name: [
            {"at_ms": i * 25, "text": source[j : j + 180]}
            for i, j in enumerate(range(0, len(source), 180))
        ]
        for name, source in cases.items()
    }


def fixed_work(items, mode):
    console = Console(width=100, color_system="truecolor")
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    counts, cpu = (
        {"highlight": 0, "parse": 0, "render": 0},
        {"highlight": 0.0, "parse": 0.0, "render": 0.0},
    )
    original = Syntax.highlight

    def highlight(syntax, *args, **kwargs):
        begin = process_time()
        result = original(syntax, *args, **kwargs)
        counts["highlight"] += 1
        cpu["highlight"] += process_time() - begin
        return result

    source = ""
    trace = []
    begin_total = process_time()
    with (
        baseline_rendering() if mode == "baseline" else nullcontext(),
        patch.object(Syntax, "highlight", highlight),
    ):
        for item in [*items, {"text": "", "complete": True}]:
            source += item["text"]
            begin = process_time()
            md = AssistantMarkdown(source)
            md.bind_fence_presentation(lifecycle, complete=item.get("complete", False))
            md.fence_cache = cache
            cpu["parse"] += process_time() - begin
            counts["parse"] += 1
            states = [
                {
                    "generation": record.identity.generation,
                    "source_start": record.identity.source_start,
                    "phase": record.phase.value,
                    "foreground": "SYNTAX"
                    if mode == "baseline"
                    else ("PLAIN" if token_id in md._active_fences else "SYNTAX"),
                    "finalizations": record.finalizations,
                }
                for token_id, record in md._fence_records.items()
            ]
            begin = process_time()
            console.render_lines(md, console.options, pad=False)
            trace.append(
                {
                    "revision": counts["parse"],
                    "characters": len(source),
                    "response_complete": item.get("complete", False),
                    "fences": states,
                }
            )
            cpu["render"] += process_time() - begin
            counts["render"] += 1
    return {
        "counts": counts,
        "cpu_s": cpu,
        "total_cpu_s": process_time() - begin_total,
        "trace": trace,
    }


async def tui(items, mode):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=False)
    profile = Profile()
    profile.app = app
    lexical = {"calls": 0, "cpu_s": 0.0}
    original = Syntax.highlight

    def highlight(syntax, *args, **kwargs):
        begin = process_time()
        result = original(syntax, *args, **kwargs)
        lexical["calls"] += 1
        lexical["cpu_s"] += process_time() - begin
        return result

    with (
        baseline_rendering() if mode == "baseline" else nullcontext(),
        profile.hooks(),
        patch.object(Syntax, "highlight", highlight),
    ):
        async with app.run_test(size=(100, 32)) as pilot:
            app._write_entry("user", "Deterministic fenced code replay")
            await pilot.pause()
            profile.active = True
            profile.start = perf_counter()
            begin = process_time()
            source = await deliver(app, profile, items)
            app._finish_pending_assistant(source)
            await asyncio.sleep(0.2)
            profile.active = False
            result = profile.summary(
                process_time() - begin, perf_counter() - profile.start, 25, "code-fence", mode
            )
            return {
                "pygments": lexical,
                **{
                    key: result[key]
                    for key in [
                        "cpu_s",
                        "counts",
                        "cpu_exclusive_s",
                        "commit_interval_ms",
                        "method_ms",
                    ]
                },
            }


async def run(args):
    result = []
    for name, items in recordings().items():
        row = {"case": name}
        for mode in ["baseline", "stable"]:
            row[mode] = {"fixed_work": fixed_work(items, mode)}
            if not args.fixed_only:
                row[mode]["tui"] = await tui(items, mode)
        result.append(row)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/neuro-code-flicker-benchmark.json")
    )
    parser.add_argument("--fixed-only", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
