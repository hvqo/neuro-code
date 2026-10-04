# ruff: noqa: RUF001
"""Real Neuro Code terminal replay; Enter starts, Ctrl+C exits after inspection.

Both modes receive the same intact synthetic deltas at the same source timing.
Use --speed slow for visual inspection and --speed normal for normal throughput.
No provider credentials, real model, or terminal configuration are accessed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.visual.showcases import _VisualFixtureRunner

from neuro_code.interfaces.tui import widgets
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, AssistantMessage, PromptInput
from neuro_code.shared.ui_theme import UiTheme

CASES = {
    "python": [
        'value = """first\n',
        "second",
        '\nthird"""\n',
        "# comment\n",
        'message = f"value: {',
        'value}"\n',
    ],
    "rust": ["/* comment\n", "continues", " */\n", 'let text = r#"first\n', 'second"#;\n'],
    "typescript": ["/* comment\n", "continues", " */\n", "const text = `first\n", "${value}`;\n"],
    "json": ['{\n  "name": "', "stream", '",\n  "value": 42\n}\n'],
    "shell": ["# comment\n", 'text="first\n', "second", '"\nprintf "%s" "$text"\n'],
    "diff": ["@@ -1 +1 @@\n", "-old", "\n+new", "\n context\n"],
}


def stress_deltas(language: str) -> list[str]:
    """Include an EOF closing candidate invalidated by its next intact delta."""
    deltas = [f"```{language}\n", *CASES[language], "```", "not-a-close", "\n", "```", "\n"]
    deltas.extend(["\nResult: ", "后续正文继续。", "\n"])
    return deltas


def python_replay(*, lines: int = 200) -> list[str]:
    if not 100 <= lines <= 1000:
        raise ValueError("lines must be between 100 and 1000")
    code = [
        '"""Stable plain streaming; final token colors arrive once.\n',
        "中文 + English\n",
        '"""\n',
    ]
    code.extend(
        f"value_{index:03d}: int = {index} * 2  # stable foreground\n" for index in range(lines - 3)
    )
    return [
        "```python\n",
        *code,
        "```",
        "\n",
        "\n## 结果\n\n",
        "代码完成后，",
        "正文仍在输出，",
        "代码配色保持稳定。\n",
    ]


@contextmanager
def baseline_rendering():
    """Test-only main behavior: full lexical color on every active revision."""
    create = widgets._FencedCodeBlock.create
    fence_key = widgets.fence_render_key

    def main_create(markdown, token):
        element = create(markdown, token)
        element._defer_syntax = False
        element.fence_cache = (
            markdown.fence_cache
            if isinstance(markdown, AssistantMarkdown) and id(token) in markdown._closed_fences
            else None
        )
        return element

    def main_key(code, language, theme, console, options):
        key = fence_key(code, language, theme, console, options)
        return (
            replace(key, context=(options.max_height, options.height, *key.context))
            if key
            else None
        )

    with (
        patch.object(widgets._FencedCodeBlock, "create", side_effect=main_create),
        patch.object(widgets, "fence_render_key", side_effect=main_key),
    ):
        yield


class CodeFlickerReplay(NeuroCodeApp):
    def __init__(self, args):
        super().__init__(
            _VisualFixtureRunner(),
            ui_theme=UiTheme(args.theme),
            provider_name="deterministic-replay",
            model_name=f"code-{args.mode}",
            cwd=Path.cwd(),
        )
        self.args = args
        self.running_replay = False
        self.trace = []

    def on_mount(self):
        super().on_mount()
        if self.args.auto_exit:
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

    def capture_commit(self, view):
        if not isinstance(view.renderable, AssistantMarkdown):
            return
        md = view.renderable
        self.trace.append(
            {
                "event": "view_commit",
                "characters": len(view.content),
                "fences": [
                    {
                        "generation": record.identity.generation,
                        "source_start": record.identity.source_start,
                        "phase": record.phase.value,
                        "foreground": (
                            "PLAIN"
                            if self.args.mode == "stable" and token_id in md._active_fences
                            else "SYNTAX"
                        ),
                        "finalizations": record.finalizations,
                    }
                    for token_id, record in md._fence_records.items()
                ],
            }
        )

    async def replay(self):
        self._replace_transcript([])
        self.trace.clear()
        self._write_entry("user", f"Python code replay · {self.args.mode} · {self.args.speed}")
        await asyncio.sleep(0.2)
        source = ""
        for delta in python_replay(lines=self.args.lines):
            source += delta
            self._update_pending_assistant(source)
            await asyncio.sleep(0.12 if self.args.speed == "slow" else 0.025)
        pending = self._pending_assistant
        self._finish_pending_assistant(source)
        self.capture_commit(pending)
        await asyncio.sleep(0.2)
        self.args.log.parent.mkdir(parents=True, exist_ok=True)
        self.args.log.write_text(
            json.dumps(self.trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.running_replay = False
        if self.args.auto_exit:
            self.exit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["stable", "baseline"], default="stable")
    parser.add_argument("--speed", choices=["slow", "normal"], default="normal")
    parser.add_argument("--theme", choices=["system", "graphite", "porcelain"], default="system")
    parser.add_argument("--lines", type=int, default=200)
    parser.add_argument("--log", type=Path, default=Path("/tmp/neuro-code-fence-replay.json"))
    parser.add_argument("--auto-exit", action="store_true")
    args = parser.parse_args()
    app = CodeFlickerReplay(args)
    commit = AssistantMessage._commit_stream_view

    def capture(view, now):
        commit(view, now)
        if app.running_replay:
            app.capture_commit(view)

    with ExitStack() as stack:
        if args.mode == "baseline":
            stack.enter_context(baseline_rendering())
        stack.enter_context(patch.object(AssistantMessage, "_commit_stream_view", capture))
        app.run()


if __name__ == "__main__":
    main()
