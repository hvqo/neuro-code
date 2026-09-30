"""Real transport encodings, capability evidence and prompt editing semantics."""

from __future__ import annotations

import errno
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from textual import events
from textual._xterm_parser import XTermParser
from textual.document._document import Selection

from neuro_code.interfaces.tui.terminal_keyboard import TerminalKeyboardCapability
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app


@pytest.mark.skipif(os.name != "posix", reason="Textual LinuxDriver requires a POSIX PTY")
@pytest.mark.parametrize(
    ("wire", "value", "modified"),
    [
        ("first\x1b[O\x1b[I\x1b[13;2usecond\r", "first\nsecond", True),
        ("legacy\x1bOM", "legacy", False),
        ("\x1b[200~中文 draft\r\nsecond\x1b[201~\r", "中文 draft\nsecond", False),
    ],
)
def test_real_driver_negotiates_restores_and_delivers_prompt_input(
    tmp_path: Path, wire: str, value: str, modified: bool
) -> None:
    """A terminal request is not support; only wire events preserve modifiers.

    Exercise the actual driver lifecycle, input thread, parser, message queue,
    and PromptInput without a Provider or a second terminal input reader.
    """
    import pty
    import termios

    master, slave = pty.openpty()
    original_mode = termios.tcgetattr(slave)
    ready = tmp_path / "ready"
    result = tmp_path / "result.json"
    environment = os.environ.copy()
    for name in ("TEXTUAL_DRIVER", "TEXTUAL_PRESS"):
        environment.pop(name, None)
    environment.update(TEXTUAL_ANIMATIONS="none", TERM="xterm-256color")
    output = bytearray()
    child = subprocess.Popen(
        [sys.executable, "tests/fixtures/terminal_keyboard_app.py", str(ready), str(result)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
    )

    def read_output() -> None:
        if select.select([master], [], [], 0.05)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
        assert len(output) < 1_000_000, "terminal fixture output exceeded its bound"

    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            read_output()
        assert ready.exists(), output.decode("utf-8", errors="replace")
        read_output()
        assert b"\x1b[>1u" in output
        os.write(master, wire.encode("utf-8"))
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            read_output()
        assert child.poll() == 0, output.decode("utf-8", errors="replace")
        while select.select([master], [], [], 0)[0]:
            read_output()
        assert json.loads(result.read_text(encoding="utf-8")) == {
            "value": value,
            "modified_enter_observed": modified,
        }
        # The same driver owns teardown: restore protocol before alt-screen exit
        # and restore raw-mode changes; focus cycling never adds another push.
        assert output.count(b"\x1b[>1u") == 1
        assert output.count(b"\x1b[<u") == 1
        assert output.index(b"\x1b[<u") < output.index(b"\x1b[?1049l")
        assert b"\x1b[?2004h" in output
        assert b"\x1b[?2004l" in output
        assert termios.tcgetattr(slave) == original_mode
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    ("wire", "key"),
    [
        ("\r", "enter"),
        ("\x1bOM", "enter"),
        ("\x1b[13u", "enter"),
        ("\x1b[13;2u", "shift+enter"),
        ("\x1b[13;66u", "shift+enter"),
    ],
)
def test_textual_normalizes_enter_transport_without_guessing_modifiers(wire: str, key: str) -> None:
    messages = list(XTermParser().feed(wire))
    assert len(messages) == 1
    assert isinstance(messages[0], events.Key)
    assert messages[0].key == key


def test_capability_requires_observed_modifier_not_term_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-kitty")
    capability = TerminalKeyboardCapability()
    for key in ("enter", "ctrl+j", "f2", "alt+enter"):
        capability.observe(key)
    assert not capability.modified_enter_observed
    assert capability.help_key == "keyboard.unconfirmed"
    capability.observe("shift+enter")
    capability.observe("enter")
    assert capability.modified_enter_observed
    assert capability.help_key == "keyboard.confirmed"


@pytest.mark.asyncio
async def test_enhanced_shift_enter_replaces_selection_and_help_reports_observation() -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "first remove last"
        prompt.selection = Selection((0, 5), (0, 12))
        for message in XTermParser().feed("\x1b[13;2u"):
            prompt.post_message(message)
        await pilot.pause()
        assert prompt.value == "first\n last"
        assert not app.entries
        assert prompt.keyboard_capability.modified_enter_observed
        app.action_show_help()
        await pilot.pause()
        assert "distinct Shift+Enter observed" in app.entries[-1].text


@pytest.mark.asyncio
async def test_bracketed_multiline_paste_remains_text_not_submit() -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        wire = "\x1b[200~中文 draft\r\nsecond\x1b[201~"
        messages = list(XTermParser().feed(wire + "x"))
        paste = next(message for message in messages if isinstance(message, events.Paste))
        prompt.post_message(paste)
        await pilot.pause()
        assert prompt.value == "中文 draft\nsecond"
        assert not app.entries
        assert not prompt.keyboard_capability.modified_enter_observed


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["shift+enter", "ctrl+j", "f2"])
async def test_read_only_prompt_preserves_draft_on_newline(key: str) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        prompt.value = "draft"
        prompt.read_only = True
        await pilot.press(key)
        assert prompt.value == "draft"
        assert not app.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["\r", "\x1bOM", "\x1b[13u", "\x1b[13;2u"])
async def test_normalized_transport_reaches_send_or_newline(wire: str) -> None:
    from textual.app import App, ComposeResult

    class InputHarness(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.submissions: list[str] = []

        def compose(self) -> ComposeResult:
            yield PromptInput(id="prompt")

        def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
            self.submissions.append(message.value)

    app = InputHarness()
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        prompt.value = "中文 draft"
        prompt.cursor_position = len(prompt.value)
        for message in XTermParser().feed(wire):
            prompt.post_message(message)
        await pilot.pause()
        if wire == "\x1b[13;2u":
            assert prompt.value == "中文 draft\n"
            assert not app.submissions
        else:
            assert app.submissions == ["中文 draft"]
            assert prompt.value == "中文 draft"
