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
    PASS_THROUGH = "pass_through"


@dataclass(frozen=True, slots=True)
class TerminalIdentity:
    """A positively identified terminal and its parsed release version."""

    family: str
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
    """Evidence-carrying key mapping for one verified terminal release range."""

    terminal_family: str
    minimum_version: tuple[int, int, int]
    maximum_version_exclusive: tuple[int, int, int]
    observed_key: str
    observed_character: str | None
    observed_wire_sequence: str
    action: TerminalInputAction
    known_tradeoff: str
    evidence: tuple[str, ...]
    regression_coverage: tuple[str, ...]
    help_key: str | None = None

    def __post_init__(self) -> None:
        if self.minimum_version >= self.maximum_version_exclusive:
            raise ValueError("terminal compatibility version range must be non-empty")
        metadata = (
            self.terminal_family.strip(),
            self.observed_key.strip(),
            self.observed_wire_sequence,
            self.known_tradeoff.strip(),
            *self.evidence,
            *self.regression_coverage,
        )
        if not all(metadata) or not self.evidence or not self.regression_coverage:
            raise ValueError(
                "terminal compatibility rules require input, tradeoff, evidence, and regression coverage"
            )

    def applies_to_identity(self, identity: TerminalIdentity) -> bool:
        return (
            identity.family == self.terminal_family
            and self.minimum_version <= identity.version < self.maximum_version_exclusive
        )

    def matches(self, identity: TerminalIdentity, key: str, character: str | None) -> bool:
        return (
            self.applies_to_identity(identity)
            and key == self.observed_key
            and character == self.observed_character
        )


# Konsole 25.12.x encodes Shift+Return as a characterless SS3 Enter. The same
# event can represent the physical keypad Enter, which intentionally gets the
# same action because the transport no longer carries enough information.
_KONSOLE_25_12_RULES = tuple(
    TerminalInputRule(
        terminal_family="Konsole",
        minimum_version=(25, 12, 0),
        maximum_version_exclusive=(25, 13, 0),
        observed_key=key,
        observed_character=None,
        observed_wire_sequence="\x1bOM",
        action=TerminalInputAction.NEWLINE,
        known_tradeoff=(
            "Physical keypad Enter produces the same SS3 event and therefore also inserts "
            "a newline; the transport cannot distinguish it from Shift+Return."
        ),
        evidence=(
            "https://github.com/KDE/konsole/blob/v25.12.3/src/session/SessionManager.cpp#L1254-L1286",
            "https://github.com/KDE/konsole/blob/v25.12.3/data/keyboard-layouts/default.keytab",
            "https://github.com/Textualize/textual/blob/v1.0.0/src/textual/_xterm_parser.py",
        ),
        regression_coverage=(
            "tests/test_tui_terminal_keyboard.py::test_normalizer_prefers_native_keys_then_quirk_then_fallbacks",
            "tests/test_tui_terminal_keyboard.py::test_real_driver_negotiates_restores_and_delivers_prompt_input",
            "tests/test_tui_terminal_keyboard.py::test_konsole_compatibility_rule_records_its_evidence_and_tradeoff",
        ),
        help_key="keyboard.konsole-25.12",
    )
    for key in ("enter", "keypad_enter")
)


class TerminalInputCompatibilityRegistry:
    """Extensible data registry; composer behavior does not name terminals."""

    def __init__(self, rules: Iterable[TerminalInputRule] = ()) -> None:
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[TerminalInputRule, ...]:
        """Expose the immutable rule catalogue for diagnostics and audits."""

        return self._rules

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

    def normalize(self, key: str, character: str | None = None) -> TerminalInputAction:
        """Return a prompt action; unrelated editing keys pass to Textual unchanged."""

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
        return TerminalInputAction.PASS_THROUGH


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
