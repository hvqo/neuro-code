"""Crash/ACK/lock/integrity tests: real SQLite, fault injection explicitly named."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    WorkflowStatus,
)
from neuro_code.infrastructure.persistence import sqlite_session_core
from neuro_code.infrastructure.persistence import sqlite_session_workflow_repair as persistence
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.persistence.sqlite_session_workflow_verification_lock import (
    verification_execution_lock,
)
from tests.architecture.test_workflow_repair_adapter import build, ready
from tests.architecture.test_workflow_verify_adapter import tick


async def fixture(tmp_path, **kwargs):
    return await ready(tmp_path, injected_verify=True, **kwargs)


async def test_fail_branch_consumes_repair_only_next_tick(tmp_path):
    store, root, session, key = await fixture(tmp_path, branch=True)
    adapter, provider, _ = build(store, root, session)
    terminal = (await adapter.run_once(key)).attempt
    assert terminal.state is State.COMPLETED
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.WAITING
    assert (await tick(store)).action == "consume_activity"
    assert len(provider.calls) == 2


async def test_rollback_after_real_write_recovery_never_redispatches(tmp_path):
    store, root, session, key = await fixture(tmp_path)
    adapter, provider, _ = build(store, root, session)
    with (
        patch.object(
            persistence, "_settle_terminal", side_effect=RuntimeError("injected settlement crash")
        ),
        pytest.raises(RuntimeError),
    ):
        await adapter.run_once(key)
    assert "assert True" in (root / "test_sample.py").read_text()
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_repair_evidence").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT count(*) FROM workflow_activity_results WHERE invocation_id=?", (key,)
        ).fetchone() == (0,)
    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    recovering, second_provider, _ = build(reopened, root, session)
    terminal = (await recovering.run_once(key)).attempt
    assert terminal.state is State.INDETERMINATE
    assert terminal.result.usage.model_calls is None
    assert terminal.result.usage.tool_calls is None
    assert terminal.result.usage.wall_milliseconds is None
    assert (await reopened.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION
    assert len(provider.calls) == 2
    assert not second_provider.calls
    assert (await recovering.run_once(key)).attempt == terminal


async def test_terminal_ack_loss_is_exact_replay(tmp_path):
    store, root, session, key = await fixture(tmp_path)
    adapter, provider, _ = build(store, root, session)
    finish = store._repair_finish

    async def lost_ack(*args, **kwargs):
        await finish(*args, **kwargs)
        raise RuntimeError("injected outer ACK loss")

    with patch.object(store, "_repair_finish", side_effect=lost_ack), pytest.raises(RuntimeError):
        await adapter.run_once(key)
    before = await store.get_workflow_run("run")
    result = (await adapter.run_once(key)).attempt
    assert result.state is State.COMPLETED
    assert result.result.usage.tool_calls == 1
    assert await store.get_workflow_run("run") == before
    assert len(provider.calls) == 2


async def test_abandoned_claim_zero_execution_unknown_time(tmp_path):
    store, root, session, key = await fixture(tmp_path)
    claimed = await store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="lost-owner",
        reserved=BudgetAmounts(model_calls=2, tool_calls=2, wall_milliseconds=100),
        updated_at=datetime.now(UTC),
    )
    adapter, provider, _ = build(store, root, session)
    result = (await adapter.run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.revision == claimed.revision + 1
    assert result.result.usage.model_calls == 0
    assert result.result.usage.tool_calls == 0
    assert result.result.usage.wall_milliseconds is None
    assert not provider.calls


async def test_distinct_invocation_same_workspace_lock(tmp_path):
    store, root, session, key = await fixture(tmp_path)
    adapter, provider, _ = build(store, root, session)
    with verification_execution_lock(
        store.database_path, "repair-workspace:" + str(root.resolve())
    ):
        assert (await adapter.run_once(key)).disposition == "busy"
    assert not provider.calls
    assert (await adapter.run_once(key)).attempt.state is State.COMPLETED


@pytest.mark.parametrize(
    "field", ["usage", "runtime_status", "workspace_after", "execution_fingerprint"]
)
async def test_rehashed_local_terminal_group_cannot_replace_journal(tmp_path, field):
    store, root, session, key = await fixture(tmp_path)
    adapter, _, _ = build(store, root, session)
    await adapter.run_once(key)
    with closing(store._connect()) as connection, connection:
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND (tbl_name LIKE 'workflow_repair_%' OR tbl_name LIKE 'workflow_activity_%')"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{row[0]}"')  # Attack copy/test DB only.
        _execution_id, raw = connection.execute(
            "SELECT execution_id,payload_json FROM workflow_repair_evidence"
        ).fetchone()
        evidence = json.loads(raw)
        if field == "usage":
            evidence[field]["tool_calls"] = 0
        elif field == "workspace_after":
            evidence[field]["workspace"]["content_fingerprint"] = "b" * 64
        elif field == "execution_fingerprint":
            evidence[field] = "b" * 64
        else:
            evidence[field] = "failed"
        connection.execute(
            "UPDATE workflow_repair_evidence SET payload_json=?,payload_fingerprint=?",
            (canonical(evidence), digest(evidence)),
        )
        result = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_activity_results WHERE invocation_id=?", (key,)
            ).fetchone()[0]
        )
        result["source_fingerprint"] = digest(evidence)
        if field == "usage":
            result["usage"] = evidence["usage"]
        result_fp = digest(result)
        connection.execute(
            "UPDATE workflow_activity_results SET payload_json=?,payload_fingerprint=? WHERE invocation_id=?",
            (canonical(result), result_fp, key),
        )
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM workflow_activity_attempts WHERE invocation_id=?", (key,)
            ).fetchone()[0]
        )
        snapshot["result"] = result
        connection.execute(
            "UPDATE workflow_activity_attempts SET snapshot_json=?,snapshot_fingerprint=? WHERE invocation_id=?",
            (canonical(snapshot), digest(snapshot), key),
        )
        local = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_activity_events WHERE invocation_id=? AND revision=3",
                (key,),
            ).fetchone()[0]
        )
        local["snapshot_fingerprint"] = digest(snapshot)
        connection.execute(
            "UPDATE workflow_activity_events SET payload_json=?,payload_fingerprint=? WHERE invocation_id=? AND revision=3",
            (canonical(local), digest(local), key),
        )
    with pytest.raises(WorkflowStateError):
        await store.get_workflow_activity(key)


@pytest.mark.parametrize("binding", ["session", "root"])
async def test_wrong_terminal_binding_refused(tmp_path, binding):
    store, root, session, key = await fixture(tmp_path)
    adapter, _, _ = build(store, root, session)
    terminal = (await adapter.run_once(key)).attempt
    with pytest.raises(WorkflowStateError):
        await store.reconcile_workflow_repair(
            key,
            parent_session_id="wrong" if binding == "session" else session,
            parent_workspace_root=root / "wrong" if binding == "root" else root,
            updated_at=terminal.updated_at,
        )


def downgrade(store):
    with closing(store._connect()) as connection, connection:
        for table in (
            "workflow_repair_evidence",
            "workflow_repair_events",
            "workflow_repair_executions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET version=42")


async def test_v42_migration_no_invented_execution(tmp_path):
    store, _root, _session, key = await fixture(tmp_path)
    original = await store.get_workflow_run("run")
    downgrade(store)
    await store.initialize()
    await store.initialize()
    assert await store.get_workflow_run("run") == original
    assert (await store.get_workflow_activity(key)).state is State.READY
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (43,)
        assert connection.execute("SELECT count(*) FROM workflow_repair_executions").fetchone() == (
            0,
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


async def test_v42_migration_ddl_failure_rolls_back(tmp_path):
    store, _root, _session, _key = await fixture(tmp_path)
    downgrade(store)
    original = sqlite_session_core._ensure_workflow_repair_schema

    def crash(connection):
        original(connection)
        raise RuntimeError("injected migration crash")

    with (
        patch.object(sqlite_session_core, "_ensure_workflow_repair_schema", side_effect=crash),
        pytest.raises(RuntimeError),
    ):
        await store.initialize()
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (42,)
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='workflow_repair_executions'"
        ).fetchone()


async def test_real_independent_process_death_recovery_no_second_write(tmp_path):
    store, root, session, key = await fixture(tmp_path)
    flag = tmp_path / "entered-second-model"
    script = """
import asyncio, sys
from pathlib import Path
from tests.architecture.test_workflow_repair_adapter import build
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
async def main():
    store=SqliteSessionStore(Path(sys.argv[1]))
    adapter, provider, _ = build(store, Path(sys.argv[2]), sys.argv[3])
    original=provider.stream
    calls=0
    async def stream(*args, **kwargs):
        nonlocal calls
        calls+=1
        if calls == 2:
            Path(sys.argv[5]).write_text('active')
            await asyncio.Event().wait()
        async for event in original(*args, **kwargs): yield event
    provider.stream=stream
    await adapter.run_once(sys.argv[4])
asyncio.run(main())
"""
    child = await asyncio.to_thread(
        subprocess.Popen,
        [
            sys.executable,
            "-c",
            script,
            str(store.database_path),
            str(root),
            session,
            key,
            str(flag),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(1200):
            if flag.exists():
                break
            if child.poll() is not None:
                raise AssertionError(child.communicate()[1].decode())
            await asyncio.sleep(0.025)
        if not flag.exists():
            child.kill()
            raise AssertionError(child.communicate(timeout=5)[1].decode())
        recovering, provider, _ = build(store, root, session)
        assert (await recovering.run_once(key)).disposition == "busy"
        assert "assert True" in (root / "test_sample.py").read_text()
        child.kill()
        child.communicate(timeout=5)
        terminal = (await recovering.run_once(key)).attempt
        assert terminal.state is State.INDETERMINATE
        assert terminal.result.usage.tool_calls is None
        assert not provider.calls
        assert "assert True" in (root / "test_sample.py").read_text()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
