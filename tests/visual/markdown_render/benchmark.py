"""Reproducible full-parser baseline/production fence-cache benchmark.

Uses the existing presentation profiler; all instrumentation is scoped to tests.
No timing parameter or runtime optimization is selected by this replay.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

from rich.console import Console
from rich.markdown import CodeBlock
from rich.syntax import Syntax
from tests.visual.markdown_render.fixtures import FIXTURES, recording
from tests.visual.text_arrival.frame_pacing import Profile, measure

from neuro_code.interfaces.tui import widgets
from neuro_code.interfaces.tui.fence_cache import FenceRenderCache


@contextmanager
def instrument(profile, mode, counts):
    with ExitStack() as stack:
        if mode == "baseline":
            # Same original Rich fence rendering as main; full parse/view cache unchanged.
            stack.enter_context(
                patch.object(
                    widgets._FencedCodeBlock, "__rich_console__", CodeBlock.__rich_console__
                )
            )
            stack.enter_context(patch.object(widgets, "closed_fences", lambda *_: frozenset()))
        original_highlight = Syntax.highlight
        original_get = FenceRenderCache.get
        original_put = FenceRenderCache.put

        def highlight(*a, **kw):
            with profile().span("syntax_highlight"):
                return original_highlight(*a, **kw)

        def get(cache, key):
            result = original_get(cache, key)
            counts["hit" if result is not None else "miss"] += 1
            return result

        def put(cache, key, segments):
            original_put(cache, key, segments)
            counts["retained_entries_max"] = max(counts["retained_entries_max"], len(cache))
            counts["estimated_bytes_max"] = max(
                counts["estimated_bytes_max"], cache.estimated_bytes
            )

        stack.enter_context(patch.object(Syntax, "highlight", highlight))
        stack.enter_context(patch.object(FenceRenderCache, "get", get))
        stack.enter_context(patch.object(FenceRenderCache, "put", put))
        yield


async def run(args):
    results = []
    for repeat in range(args.repetitions):
        for case in args.cases or FIXTURES:
            for mode in ["baseline", "production"]:
                counts = Counter()
                if args.fixed_work:
                    profile = Profile()
                    # A real message owns the cache across immutable renderable revisions.
                    message = widgets.AssistantMessage(widgets.AssistantMarkdown(""))
                    text = ""
                    with profile.hooks(), instrument(lambda profile=profile: profile, mode, counts):
                        profile.start = perf_counter()
                        profile.active = True
                        start = process_time()
                        for item in recording(case):
                            text += item["text"]
                            md = widgets.AssistantMarkdown(text)
                            md.fence_cache = message.fence_cache
                            md.cache_stream_view(animate=True)
                            console = Console(width=114, color_system="truecolor")
                            console.render_lines(md, console.options, pad=False)
                        cpu = process_time() - start
                        elapsed = perf_counter() - profile.start
                        profile.active = False
                    result = profile.summary(cpu, elapsed, 25, case, mode)
                    result["fixed_revisions"] = len(recording(case))
                else:
                    allocated = []
                    original_init = Profile.__init__

                    def initialized(instance, original_init=original_init, allocated=allocated):
                        original_init(instance)
                        allocated.append(instance)

                    with (
                        patch.object(Profile, "__init__", initialized),
                        instrument(lambda allocated=allocated: allocated[0], mode, counts),
                    ):
                        result = await measure(case, recording(case), animated=True)
                    result.pop("trace")
                result.update(
                    mode=mode,
                    repeat=repeat + 1,
                    cache_counts=dict(counts),
                    input_origin="synthetic-fence-regression",
                )
                results.append(result)
                args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
                print(
                    case,
                    mode,
                    repeat + 1,
                    result["cpu_s"],
                    result["commit_interval_ms"],
                    flush=True,
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=list(FIXTURES))
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--fixed-work", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
