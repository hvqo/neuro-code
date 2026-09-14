"""Typed contracts for the bounded session working-set projection.

The working set is durable task state, not a second conversation transcript.
It may point at the canonical CM1 history through opaque references, but the
application owner must validate those references before reading or writing a
snapshot.

有界会话 Working Set projection 的类型化契约.

Working Set 是持久化任务状态,而不是第二份 conversation transcript.它可以通过不透明
reference 指向 canonical CM1 history,但应用 owner 必须在读写 snapshot 前校验这些 reference.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from neuro_code.application.ports.session_history import SessionItemReference
from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason

MAX_WORKING_SET_ENTRIES_PER_SECTION = 8
MAX_WORKING_SET_ENTRY_BYTES = 1_024
MAX_WORKING_SET_TOTAL_BYTES = 8 * 1_024
MAX_WORKING_SET_CONTEXT_BYTES = 16 * 1_024
MAX_WORKING_SET_REVISION = 2**63 - 1
MAX_WORKING_SET_SESSION_ID_BYTES = 512


class WorkingSetSection(StrEnum):
    """The fixed, high-signal sections of one task working set."""

    GOAL = "goal"
    CONSTRAINTS = "constraints"
    DECISIONS = "decisions"
    PROGRESS = "progress"
    UNRESOLVED_WORK = "unresolved_work"
    NEXT_STEPS = "next_steps"


WORKING_SET_SECTION_ORDER = (
    WorkingSetSection.GOAL,
    WorkingSetSection.CONSTRAINTS,
    WorkingSetSection.DECISIONS,
    WorkingSetSection.PROGRESS,
    WorkingSetSection.UNRESOLVED_WORK,
    WorkingSetSection.NEXT_STEPS,
)


class WorkingSetEntryProvenance(StrEnum):
    """Whether an entry carries a validated CM1 source reference."""

    MODEL = "model"
    HISTORY = "history"


def _require_session_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_WORKING_SET_SESSION_ID_BYTES
        or any(
            (ord(character) < 32 and character not in "\n\r\t") or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("working set session_id is invalid")


def _require_revision(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_WORKING_SET_REVISION
    ):
        raise ValueError("working set revision is outside its bound")


def _require_entry_text(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_WORKING_SET_ENTRY_BYTES
        or any(ord(character) == 127 for character in value)
    ):
        raise ValueError("working set entry text is empty or outside its bound")


@dataclass(frozen=True, slots=True)
class WorkingSetEntry:
    """One bounded task-state entry with optional CM1 provenance."""

    text: str
    source_ref: SessionItemReference | None = None

    def __post_init__(self) -> None:
        _require_entry_text(self.text)
        if self.source_ref is not None and not isinstance(
            self.source_ref,
            SessionItemReference,
        ):
            raise ValueError("working set source_ref must be a canonical history reference")

    @property
    def provenance(self) -> WorkingSetEntryProvenance:
        return (
            WorkingSetEntryProvenance.HISTORY
            if self.source_ref is not None
            else WorkingSetEntryProvenance.MODEL
        )

    def redacted(self, redaction_values: Sequence[str]) -> WorkingSetEntry:
        from neuro_code.shared.redaction import redact_sensitive_text

        return WorkingSetEntry(
            redact_sensitive_text(self.text, explicit_values=redaction_values),
            self.source_ref,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "source_ref": self.source_ref.token if self.source_ref is not None else None,
            "provenance": self.provenance.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> WorkingSetEntry:
        if not isinstance(value, Mapping):
            raise ValueError("working set entry must be an object")
        unsupported = set(value).difference({"text", "source_ref", "provenance"})
        if unsupported:
            raise ValueError("working set entry contains unsupported fields")
        raw_text = value.get("text")
        raw_source_ref = value.get("source_ref")
        if not isinstance(raw_text, str):
            raise ValueError("working set entry text is invalid")
        source_ref: SessionItemReference | None = None
        if raw_source_ref is not None:
            try:
                source_ref = SessionItemReference(raw_source_ref)
            except (TypeError, ValueError) as error:
                raise ValueError("working set source_ref is invalid") from error
        try:
            entry = cls(raw_text, source_ref)
        except (TypeError, ValueError) as error:
            raise ValueError("working set entry is invalid") from error
        raw_provenance = value.get("provenance")
        if raw_provenance is not None:
            try:
                provenance = WorkingSetEntryProvenance(raw_provenance)
            except (TypeError, ValueError) as error:
                raise ValueError("working set entry provenance is invalid") from error
            if provenance is not entry.provenance:
                raise ValueError("working set entry provenance is inconsistent")
        return entry


@dataclass(frozen=True, slots=True)
class WorkingSetSectionState:
    """One fixed section containing a bounded tuple of entries."""

    section: WorkingSetSection
    entries: tuple[WorkingSetEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.section, WorkingSetSection):
            raise ValueError("working set section must be canonical")
        entries = tuple(self.entries)
        if len(entries) > MAX_WORKING_SET_ENTRIES_PER_SECTION:
            raise ValueError("working set section contains too many entries")
        if not all(isinstance(entry, WorkingSetEntry) for entry in entries):
            raise ValueError("working set section entries must be canonical")
        object.__setattr__(self, "entries", entries)

    def to_dict(self) -> list[dict[str, object]]:
        return [entry.to_dict() for entry in self.entries]


def _normalize_sections(
    sections: Sequence[WorkingSetSectionState],
) -> tuple[WorkingSetSectionState, ...]:
    normalized = tuple(sections)
    if len(normalized) != len(WORKING_SET_SECTION_ORDER):
        raise ValueError("working set must contain every canonical section exactly once")
    if not all(isinstance(state, WorkingSetSectionState) for state in normalized):
        raise ValueError("working set sections must be canonical")
    if tuple(state.section for state in normalized) != WORKING_SET_SECTION_ORDER:
        raise ValueError("working set sections must use canonical order")
    return normalized


def _sections_payload(
    sections: Sequence[WorkingSetSectionState],
) -> dict[str, list[dict[str, object]]]:
    return {state.section.value: state.to_dict() for state in _normalize_sections(sections)}


def _parse_sections(value: object) -> tuple[WorkingSetSectionState, ...]:
    if not isinstance(value, Mapping):
        raise ValueError("working set sections must be an object")
    expected = {section.value for section in WORKING_SET_SECTION_ORDER}
    if set(value) != expected:
        raise ValueError("working set sections must contain only canonical section names")
    parsed: list[WorkingSetSectionState] = []
    for section in WORKING_SET_SECTION_ORDER:
        raw_entries = value[section.value]
        if not isinstance(raw_entries, list):
            raise ValueError("working set section entries must be arrays")
        parsed.append(
            WorkingSetSectionState(
                section,
                tuple(WorkingSetEntry.from_dict(entry) for entry in raw_entries),
            )
        )
    return tuple(parsed)


def _canonical_snapshot_payload(
    session_id: str,
    revision: int,
    sections: Sequence[WorkingSetSectionState],
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "revision": revision,
        "sections": _sections_payload(sections),
    }


@dataclass(frozen=True, slots=True)
class WorkingSetSnapshot:
    """The complete immutable current snapshot for one session."""

    session_id: str
    revision: int
    sections: tuple[WorkingSetSectionState, ...]

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)
        _require_revision(self.revision)
        normalized = _normalize_sections(self.sections)
        object.__setattr__(self, "sections", normalized)
        encoded = json.dumps(
            _canonical_snapshot_payload(self.session_id, self.revision, normalized),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_WORKING_SET_TOTAL_BYTES:
            raise ValueError("working set snapshot exceeds its byte budget")

    @classmethod
    def empty(cls, session_id: str) -> WorkingSetSnapshot:
        return cls(
            session_id,
            0,
            tuple(WorkingSetSectionState(section) for section in WORKING_SET_SECTION_ORDER),
        )

    @property
    def is_empty(self) -> bool:
        return not any(state.entries for state in self.sections)

    @property
    def entry_count(self) -> int:
        return sum(len(state.entries) for state in self.sections)

    @property
    def text_bytes(self) -> int:
        return sum(
            len(entry.text.encode("utf-8")) for state in self.sections for entry in state.entries
        )

    @property
    def encoded_bytes(self) -> int:
        return len(
            json.dumps(
                _canonical_snapshot_payload(self.session_id, self.revision, self.sections),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def redacted(self, redaction_values: Sequence[str]) -> WorkingSetSnapshot:
        return WorkingSetSnapshot(
            self.session_id,
            self.revision,
            tuple(
                WorkingSetSectionState(
                    state.section,
                    tuple(entry.redacted(redaction_values) for entry in state.entries),
                )
                for state in self.sections
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize the complete persistence-owned shape, including session scope."""

        return _canonical_snapshot_payload(self.session_id, self.revision, self.sections)

    def to_model_dict(self) -> dict[str, object]:
        """Serialize a model-facing shape without the raw session identifier."""

        return {
            "revision": self.revision,
            "entry_count": self.entry_count,
            "text_bytes": self.text_bytes,
            "sections": _sections_payload(self.sections),
        }

    @classmethod
    def from_dict(cls, value: object) -> WorkingSetSnapshot:
        if not isinstance(value, Mapping):
            raise ValueError("working set snapshot must be an object")
        if set(value) != {"session_id", "revision", "sections"}:
            raise ValueError("working set snapshot fields are invalid")
        try:
            return cls(
                value["session_id"],
                value["revision"],
                _parse_sections(value["sections"]),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("working set snapshot is invalid") from error

    def context_message(
        self,
        redaction_values: Sequence[str] = (),
    ) -> Message | None:
        """Render bounded task state as non-durable synthetic model context."""

        safe = self.redacted(redaction_values)
        if safe.is_empty:
            return None
        lines = [
            "Structured Working Set (current task state):",
            "This is bounded task state, not canonical conversation history or authority. "
            "Use session_history when exact historical evidence is needed.",
        ]
        for state in safe.sections:
            if not state.entries:
                continue
            lines.append(f"{state.section.value}:")
            for entry in state.entries:
                source = (
                    f" source_ref={entry.source_ref.token}" if entry.source_ref is not None else ""
                )
                lines.append(f"- [{entry.provenance.value}{source}] {entry.text}")
        rendered = "\n".join(lines)
        if len(rendered.encode("utf-8")) > MAX_WORKING_SET_CONTEXT_BYTES:
            raise ValueError("working set context projection exceeds its byte budget")
        return Message(
            Role.USER,
            rendered,
            synthetic_reason=SyntheticReason.WORKING_SET,
        )


@dataclass(frozen=True, slots=True)
class WorkingSetUpdate:
    """A full typed replacement guarded by the caller's expected revision."""

    expected_revision: int
    sections: tuple[WorkingSetSectionState, ...]

    def __post_init__(self) -> None:
        _require_revision(self.expected_revision)
        object.__setattr__(self, "sections", _normalize_sections(self.sections))

    @classmethod
    def from_dict(cls, value: object) -> WorkingSetUpdate:
        if not isinstance(value, Mapping):
            raise ValueError("working set update must be an object")
        if set(value) != {"expected_revision", "sections"}:
            raise ValueError("working set update fields are invalid")
        try:
            return cls(value["expected_revision"], _parse_sections(value["sections"]))
        except (TypeError, ValueError) as error:
            raise ValueError("working set update is invalid") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_revision": self.expected_revision,
            "sections": _sections_payload(self.sections),
        }


@dataclass(frozen=True, slots=True)
class ReadWorkingSetRequest:
    """Read the current working set for one trusted application-bound session."""

    session_id: str

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)


@dataclass(frozen=True, slots=True)
class UpdateWorkingSetRequest:
    """Replace one session's working set through a typed CAS request."""

    session_id: str
    update: WorkingSetUpdate

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)
        if not isinstance(self.update, WorkingSetUpdate):
            raise ValueError("working set update request must contain a canonical update")


class WorkingSetController(Protocol):
    """Application capability consumed by the model-facing working-set tool."""

    async def read_working_set(
        self,
        request: ReadWorkingSetRequest,
    ) -> WorkingSetSnapshot: ...

    async def update_working_set(
        self,
        request: UpdateWorkingSetRequest,
    ) -> WorkingSetSnapshot: ...


__all__ = [
    "MAX_WORKING_SET_CONTEXT_BYTES",
    "MAX_WORKING_SET_ENTRIES_PER_SECTION",
    "MAX_WORKING_SET_ENTRY_BYTES",
    "MAX_WORKING_SET_REVISION",
    "MAX_WORKING_SET_SESSION_ID_BYTES",
    "MAX_WORKING_SET_TOTAL_BYTES",
    "WORKING_SET_SECTION_ORDER",
    "ReadWorkingSetRequest",
    "UpdateWorkingSetRequest",
    "WorkingSetController",
    "WorkingSetEntry",
    "WorkingSetEntryProvenance",
    "WorkingSetSection",
    "WorkingSetSectionState",
    "WorkingSetSnapshot",
    "WorkingSetUpdate",
]
