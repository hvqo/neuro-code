"""Contracts for the bounded, current-session context rollover control.

Context rollover changes only the active model projection.  The canonical
session item sequence and the structured Working Set remain owned by their
existing persistence boundaries.

当前会话有界 context rollover control 的契约.

Context rollover 只改变 active model projection。canonical session item sequence 和
structured Working Set 仍由现有持久化边界拥有.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CONTEXT_ROLLOVER_TOOL_NAME = "new_context"
MAX_CONTEXT_GENERATION = 2**63 - 1
MAX_CONTEXT_ROLLOVER_ITEM_BOUNDARY = 2**63 - 1
MAX_CONTEXT_ROLLOVER_SESSION_ID_BYTES = 512
MAX_CONTEXT_ROLLOVER_TURN_ID_BYTES = 512


def _require_session_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_CONTEXT_ROLLOVER_SESSION_ID_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("context rollover session_id is invalid")


def _require_generation(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CONTEXT_GENERATION
    ):
        raise ValueError("context generation is invalid")


def _require_optional_item_boundary(value: int | None) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CONTEXT_ROLLOVER_ITEM_BOUNDARY
    ):
        raise ValueError("context rollover item boundary is invalid")


def _require_optional_turn_id(value: str | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_CONTEXT_ROLLOVER_TURN_ID_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("context rollover turn_id is invalid")


@dataclass(frozen=True, slots=True)
class ReadContextRolloverRequest:
    """Read the durable active-context generation for one session."""

    session_id: str

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)


@dataclass(frozen=True, slots=True)
class AdvanceContextRolloverRequest:
    """Advance one session to its next fresh active-context generation."""

    session_id: str
    history_item_boundary: int | None = None
    turn_id: str | None = None

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)
        _require_optional_item_boundary(self.history_item_boundary)
        _require_optional_turn_id(self.turn_id)
        if (self.history_item_boundary is None) != (self.turn_id is None):
            raise ValueError("context rollover item boundary and turn_id must be supplied together")


@dataclass(frozen=True, slots=True)
class ContextRolloverState:
    """The durable generation selected for one runtime-bound session."""

    session_id: str
    generation: int
    history_item_boundary: int = 0

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)
        _require_generation(self.generation)
        _require_optional_item_boundary(self.history_item_boundary)


class ContextRolloverController(Protocol):
    """Application boundary used by the model control and Agent runtime."""

    async def read_context_rollover(
        self,
        request: ReadContextRolloverRequest,
    ) -> ContextRolloverState: ...

    async def advance_context_rollover(
        self,
        request: AdvanceContextRolloverRequest,
    ) -> ContextRolloverState: ...


__all__ = [
    "CONTEXT_ROLLOVER_TOOL_NAME",
    "MAX_CONTEXT_GENERATION",
    "MAX_CONTEXT_ROLLOVER_ITEM_BOUNDARY",
    "MAX_CONTEXT_ROLLOVER_SESSION_ID_BYTES",
    "MAX_CONTEXT_ROLLOVER_TURN_ID_BYTES",
    "AdvanceContextRolloverRequest",
    "ContextRolloverController",
    "ContextRolloverState",
    "ReadContextRolloverRequest",
]
