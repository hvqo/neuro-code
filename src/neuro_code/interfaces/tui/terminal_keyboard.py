"""Normalize terminal key reports through native and verified compatibility rules."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import environ


class TerminalInputAction(StrEnum):
    """The prompt-level meaning of an input event."""

    SEND = "send"
    NEWLINE = "newline"


@dataclass(frozen=True, slots=True)
class TerminalIdentity:
    """A positively identified terminal and its parsed release version."""

    name: str
    version: tuple[int, int, int]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> TerminalIdentity | None:
        """Read Konsole's dedicated six-digit version marker; unknown stays unknown."""

        # Multiplexers may inherit the outer terminal's environment while changing
        # the active key path. Do not apply an emulator-specific input quirk there.
        if any(
            marker in environment
            for marker in (
                "TMUX",
                "STY",
                "ZELLIJ",
                "SSH_CLIENT",
                "SSH_CONNECTION",
                "SSH_TTY",
            )
        ):
            return None
        encoded_version = environment.get("KONSOLE_VERSION", "")
        match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})", encoded_version)
        if match is None:
            return None
        major, minor, patch = (int(part) for part in match.groups())
        return cls("Konsole", (major, minor, patch))


@dataclass(frozen=True, slots=True)
class TerminalInputRule:
    """One bounded key-event mapping for a verified terminal release range."""

    terminal_name: str
    minimum_version: tuple[int, int, int]
    maximum_version_exclusive: tuple[int, int, int]
    key: str
    character: str | None
    action: TerminalInputAction
    help_key: str | None = None

    def applies_to_identity(self, identity: TerminalIdentity) -> bool:
        return (
            identity.name == self.terminal_name
            and self.minimum_version <= identity.version < self.maximum_version_exclusive
        )

    def matches(self, identity: TerminalIdentity, key: str, character: str | None) -> bool:
        return (
            self.applies_to_identity(identity) and key == self.key and character == self.character
        )


# Konsole 25.12.x encodes Shift+Return as a characterless SS3 Enter. The same
# event can represent the physical keypad Enter, which intentionally gets the
# same action because the transport no longer carries enough information.
_KONSOLE_25_12_RULES = tuple(
    TerminalInputRule(
        terminal_name="Konsole",
        minimum_version=(25, 12, 0),
        maximum_version_exclusive=(25, 13, 0),
        key=key,
        character=None,
        action=TerminalInputAction.NEWLINE,
        help_key="keyboard.konsole-25.12",
    )
    for key in ("enter", "keypad_enter")
)


class TerminalInputCompatibilityRegistry:
    """Extensible data registry; composer behavior does not name terminals."""

    def __init__(self, rules: Iterable[TerminalInputRule] = ()) -> None:
        self._rules = tuple(rules)

    def resolve(
        self,
        identity: TerminalIdentity | None,
        key: str,
        character: str | None,
    ) -> TerminalInputAction | None:
        if identity is None:
            return None
        return next(
            (rule.action for rule in self._rules if rule.matches(identity, key, character)),
            None,
        )

    def applies_to_identity(self, identity: TerminalIdentity | None) -> bool:
        return identity is not None and any(
            rule.applies_to_identity(identity) for rule in self._rules
        )

    def help_key_for(self, identity: TerminalIdentity | None) -> str | None:
        if identity is None:
            return None
        return next(
            (
                rule.help_key
                for rule in self._rules
                if rule.applies_to_identity(identity) and rule.help_key is not None
            ),
            None,
        )


DEFAULT_TERMINAL_INPUT_COMPATIBILITY = TerminalInputCompatibilityRegistry(_KONSOLE_25_12_RULES)


@dataclass(frozen=True, slots=True)
class TerminalInputNormalizer:
    """Resolve a key event in native, verified-quirk, then fallback order."""

    identity: TerminalIdentity | None = None
    registry: TerminalInputCompatibilityRegistry = DEFAULT_TERMINAL_INPUT_COMPATIBILITY

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] = environ) -> TerminalInputNormalizer:
        return cls(TerminalIdentity.from_environment(environment))

    @property
    def compatibility_active(self) -> bool:
        return self.registry.applies_to_identity(self.identity)

    @property
    def help_key(self) -> str:
        return self.compatibility_help_key or "keyboard.unconfirmed"

    @property
    def compatibility_help_key(self) -> str | None:
        return self.registry.help_key_for(self.identity)

    def normalize(self, key: str, character: str | None = None) -> TerminalInputAction | None:
        """Map a Textual-normalized event while retaining its character evidence."""

        # Native modified-key reports always outrank emulator compatibility rules.
        if key == "shift+enter":
            return TerminalInputAction.NEWLINE

        compatibility_action = self.registry.resolve(self.identity, key, character)
        if compatibility_action is not None:
            return compatibility_action

        if key in {"ctrl+j", "f2"}:
            return TerminalInputAction.NEWLINE
        if key in {"enter", "keypad_enter"}:
            return TerminalInputAction.SEND
        return None


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


__all__ = [
    "DEFAULT_TERMINAL_INPUT_COMPATIBILITY",
    "TerminalIdentity",
    "TerminalInputAction",
    "TerminalInputCompatibilityRegistry",
    "TerminalInputNormalizer",
    "TerminalInputRule",
    "TerminalKeyboardCapability",
]
