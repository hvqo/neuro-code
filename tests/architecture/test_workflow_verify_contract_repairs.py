"""Three P1 attacks: real VERIFY provenance, independent anchors, last entry guard."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from neuro_code.application.permissions.contracts import PermissionApproval
from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.tools import ToolExecutionResult
from neuro_code.domain.workflows.activity import WorkflowActivityResult
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.interpreter import OutputKind, WorkflowStepOutput
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    WorkflowChange,
    WorkflowEventKind,
    WorkflowStatus,
)
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity import _settle_terminal
from neuro_code.infrastructure.tools.bash import BashTool
from neuro_code.infrastructure.tools.registry import ToolRegistry
from tests.architecture.test_workflow_verify_adapter import (
    ControlledCommand,
    adapter,
    executor,
    ready,
    tick,
)
from tests.fakes import EmptyWorkspaceChangeObserver


def durable_rows(store):
    with closing(store._connect()) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in (
                "workflow_runs",
                "workflow_activity_attempts",
                "workflow_activity_results",
                "workflow_activity_events",
                "workflow_transition_journal",
                "workflow_step_outputs",
                "workflow_verification_executions",
                "workflow_verification_evidence",
            )
        )


async def started_generic(store, key):
    a = await store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="untrusted",
        reserved=BudgetAmounts(),
        updated_at=datetime.now(UTC),
    )
    return await store.start_workflow_activity(
        key,
        expected_revision=a.revision,
        owner_id=a.owner_id,
        owner_fence=a.owner_fence,
        updated_at=datetime.now(UTC),
    )


def forged_result(attempt, status, source):
    return WorkflowActivityResult(
        attempt.invocation.invocation_id,
        attempt.invocation.request_fingerprint,
        attempt.invocation.activity,
        State.COMPLETED,
        source,
        digest("caller"),
        BudgetAmounts(),
        datetime.now(UTC),
        canonical({"status": status, "workspace_generation": 0}),
    )


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
@pytest.mark.parametrize("source", ["caller-created", "verify-exec-forged"])
async def test_generic_verify_finish_rejected_without_any_writes(tmp_path, status, source):
    store, _root, _session, key = await ready(tmp_path)
    attempt = await started_generic(store, key)
    before = durable_rows(store)
    with pytest.raises(WorkflowStateError, match="VERIFY"):
        await store.finish_workflow_activity(
            forged_result(attempt, status, source),
            expected_revision=attempt.revision,
            owner_id=attempt.owner_id,
            owner_fence=attempt.owner_fence,
        )
    assert durable_rows(store) == before


@pytest.mark.parametrize("kind", [OutputKind.ACTIVITY, OutputKind.FAKE_ACTIVITY])
async def test_direct_verify_output_without_proof_or_scope_is_rejected(tmp_path, kind):
    store, _root, _session, key = await ready(tmp_path)
    a = await started_generic(store, key)
    result = forged_result(a, "PASS", "caller-created")
    output = WorkflowStepOutput(
        a.invocation.run_id,
        a.invocation.step,
        a.invocation.input_fingerprint,
        kind,
        key,
        result.fingerprint,
        result.output_json,
    )
    run = await store.get_workflow_run(a.invocation.run_id)
    before = durable_rows(store)
    with pytest.raises(WorkflowStateError):
        await store.commit_workflow_step_output(
            output,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=datetime.now(UTC),
        )
    assert durable_rows(store) == before


async def test_new_verify_terminal_without_evidence_cannot_even_be_read(tmp_path):
    store, root, session, key = await ready(tmp_path)
    a = await started_generic(store, key)
    with closing(store._connect()) as connection, connection:
        _settle_terminal(connection, a, forged_result(a, "PASS", "caller-created"))
    before = durable_rows(store)
    with pytest.raises(WorkflowStateError, match="evidence missing"):
        await store.get_workflow_activity(key)
    with pytest.raises(WorkflowStateError):
        await tick(store, adapter(store, root, session))
    assert durable_rows(store) == before


async def test_v41_legacy_terminal_readonly_not_new_consumption(tmp_path):
    store, root, session, key = await ready(tmp_path)
    a = await started_generic(store, key)
    with closing(store._connect()) as connection, connection:
        connection.execute("DROP TRIGGER workflow_transition_journal_immutable")
        row = connection.execute(
            "SELECT generation,payload_json FROM workflow_transition_journal WHERE run_id=? AND request_id=?",
            (a.invocation.run_id, "activity:" + key),
        ).fetchone()
        payload = json.loads(row[1])
        payload.pop("verification_protocol")
        connection.execute(
            "UPDATE workflow_transition_journal SET payload_json=?,payload_fingerprint=? WHERE run_id=? AND generation=?",
            (canonical(payload), digest(payload), a.invocation.run_id, row[0]),
        )
        terminal = _settle_terminal(connection, a, forged_result(a, "fake", "legacy-result"))
        connection.execute("DROP TABLE workflow_verification_evidence")
        connection.execute("DROP TABLE workflow_verification_executions")
        connection.execute("UPDATE schema_meta SET version=41")
    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    assert await reopened.get_workflow_activity(key) == terminal
    assert (
        await reopened.finish_workflow_activity(
            terminal.result, expected_revision=0, owner_id="gone", owner_fence=0
        )
        == terminal
    )
    before = durable_rows(reopened)
    with pytest.raises(WorkflowStateError):
        await tick(reopened, adapter(reopened, root, session))
    assert durable_rows(reopened) == before


def rehash_local_group(store, mutation):
    """Attack only the local proof group; never touch the independent Run journal."""
    tables = [
        "workflow_verification_evidence",
        "workflow_activity_results",
        "workflow_activity_attempts",
        "workflow_activity_events",
    ]
    if mutation in {"command", "workspace", "source"}:
        tables.append("workflow_verification_executions")
    with closing(store._connect()) as connection, connection:
        for table in tables:
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
            ).fetchall():
                connection.execute("DROP TRIGGER " + name)
        eid, key, payload, _fp = connection.execute(
            "SELECT * FROM workflow_verification_executions"
        ).fetchone()
        execution = json.loads(payload)
        evidence = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_verification_evidence"
            ).fetchone()[0]
        )
        result = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_activity_results WHERE invocation_id=?", (key,)
            ).fetchone()[0]
        )
        if mutation == "fail_to_pass":
            evidence["exit_code"], evidence["outcome"] = 0, "PASS"
            evidence["observation"]["is_error"] = False
            out = json.loads(evidence["output_json"])
            out["status"] = "PASS"
            evidence["output_json"] = canonical(out)
        elif mutation == "exit":
            evidence["exit_code"] = 0
        elif mutation == "status":
            out = json.loads(evidence["output_json"])
            out["status"] = "PASS"
            evidence["output_json"] = canonical(out)
        elif mutation == "command":
            execution["configuration"]["command"] = "pytest -q"
        elif mutation == "workspace":
            execution["workspace_before"]["content_fingerprint"] = "b" * 64
            evidence["workspace_after"] = execution["workspace_before"]
        elif mutation == "source":
            execution["execution_id"] = "verify-exec-forged"
        if mutation in {"command", "workspace", "source"}:
            connection.execute("PRAGMA defer_foreign_keys=ON")
            connection.execute(
                "UPDATE workflow_verification_executions SET execution_id=?,payload_json=?,payload_fingerprint=? WHERE execution_id=?",
                (execution["execution_id"], canonical(execution), digest(execution), eid),
            )
            connection.execute(
                "UPDATE workflow_verification_evidence SET execution_id=? WHERE execution_id=?",
                (execution["execution_id"], eid),
            )
            evidence["execution_fingerprint"] = digest(execution)
        connection.execute(
            "UPDATE workflow_verification_evidence SET payload_json=?,payload_fingerprint=?",
            (canonical(evidence), digest(evidence)),
        )
        result["output_json"], result["source_fingerprint"], result["source_id"] = (
            evidence["output_json"],
            digest(evidence),
            execution["execution_id"],
        )
        connection.execute(
            "UPDATE workflow_activity_results SET payload_json=?,payload_fingerprint=? WHERE invocation_id=?",
            (canonical(result), digest(result), key),
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
        event = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_activity_events WHERE invocation_id=? AND revision=?",
                (key, snapshot["revision"]),
            ).fetchone()[0]
        )
        event["snapshot_fingerprint"] = digest(snapshot)
        connection.execute(
            "UPDATE workflow_activity_events SET payload_json=?,payload_fingerprint=? WHERE invocation_id=? AND revision=?",
            (canonical(event), digest(event), key, snapshot["revision"]),
        )


@pytest.mark.parametrize(
    "mutation", ["fail_to_pass", "exit", "status", "command", "workspace", "source"]
)
async def test_real_pytest_fail_group_rehash_rejected_after_copy_reopen(tmp_path, mutation):
    store, root, session, key = await ready(tmp_path, "def test_fail():\n    assert False\n")
    terminal = (await adapter(store, root, session).run_once(key)).attempt
    assert json.loads(terminal.result.output_json)["status"] == "FAIL"
    copied = tmp_path / "attack.db"
    with closing(store._connect()) as original, closing(sqlite3.connect(copied)) as target:
        original.backup(target)
    attack = SqliteSessionStore(copied)
    original_journal = durable_rows(attack)[4]
    rehash_local_group(attack, mutation)
    assert durable_rows(attack)[4] == original_journal
    reopened = SqliteSessionStore(copied)
    await reopened.initialize()
    with pytest.raises(WorkflowStateError):
        await reopened.get_workflow_activity(key)
    with pytest.raises(WorkflowStateError):
        await tick(reopened, adapter(reopened, root, session))
    assert await store.get_workflow_activity(key) == terminal


@pytest.mark.parametrize("change", ["missing", "conflict", "fingerprint"])
async def test_terminal_independent_journal_missing_or_conflicting(tmp_path, change):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    command.release.set()
    await adapter(store, root, session, command=command).run_once(key)
    with closing(store._connect()) as connection, connection:
        connection.execute("DROP TRIGGER workflow_transition_journal_immutable")
        if change == "missing":
            connection.execute(
                "DELETE FROM workflow_transition_journal WHERE request_id=?",
                ("activity-settle:" + key,),
            )
        elif change == "fingerprint":
            connection.execute(
                "UPDATE workflow_transition_journal SET payload_fingerprint=? WHERE request_id=?",
                ("0" * 64, "activity-settle:" + key),
            )
        else:
            row = connection.execute(
                "SELECT payload_json FROM workflow_transition_journal WHERE request_id=?",
                ("activity-settle:" + key,),
            ).fetchone()
            data = json.loads(row[0])
            data["verification"]["exit_code"] = 1
            connection.execute(
                "UPDATE workflow_transition_journal SET payload_json=?,payload_fingerprint=? WHERE request_id=?",
                (canonical(data), digest(data), "activity-settle:" + key),
            )
    with pytest.raises(WorkflowStateError, match="journal proof"):
        await store.get_workflow_activity(key)


async def cancel_run(store):
    run = await store.get_workflow_run("run")
    await store.transition_workflow_run(
        "run",
        WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="cancel-before-dispatch",
        updated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("point", ["approval", "preflight", "hook", "owner"])
async def test_real_final_tool_guard_zero_spawn_after_late_cancel_or_reclaim(tmp_path, point):
    store, root, session, key = await ready(tmp_path)
    bash = BashTool()
    underlying = bash._local_process_sandbox
    spawns = []

    class RecordingLauncher:
        async def spawn(self, request):
            spawns.append(request)
            return await underlying.spawn(request)

    bound = executor(
        root,
        context=ToolContext(root, local_process_sandbox=RecordingLauncher()),
        tools=ToolRegistry([bash]),
    )

    class Approver:
        async def request(self, request):
            await cancel_run(store)
            return PermissionApproval.allow_once()

    class Hook:
        async def before_tool(self, *args, **kwargs):
            if point == "owner":
                run = await store.get_workflow_run("run")
                await store.claim_workflow_run(
                    "run",
                    expected_generation=run.generation,
                    expected_owner_fence=run.owner_fence,
                    owner_id="another-owner",
                    request_id="reclaim",
                    updated_at=datetime.now(UTC),
                )
            else:
                await cancel_run(store)

        async def after_tool(self, result):
            pass

    if point == "approval":
        bound._permissions = PermissionManager(mode=PermissionMode.DEFAULT, interactive=True)
        bound._approver = Approver()
    elif point == "preflight":
        loop = asyncio.get_running_loop()

        class Observer(EmptyWorkspaceChangeObserver):
            def capture(self, root):
                asyncio.run_coroutine_threadsafe(cancel_run(store), loop).result(10)
                return super().capture(root)

        bound._workspace_change_observer = Observer()
    else:
        bound._hooks = (Hook(),)
    outcome = (await adapter(store, root, session, command=bound).run_once(key)).attempt
    assert spawns == []
    assert outcome.state is State.BLOCKED
    assert outcome.result.output_json is None
    assert outcome.result.usage.tool_calls == 0
    assert (await store.get_workflow_run("run")).status is (
        WorkflowStatus.NEEDS_ATTENTION if point == "owner" else WorkflowStatus.CANCELLED
    )
    assert await store.get_workflow_activity(key) == outcome


async def test_already_cancelled_run_never_claims_or_spawns(tmp_path):
    store, root, session, key = await ready(tmp_path)
    await cancel_run(store)
    before = durable_rows(store)
    command = ControlledCommand(root)
    with pytest.raises(WorkflowStateError):
        await adapter(store, root, session, command=command).run_once(key)
    assert command.calls == 0
    assert durable_rows(store) == before


async def test_atomic_terminal_anchor_rollback_then_unknown_recovery(tmp_path):
    from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity import _append_event

    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    command.release.set()

    def append_then_crash(connection, run, request_id, kind, payload):
        _append_event(connection, run, request_id, kind, payload)
        if request_id == "activity-settle:" + key:
            raise RuntimeError("anchor rollback")

    with (
        patch(
            "neuro_code.infrastructure.persistence.sqlite_session_workflow_activity._append_event",
            side_effect=append_then_crash,
        ),
        pytest.raises(RuntimeError, match="anchor rollback"),
    ):
        await adapter(store, root, session, command=command).run_once(key)
    active = await store.get_workflow_activity(key)
    assert active.state is State.RUNNING
    assert active.result is None
    with closing(store._connect()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_verification_evidence"
        ).fetchone() == (0,)
        assert not connection.execute(
            "SELECT 1 FROM workflow_transition_journal WHERE request_id=?",
            ("activity-settle:" + key,),
        ).fetchone()
    run = await store.get_workflow_run("run")
    assert (
        next(
            r for r in run.ledger.reservations if r.reservation_id == active.reservation_id
        ).consumed
        is None
    )
    recovered = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert recovered.state is State.INDETERMINATE
    assert recovered.result.usage.tool_calls is None
    assert command.calls == 1
    assert await store.get_workflow_activity(key) == recovered


async def test_real_execution_missing_terminal_evidence_cannot_be_consumed(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    command.release.set()
    await adapter(store, root, session, command=command).run_once(key)
    with closing(store._connect()) as connection, connection:
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='workflow_verification_evidence'"
        ).fetchall():
            connection.execute("DROP TRIGGER " + name)
        connection.execute("DELETE FROM workflow_verification_evidence")
    before = durable_rows(store)
    with pytest.raises(WorkflowStateError, match="evidence missing"):
        await store.get_workflow_activity(key)
    with pytest.raises(WorkflowStateError):
        await tick(store, adapter(store, root, session))
    assert durable_rows(store) == before


@pytest.mark.parametrize(("code", "is_error"), [(0, True), (1, False)])
async def test_inconsistent_exit_observation_is_never_pass_or_fail(tmp_path, code, is_error):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(
        root,
        result=ToolExecutionResult(
            "call",
            "bash",
            "inconsistent observation",
            is_error=is_error,
            metadata={"exit_code": code},
        ),
    )
    command.release.set()
    result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.output_json is None
    assert await store.get_workflow_activity(key) == result


async def test_real_started_command_run_cancel_waits_for_descendant_cleanup(tmp_path):
    started, survived = tmp_path / "child-started", tmp_path / "child-survived"
    child = f"from pathlib import Path; import time; Path({str(started)!r}).write_text('started'); time.sleep(4); Path({str(survived)!r}).write_text('leaked')"
    text = f"import subprocess, sys, time\ndef test_tree():\n    subprocess.Popen([sys.executable, '-c', {child!r}])\n    time.sleep(20)\n"
    store, root, session, key = await ready(tmp_path, text)
    verifier = adapter(store, root, session)
    task = asyncio.create_task(verifier.run_once(key))
    try:
        async with asyncio.timeout(10):
            for _ in range(200):
                if started.exists():
                    break
                await asyncio.sleep(0.05)
            assert started.exists()
        await cancel_run(store)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    await asyncio.sleep(4.2)
    assert not survived.exists()
    recovered = (await verifier.run_once(key)).attempt
    assert recovered.state is State.INDETERMINATE
    assert recovered.result.output_json is None
    assert recovered.result.usage.tool_calls is None
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.CANCELLED
