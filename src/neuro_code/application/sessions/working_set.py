"""Application owner for the bounded session Working Set projection.

This service validates CM1 source references and applies the configured
redaction policy before a snapshot is exposed or persisted.  SQLite owns the
atomic revision check; this owner supplies the application-level provenance
boundary around it.

有界会话 Working Set projection 的应用层 owner.

该服务在 snapshot 暴露或持久化前校验 CM1 source reference 并应用配置的脱敏策略.SQLite
负责 revision 的原子检查;本 owner 负责其外层的应用 provenance boundary.
"""

from __future__ import annotations

from collections.abc import Sequence

from neuro_code.application.ports.session_history import SessionItemReference
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.working_set import (
    ReadWorkingSetRequest,
    UpdateWorkingSetRequest,
    WorkingSetSnapshot,
)
from neuro_code.shared.errors import SessionError


class SessionWorkingSetApplicationService:
    """Read and atomically replace one session's structured task state."""

    __slots__ = ("_redaction_values", "_store")

    def __init__(self, store: SessionStore, *, redaction_values: Sequence[str] = ()) -> None:
        values = tuple(redaction_values)
        if not all(isinstance(value, str) for value in values):
            raise TypeError("redaction_values must contain strings")
        self._store = store
        self._redaction_values = tuple(value for value in values if value)

    async def read_working_set(self, request: ReadWorkingSetRequest) -> WorkingSetSnapshot:
        """Load one current snapshot, validating every stored history reference."""

        if not isinstance(request, ReadWorkingSetRequest):
            raise ValueError("working set read request must be canonical")
        snapshot = await self._store.load_working_set(request.session_id)
        if snapshot is None:
            return WorkingSetSnapshot.empty(request.session_id)
        self._validate_snapshot_scope(snapshot, request.session_id)
        await self._validate_source_references(snapshot)
        return snapshot.redacted(self._redaction_values)

    async def update_working_set(self, request: UpdateWorkingSetRequest) -> WorkingSetSnapshot:
        """Validate and persist one complete snapshot replacement with CAS."""

        if not isinstance(request, UpdateWorkingSetRequest):
            raise ValueError("working set update request must be canonical")
        next_revision = request.update.expected_revision + 1
        try:
            candidate = WorkingSetSnapshot(
                request.session_id,
                next_revision,
                request.update.sections,
            ).redacted(self._redaction_values)
        except (TypeError, ValueError) as error:
            raise SessionError("working set update is invalid") from error
        await self._validate_source_references(candidate)
        saved = await self._store.save_working_set(
            request.session_id,
            candidate,
            expected_revision=request.update.expected_revision,
        )
        self._validate_snapshot_scope(saved, request.session_id)
        if saved.revision != next_revision:
            raise SessionError("working set store returned an unexpected revision")
        return saved.redacted(self._redaction_values)

    @staticmethod
    def _validate_snapshot_scope(snapshot: WorkingSetSnapshot, session_id: str) -> None:
        if not isinstance(snapshot, WorkingSetSnapshot):
            raise SessionError("working set store returned an invalid snapshot")
        if snapshot.session_id != session_id:
            raise SessionError("working set snapshot belongs to another session")

    async def _validate_source_references(self, snapshot: WorkingSetSnapshot) -> None:
        references = tuple(
            entry.source_ref
            for state in snapshot.sections
            for entry in state.entries
            if entry.source_ref is not None
        )
        if not references:
            return
        items = await self._store.load_session_items(snapshot.session_id)
        for reference in references:
            assert isinstance(reference, SessionItemReference)
            reference.resolve(snapshot.session_id, items)


__all__ = ["SessionWorkingSetApplicationService"]
