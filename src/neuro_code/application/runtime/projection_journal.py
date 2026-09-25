"""Bounded session-binding projection revisions kept outside canonical history.

规范会话历史之外、按会话绑定管理的有界投影修订。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from neuro_code.domain.conversation.messages import Message, SessionItem, SyntheticReason

MAX_PROJECTION_JOURNAL_ENTRIES = 256
MAX_PROJECTION_JOURNAL_BYTES = 512 * 1024

_JOURNALED_REASONS = frozenset(
    {
        SyntheticReason.WORKING_SET,
        SyntheticReason.RUNTIME_PLAN,
        SyntheticReason.RUNTIME_BUDGET,
        SyntheticReason.RUNTIME_CHECKPOINT,
        SyntheticReason.RUNTIME_SUPERVISION,
        SyntheticReason.RUNTIME_BACKGROUND_TASK,
        SyntheticReason.RUNTIME_CONTEXT_ROLLOVER,
        SyntheticReason.INSTRUCTION_SCOPE_REVISION,
        SyntheticReason.SKILL_SCOPE_REVISION,
    }
)


class ProjectionJournalLimitError(ValueError):
    """Raised rather than silently dropping a model-visible revision."""


@dataclass(frozen=True, slots=True)
class ProjectionJournalEntry:
    durable_item_boundary: int
    message: Message
    fingerprint: str
    byte_count: int


def _message_fingerprint(message: Message) -> str:
    material = "\0".join(
        (
            message.synthetic_reason.value if message.synthetic_reason is not None else "",
            message.role.value,
            message.content,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ProviderProjectionJournal:
    """Append-only, in-memory control revisions for one active binding.

    The journal stores only synthetic messages owned by the application. It is
    intentionally not persisted; recreating the binding starts a fresh cache
    epoch and rebuilds only current stable authority snapshots.
    """

    __slots__ = ("_bytes", "_entries", "_keys", "_latest_by_reason")

    def __init__(self) -> None:
        self._entries: list[ProjectionJournalEntry] = []
        self._keys: set[tuple[int, SyntheticReason, str]] = set()
        self._latest_by_reason: dict[SyntheticReason, str] = {}
        self._bytes = 0

    @property
    def entries(self) -> tuple[ProjectionJournalEntry, ...]:
        return tuple(self._entries)

    @property
    def byte_count(self) -> int:
        return self._bytes

    def append(self, durable_item_boundary: int, message: Message) -> bool:
        if (
            isinstance(durable_item_boundary, bool)
            or not isinstance(durable_item_boundary, int)
            or durable_item_boundary < 0
        ):
            raise ValueError("durable item boundary must be non-negative")
        if not isinstance(message, Message) or message.synthetic_reason not in _JOURNALED_REASONS:
            raise TypeError("projection journal accepts only owned synthetic messages")
        digest = _message_fingerprint(message)
        reason = message.synthetic_reason
        assert reason is not None
        key = (durable_item_boundary, reason, digest)
        if key in self._keys or self._latest_by_reason.get(reason) == digest:
            return False
        byte_count = len(message.content.encode("utf-8"))
        if len(self._entries) >= MAX_PROJECTION_JOURNAL_ENTRIES:
            raise ProjectionJournalLimitError("projection journal entry limit reached")
        if self._bytes + byte_count > MAX_PROJECTION_JOURNAL_BYTES:
            raise ProjectionJournalLimitError("projection journal byte limit reached")
        self._entries.append(
            ProjectionJournalEntry(durable_item_boundary, message, digest, byte_count)
        )
        self._keys.add(key)
        self._latest_by_reason[reason] = digest
        self._bytes += byte_count
        return True

    def project(self, canonical_items: tuple[SessionItem, ...]) -> tuple[SessionItem, ...]:
        by_boundary: dict[int, list[Message]] = {}
        for entry in self._entries:
            boundary = min(entry.durable_item_boundary, len(canonical_items))
            by_boundary.setdefault(boundary, []).append(entry.message)
        result: list[SessionItem] = []
        for boundary in range(len(canonical_items) + 1):
            result.extend(by_boundary.get(boundary, ()))
            if boundary < len(canonical_items):
                result.append(canonical_items[boundary])
        return tuple(result)

    def reset(self) -> None:
        self._entries.clear()
        self._keys.clear()
        self._latest_by_reason.clear()
        self._bytes = 0


__all__ = [
    "MAX_PROJECTION_JOURNAL_BYTES",
    "MAX_PROJECTION_JOURNAL_ENTRIES",
    "ProjectionJournalEntry",
    "ProjectionJournalLimitError",
    "ProviderProjectionJournal",
]
