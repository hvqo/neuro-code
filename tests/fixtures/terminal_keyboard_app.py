"""Provider-free PromptInput fixture using Textual's real POSIX driver."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.drivers.linux_driver import LinuxDriver

from neuro_code.interfaces.tui.widgets import PromptInput


class KeyboardApp(App[dict[str, object]]):
    def compose(self) -> ComposeResult:
        yield PromptInput(id="prompt")

    def on_mount(self) -> None:
        self.query_one(PromptInput).focus()
        self.call_after_refresh(Path(sys.argv[1]).write_text, "ready", encoding="utf-8")

    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        self.exit(
            {
                "value": message.value,
                "modified_enter_observed": self.query_one(
                    PromptInput
                ).keyboard_capability.modified_enter_observed,
            }
        )


if __name__ == "__main__":
    result = KeyboardApp(driver_class=LinuxDriver).run()
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
