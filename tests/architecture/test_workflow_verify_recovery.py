"""Independent-process arbitration, real SQLite migration and cancellation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.state import WorkflowChange, WorkflowEventKind, WorkflowStatus
from neuro_code.infrastructure.persistence import sqlite_session_core
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from tests.architecture.test_workflow_verify_adapter import (
    ControlledCommand,
    adapter,
    ready,
    tick,
)

PROCESS = """
import asyncio, sys
from pathlib import Path
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from tests.architecture.test_workflow_verify_adapter import adapter, ControlledCommand
async def main():
    database, root, session, key = sys.argv[1:]
    store = SqliteSessionStore(Path(database))
    command = ControlledCommand(Path(root))
    task = asyncio.create_task(adapter(store, Path(root), session, command=command).run_once(key))
    await command.entered.wait()
    print('RUNNING', flush=True)
    await task
asyncio.run(main())
"""


async def test_independent_process_alive_busy_then_death_unknown(tmp_path):
    store, root, session, key = await ready(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        PROCESS,
        str(store.database_path),
        str(root),
        session,
        key,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 20)
        assert line == b"RUNNING\n"
        before = await store.get_workflow_run("run")
        with pytest.raises(WorkflowStateError, match="active"):
            await store.reconcile_workflow_verification(
                key,
                parent_session_id=session,
                parent_workspace_root=root,
                updated_at=datetime.now(UTC),
            )
        assert await store.get_workflow_run("run") == before
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
    command = ControlledCommand(root)
    result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.usage.tool_calls is None
    assert command.calls == 0
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_cancelled_run_no_new_dispatch_and_late_accounting(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    task = asyncio.create_task(adapter(store, root, session, command=command).run_once(key))
    await asyncio.wait_for(command.entered.wait(), 10)
    run = await store.get_workflow_run("run")
    await store.transition_workflow_run(
        "run",
        WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="cancel",
        updated_at=datetime.now(UTC),
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert command.calls == 1
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.CANCELLED


def downgrade_fixture(store):
    """Retain real v41 rows and remove only the new v42 evidence schema."""
    with closing(store._connect()) as connection, connection:
        connection.execute("DROP TABLE workflow_verification_evidence")
        connection.execute("DROP TABLE workflow_verification_executions")
        connection.execute("UPDATE schema_meta SET version=41")


async def test_v41_upgrade_idempotent_no_invented_evidence(tmp_path):
    store, root, session, key = await ready(tmp_path)
    original = await store.get_workflow_run("run")
    downgrade_fixture(store)
    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    await reopened.initialize()
    assert await reopened.get_workflow_run("run") == original
    with closing(reopened._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (42,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_verification_executions"
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    result = (await adapter(reopened, root, session).run_once(key)).attempt
    assert result.state is State.COMPLETED
    assert (await tick(reopened, adapter(reopened, root, session))).action == "consume_activity"


async def test_migration_failure_rolls_back_ddl_and_version(tmp_path):
    store, _root, _session, _key = await ready(tmp_path)
    downgrade_fixture(store)
    original = sqlite_session_core._ensure_workflow_verification_schema

    def fail(connection):
        original(connection)
        raise RuntimeError("migration interrupted")

    with (
        patch.object(sqlite_session_core, "_ensure_workflow_verification_schema", side_effect=fail),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        await SqliteSessionStore(store.database_path).initialize()
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (41,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='workflow_verification_executions'"
            ).fetchall()
            == []
        )
    await SqliteSessionStore(store.database_path).initialize()


@pytest.mark.parametrize(
    "table", ["workflow_verification_executions", "workflow_verification_evidence"]
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_evidence_immutable_database_boundary(tmp_path, table, operation):
    store, root, session, key = await ready(tmp_path)
    await adapter(store, root, session).run_once(key)
    with (
        closing(store._connect()) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute(
            f"{operation} {'FROM ' if operation == 'DELETE' else ''}{table}"
            + (" SET payload_fingerprint='" + "0" * 64 + "'" if operation == "UPDATE" else "")
        )


async def test_final_evidence_can_replay_after_parent_head_changes(tmp_path):
    from tests.architecture.test_workflow_verify_adapter import git

    store, root, session, key = await ready(tmp_path)
    verifier = adapter(store, root, session)
    result = (await verifier.run_once(key)).attempt
    (root / "new.txt").write_text("new revision")
    git(root, "add", ".")
    git(root, "commit", "-m", "new parent head")
    assert (await verifier.run_once(key)).attempt == result
    with pytest.raises(WorkflowStateError, match="stale"):
        await tick(store, verifier)


async def test_command_and_workspace_rehashed_tamper_pinned_by_result(tmp_path):
    from neuro_code.domain.workflows.publication import canonical, digest

    store, root, session, key = await ready(tmp_path)
    await adapter(store, root, session).run_once(key)
    with closing(store._connect()) as connection, connection:
        connection.execute("DROP TRIGGER workflow_verification_executions_immutable_update")
        exec_id, _key, payload, _fp = connection.execute(
            "SELECT * FROM workflow_verification_executions"
        ).fetchone()
        data = json.loads(payload)
        data["configuration"]["command"] = "pytest -q"
        connection.execute(
            "UPDATE workflow_verification_executions SET payload_json=?,payload_fingerprint=? WHERE execution_id=?",
            (canonical(data), digest(data), exec_id),
        )
    with pytest.raises(WorkflowStateError, match="proof"):
        await store.get_workflow_activity(key)
