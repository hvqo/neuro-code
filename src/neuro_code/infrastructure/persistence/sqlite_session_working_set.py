"""SQLite persistence owner for one current Working Set snapshot per session.

The row is intentionally separate from ``sessions.messages_json``.  It stores
only the derived task-state projection and uses a transactional revision check
for complete snapshot replacement.

每个会话一个当前 Working Set snapshot 的 SQLite 持久化 owner.

该行有意独立于 ``sessions.messages_json``;它只保存派生任务状态 projection,并通过事务
revision 检查完成整个 snapshot 的替换.
"""

from __future__ import annotations

import json
from contextlib import closing

from neuro_code.application.ports.working_set import WorkingSetSnapshot
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SessionError


def _working_set_from_row(
    row: tuple[object, object],
    *,
    session_id: str,
) -> WorkingSetSnapshot:
    try:
        revision = row[0]
        raw_snapshot = row[1]
        if not isinstance(raw_snapshot, str):
            raise ValueError("working set snapshot JSON must be text")
        snapshot = WorkingSetSnapshot.from_dict(json.loads(raw_snapshot))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SessionError(f"session {session_id} contains invalid working set state") from error
    if snapshot.session_id != session_id or snapshot.revision != revision:
        raise SessionError(f"session {session_id} contains invalid working set state")
    return snapshot


class WorkingSetMixin(_SqliteSessionPersistenceContext):
    """Own the durable current-snapshot and revision-CAS operations."""

    async def load_working_set(self, session_id: str) -> WorkingSetSnapshot | None:
        def load() -> WorkingSetSnapshot | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT revision, snapshot_json
                    FROM session_working_sets
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    exists = connection.execute(
                        "SELECT 1 FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    if exists is None:
                        raise SessionError(f"unknown session: {session_id}")
                    return None
                return _working_set_from_row(row, session_id=session_id)

        return await run_blocking(load)

    async def save_working_set(
        self,
        session_id: str,
        snapshot: WorkingSetSnapshot,
        *,
        expected_revision: int,
    ) -> WorkingSetSnapshot:
        if not isinstance(snapshot, WorkingSetSnapshot):
            raise TypeError("working set snapshot must be canonical")
        if snapshot.session_id != session_id:
            raise SessionError("working set snapshot belongs to another session")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or snapshot.revision != expected_revision + 1
        ):
            raise SessionError("working set revision is invalid")
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, separators=(",", ":"))

        def save() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise SessionError(f"unknown session: {session_id}")
                row = connection.execute(
                    """
                    SELECT revision, snapshot_json
                    FROM session_working_sets
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    if expected_revision != 0:
                        raise SessionError("working set revision is stale")
                    connection.execute(
                        """
                        INSERT INTO session_working_sets(session_id, revision, snapshot_json)
                        VALUES (?, ?, ?)
                        """,
                        (session_id, snapshot.revision, payload),
                    )
                else:
                    current = _working_set_from_row(row, session_id=session_id)
                    if current.revision != expected_revision:
                        raise SessionError("working set revision is stale")
                    cursor = connection.execute(
                        """
                        UPDATE session_working_sets
                        SET revision = ?, snapshot_json = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE session_id = ? AND revision = ?
                        """,
                        (snapshot.revision, payload, session_id, expected_revision),
                    )
                    if cursor.rowcount != 1:
                        raise SessionError("working set revision is stale")
                connection.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

        async with self._write_lock:
            await run_blocking(save)
        return snapshot


__all__ = ["WorkingSetMixin"]
