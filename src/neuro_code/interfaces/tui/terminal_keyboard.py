"""Keyboard capability from Textual normalized input, never terminal names.

Textual owns protocol negotiation, parsing, paste and input lifecycle. Requesting
Kitty enhancement is not proof that a terminal supports it. Only an observed
modified Enter proves that this input path preserves that modifier. Legacy CR
and SS3 keypad Enter stay Enter; neither carries a reliable Shift bit.
"""

from dataclasses import dataclass


@dataclass(slots=True)
class TerminalKeyboardCapability:
    """A bounded observation; absence of evidence does not assert support."""

    modified_enter_observed: bool = False

    def observe(self, key: str) -> None:
        if key == "shift+enter":
            self.modified_enter_observed = True

    @property
    def help_key(self) -> str:
        return "keyboard.confirmed" if self.modified_enter_observed else "keyboard.unconfirmed"
