"""Real transport encodings, capability evidence and prompt editing semantics."""

from __future__ import annotations

import pytest
from textual import events
from textual._xterm_parser import XTermParser
from textual.document._document import Selection

from neuro_code.interfaces.tui.terminal_keyboard import TerminalKeyboardCapability
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app


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
