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

from neuro_code.interfaces.tui.terminal_keyboard import (
    TerminalIdentity,
    TerminalInputAction,
    TerminalInputCompatibilityRegistry,
    TerminalInputNormalizer,
    TerminalInputRule,
    TerminalKeyboardCapability,
)
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app


@pytest.mark.skipif(os.name != "posix", reason="Textual LinuxDriver requires a POSIX PTY")
@pytest.mark.parametrize(
    ("wire", "value", "modified", "konsole_version"),
    [
        ("first\x1b[O\x1b[I\x1b[13;2usecond\r", "first\nsecond", True, None),
        ("legacy\x1bOM", "legacy", False, None),
        ("before\x1bOMafter\r", "before\nafter", False, "251203"),
        ("\x1b[200~中文 draft\r\nsecond\x1b[201~\r", "中文 draft\nsecond", False, None),
    ],
)
def test_real_driver_negotiates_restores_and_delivers_prompt_input(
    tmp_path: Path, wire: str, value: str, modified: bool, konsole_version: str | None
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
    for name in (
        "KONSOLE_VERSION",
        "TMUX",
        "STY",
        "ZELLIJ",
        "SSH_CLIENT",
        "SSH_CONNECTION",
        "SSH_TTY",
    ):
        environment.pop(name, None)
    if konsole_version is not None:
        environment["KONSOLE_VERSION"] = konsole_version
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
    if wire == "\r":
        assert messages[0].character == "\r"
    elif wire == "\x1bOM":
        assert messages[0].character is None


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


@pytest.mark.parametrize(
    ("encoded_version", "recognized"),
    [
        ("251200", True),
        ("251203", True),
        ("251299", True),
        ("251199", False),
        ("251300", False),
        ("25.12.3", False),
        ("25123", False),
        (" 251203", False),
        ("", False),
    ],
)
def test_konsole_identity_requires_dedicated_version_marker_and_exact_range(
    encoded_version: str, recognized: bool
) -> None:
    normalizer = TerminalInputNormalizer.from_environment({"KONSOLE_VERSION": encoded_version})

    assert (normalizer.identity is not None) is (
        len(encoded_version) == 6 and encoded_version.isdigit()
    )
    assert normalizer.compatibility_active is recognized
    assert normalizer.normalize("enter", None) is (
        TerminalInputAction.NEWLINE if recognized else TerminalInputAction.SEND
    )


@pytest.mark.parametrize(
    "marker", ["TMUX", "STY", "ZELLIJ", "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"]
)
def test_known_multiplexer_markers_fail_closed(marker: str) -> None:
    normalizer = TerminalInputNormalizer.from_environment(
        {"KONSOLE_VERSION": "251203", marker: "active"}
    )

    assert normalizer.identity is None
    assert normalizer.normalize("enter", None) is TerminalInputAction.SEND


def test_normalizer_prefers_native_keys_then_quirk_then_fallbacks() -> None:
    normalizer = TerminalInputNormalizer.from_environment({"KONSOLE_VERSION": "251203"})

    assert normalizer.normalize("enter", "\r") is TerminalInputAction.SEND
    assert normalizer.normalize("enter", None) is TerminalInputAction.NEWLINE
    assert normalizer.normalize("keypad_enter", None) is TerminalInputAction.NEWLINE
    assert normalizer.normalize("shift+enter") is TerminalInputAction.NEWLINE
    assert normalizer.normalize("ctrl+j") is TerminalInputAction.NEWLINE
    assert normalizer.normalize("f2") is TerminalInputAction.NEWLINE
    assert normalizer.normalize("alt+enter") is None


def test_unidentified_keypad_enter_does_not_apply_konsole_rule() -> None:
    normalizer = TerminalInputNormalizer.from_environment({"TERM": "konsole-256color"})

    assert normalizer.identity is None
    assert normalizer.normalize("enter", None) is TerminalInputAction.SEND
    assert normalizer.normalize("keypad_enter", None) is TerminalInputAction.SEND


def test_registry_accepts_future_compatibility_rules_without_composer_terminal_branches() -> None:
    future_rule = TerminalInputRule(
        terminal_name="ExampleTerminal",
        minimum_version=(1, 4, 0),
        maximum_version_exclusive=(1, 5, 0),
        key="enter",
        character=None,
        action=TerminalInputAction.NEWLINE,
    )
    normalizer = TerminalInputNormalizer(
        identity=TerminalIdentity("ExampleTerminal", (1, 4, 2)),
        registry=TerminalInputCompatibilityRegistry((future_rule,)),
    )

    assert normalizer.normalize("enter", None) is TerminalInputAction.NEWLINE
    assert normalizer.normalize("enter", "\r") is TerminalInputAction.SEND


@pytest.mark.asyncio
async def test_prompt_input_consumes_registry_rule_without_terminal_specific_logic() -> None:
    from textual.app import App, ComposeResult

    rule = TerminalInputRule(
        terminal_name="ExampleTerminal",
        minimum_version=(1, 0, 0),
        maximum_version_exclusive=(2, 0, 0),
        key="enter",
        character=None,
        action=TerminalInputAction.NEWLINE,
    )
    normalizer = TerminalInputNormalizer(
        identity=TerminalIdentity("ExampleTerminal", (1, 2, 3)),
        registry=TerminalInputCompatibilityRegistry((rule,)),
    )

    class InputHarness(App[None]):
        def compose(self) -> ComposeResult:
            yield PromptInput(id="prompt", input_normalizer=normalizer)

    app = InputHarness()
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        prompt.value = "draft"
        prompt.cursor_position = len(prompt.value)
        for message in XTermParser().feed("\x1bOM"):
            prompt.post_message(message)
        await pilot.pause()

        assert prompt.value == "draft\n"
        assert prompt.keyboard_help_key == "keyboard.unconfirmed"


@pytest.mark.asyncio
async def test_konsole_help_discloses_keypad_enter_tradeoff_after_native_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KONSOLE_VERSION", "251203")
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test() as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        prompt.keyboard_capability.observe("shift+enter")
        app.action_show_help()
        await pilot.pause()

        assert "physical keypad Enter does too" in app.entries[-1].text


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
@pytest.mark.parametrize(
    ("wire", "environment", "action"),
    [
        ("\r", {}, "send"),
        ("\x1bOM", {}, "send"),
        ("\x1b[13u", {}, "send"),
        ("\x1b[13;2u", {}, "newline"),
        ("\x1bOM", {"KONSOLE_VERSION": "251203"}, "newline"),
    ],
)
async def test_normalized_transport_reaches_prompt_action(
    wire: str, environment: dict[str, str], action: str
) -> None:
    from textual.app import App, ComposeResult

    class InputHarness(App[None]):
        def __init__(self, normalizer: TerminalInputNormalizer) -> None:
            super().__init__()
            self.submissions: list[str] = []
            self.normalizer = normalizer

        def compose(self) -> ComposeResult:
            yield PromptInput(id="prompt", input_normalizer=self.normalizer)

        def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
            self.submissions.append(message.value)

    app = InputHarness(TerminalInputNormalizer.from_environment(environment))
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        prompt.value = "中文 draft"
        prompt.cursor_position = len(prompt.value)
        for message in XTermParser().feed(wire):
            prompt.post_message(message)
        await pilot.pause()
        if action == "newline":
            assert prompt.value == "中文 draft\n"
            assert not app.submissions
        else:
            assert app.submissions == ["中文 draft"]
            assert prompt.value == "中文 draft"
