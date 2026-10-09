"""Trusted live ADOPT measurement versus uncertain durable recovery."""

import asyncio
import json
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import pytest

from neuro_code.application.ports.result_adoption import ResultAdoptionError
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import BudgetAmounts, WorkflowStatus
from neuro_code.infrastructure.persistence import sqlite_session_workflow_adoption_meter as meter
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from tests.architecture.test_workflow_adopt_adapter import adapter, ready
from tests.architecture.test_workflow_adoption import END, service
from tests.architecture.test_workflow_interpreter import engine
from tests.test_result_adoption import _RecordingMutation


@pytest.mark.parametrize("no_changes", [False, True])
async def test_trusted_success_consumes_without_manual_accounting(tmp_path, no_changes):
    f, key = await ready(tmp_path, no_changes=no_changes)

    class DiskMutation(_RecordingMutation):
        async def apply(self, request, *, session_id):
            assert (await f.store.get_workflow_activity(key)).state is State.RUNNING
            result = await super().apply(request, session_id=session_id)
            path = f.binding.workspace_root / request.path
            if request.desired is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(request.desired.content)
            return result

    port = DiskMutation(f.parent)
    result = await adapter(f, mutation=port).run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert result.attempt.result.usage.tool_calls == len(port.calls) == (0 if no_changes else 2)
    assert result.attempt.result.usage.wall_milliseconds > 0
    assert result.attempt.result.usage.known
    with closing(f.store._connect()) as c:
        start = c.execute("SELECT payload_json FROM workflow_adoption_executions").fetchone()
        end = c.execute("SELECT payload_json FROM workflow_adoption_measurements").fetchone()
        assert json.loads(end[0])["execution_fingerprint"] == digest(json.loads(start[0]))
        assert json.loads(end[0])["elapsed_ns"] > 0
    for request in port.calls:
        path = f.binding.workspace_root / request.path
        assert (path.read_bytes() if path.exists() else None) == (
            request.desired.content if request.desired else None
        )
    reopened = SqliteSessionStore(f.store.database_path)
    assert await reopened.get_workflow_activity(key) == result.attempt
    run = await reopened.get_workflow_run("workflow-run")
    assert run.status is WorkflowStatus.WAITING
    outcome = await engine(reopened).advance_once(
        run.run_id,
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=END + timedelta(seconds=2),
    )
    assert outcome.action == "consume_activity"
    assert json.loads(result.attempt.result.output_json)["parent_workspace_changed"] is (
        not no_changes
    )


@pytest.mark.parametrize("after_write", [False, True])
async def test_actual_port_exception_is_counted(tmp_path, after_write):
    f, key = await ready(tmp_path)

    class RaisingPort(_RecordingMutation):
        async def apply(self, request, *, session_id):
            if after_write:
                await super().apply(request, session_id=session_id)
            else:
                self.calls.append(request)
            raise ResultAdoptionError("permission denied inside port", kind="permission_denied")

    port = RaisingPort(f.parent)
    result = await adapter(f, mutation=port).run_once(key)
    assert result.attempt.state is (State.COMPLETED if after_write else State.FAILED)
    assert result.attempt.result.usage.tool_calls == len(port.calls) == (2 if after_write else 1)
    assert result.attempt.result.usage.known
    with closing(f.store._connect()) as c:
        assert c.execute(
            "SELECT COUNT(*) FROM workflow_adoption_dispatch_events WHERE phase='raised'"
        ).fetchone()[0] == len(port.calls)


@pytest.mark.parametrize("window", ["intent", "port_ack", "partial_ack", "terminal_before_meter"])
async def test_incomplete_scope_never_infers_known_usage(tmp_path, window):
    f, key = await ready(tmp_path)
    real_write = meter._ExecutionMeter._write
    real_ack = meter._ExecutionMeter._ack

    async def crash_write(self, operation):
        result = await real_write(self, operation)
        if window == "intent" and operation.__name__ == "intent":
            raise asyncio.CancelledError("intent ACK lost, entry unknown")
        return result

    async def crash_ack(self, ordinal, phase, value):
        if window == "port_ack" or (window == "partial_ack" and ordinal == 2):
            raise asyncio.CancelledError("port executed, measurement ACK lost")
        await real_ack(self, ordinal, phase, value)

    with (
        patch.object(meter._ExecutionMeter, "_write", crash_write),
        patch.object(meter._ExecutionMeter, "_ack", crash_ack),
        patch.object(
            meter._ExecutionMeter,
            "complete",
            side_effect=asyncio.CancelledError("terminal, before measurement"),
        )
        if window == "terminal_before_meter"
        else patch.object(meter, "uuid", meter.uuid),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter(f).run_once(key)
    calls = len(f.mutation.calls)
    reopened = SqliteSessionStore(f.store.database_path)
    with patch(
        "neuro_code.application.workflows.result_adoption.owner_is_alive", return_value=False
    ):
        result = await adapter(f, store=reopened).run_once(key)
    # No successful path is inferred from an intent or a desired-file image.
    if result.attempt.result:
        assert result.attempt.result.usage.tool_calls is None
        assert result.attempt.result.usage.wall_milliseconds is None
    assert (
        await reopened.get_workflow_run("workflow-run")
    ).status is WorkflowStatus.NEEDS_ATTENTION
    assert len({r.path for r in f.mutation.calls}) == len(f.mutation.calls)
    if window in {"partial_ack", "terminal_before_meter"}:
        assert len(f.mutation.calls) == calls == 2
    with closing(reopened._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_adoption_measurements").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["usage", "receipt", "wall_milliseconds", "tool_calls"])
async def test_execution_boundary_accepts_no_caller_measurement(tmp_path, field):
    f, key = await ready(tmp_path)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )

    async def dispatch(*args):
        raise AssertionError("must reject before execution")

    with pytest.raises(TypeError, match=field):
        await f.store.execute_workflow_adoption(
            key,
            expected_revision=claimed.revision,
            owner_id="owner",
            owner_fence=claimed.owner_fence,
            updated_at=END,
            mutation=f.mutation,
            dispatch=dispatch,
            **{field: BudgetAmounts()},
        )
    assert await f.store.get_workflow_activity(key) == claimed
    assert f.mutation.calls == []


@pytest.mark.parametrize(
    "kind", ["wrong_owner", "wrong_fence", "wrong_invocation", "historical_running"]
)
async def test_measurement_requires_fresh_exact_execution_identity(tmp_path, kind):
    f, key = await ready(tmp_path)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )
    if kind == "historical_running":
        await f.store.start_workflow_activity(
            key,
            expected_revision=1,
            owner_id="owner",
            owner_fence=claimed.owner_fence,
            updated_at=END,
        )
    store = SqliteSessionStore(f.store.database_path)

    async def dispatch(*args):
        raise AssertionError("no dispatch")

    with pytest.raises(WorkflowStateError):
        await store.execute_workflow_adoption(
            "wrong" if kind == "wrong_invocation" else key,
            expected_revision=1,
            owner_id="wrong" if kind == "wrong_owner" else "owner",
            owner_fence=claimed.owner_fence + (kind == "wrong_fence"),
            updated_at=END,
            mutation=f.mutation,
            dispatch=dispatch,
        )
    assert f.mutation.calls == []
    with closing(store._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_adoption_executions").fetchone()[0] == 0


async def test_historical_core_execution_without_meter_stays_unknown(tmp_path):
    f, key = await ready(tmp_path)
    request = await f.store.get_workflow_adoption_request(key)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="old-owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )
    await f.store.start_workflow_activity(
        key,
        expected_revision=1,
        owner_id="old-owner",
        owner_fence=claimed.owner_fence,
        updated_at=END,
    )
    await service(f).adopt(request)
    assert len(f.mutation.calls) == 2
    result = await adapter(f).run_once(key)
    assert result.attempt.result.usage.tool_calls is None
    assert result.attempt.result.usage.wall_milliseconds is None
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_unwrapped_dispatch_cannot_seal_zero_calls(tmp_path):
    f, key = await ready(tmp_path)
    request = await f.store.get_workflow_adoption_request(key)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )

    async def dishonest_callback(_attempt, _measured_port):
        # Even a callback bypassing its injected port cannot create a known zero receipt.
        await service(f).adopt(request)

    with pytest.raises(WorkflowStateError, match="unmeasured"):
        await f.store.execute_workflow_adoption(
            key,
            expected_revision=1,
            owner_id="owner",
            owner_fence=claimed.owner_fence,
            updated_at=END,
            mutation=f.mutation,
            dispatch=dishonest_callback,
        )
    assert len(f.mutation.calls) == 2
    result = await adapter(f).run_once(key)
    assert result.attempt.result.usage.tool_calls is None


@pytest.mark.parametrize(
    "table",
    [
        "workflow_adoption_executions",
        "workflow_adoption_dispatch_events",
        "workflow_adoption_measurements",
    ],
)
async def test_meter_facts_immutable_and_rehashed_tamper_rejected(tmp_path, table):
    f, key = await ready(tmp_path)
    await adapter(f).run_once(key)
    with closing(f.store._connect()) as c, c:
        with pytest.raises(Exception, match="immutable"):
            c.execute(f"UPDATE {table} SET payload_json='{{}}'")
        c.execute(f"DROP TRIGGER {table}_immutable_update")
        row = c.execute(f"SELECT rowid,payload_json FROM {table} LIMIT 1").fetchone()
        value = json.loads(row[1])
        if table == "workflow_adoption_executions":
            value["owner_fence"] += 1
        elif table == "workflow_adoption_dispatch_events":
            value["request_fingerprint"] = "f" * 64
        else:
            value["usage"]["tool_calls"] = 0
        c.execute(
            f"UPDATE {table} SET payload_json=?,payload_fingerprint=? WHERE rowid=?",
            (canonical(value), digest(value), row[0]),
        )
    with pytest.raises(WorkflowStateError):
        await SqliteSessionStore(f.store.database_path).get_workflow_activity(key)


async def test_v40_migration_preserves_activity_and_does_not_invent_measurement(tmp_path):
    f, key = await ready(tmp_path)
    before = await f.store.get_workflow_activity(key)
    with closing(f.store._connect()) as c, c:
        for table in (
            "workflow_adoption_measurements",
            "workflow_adoption_dispatch_events",
            "workflow_adoption_executions",
        ):
            c.execute(f"DROP TABLE {table}")
        c.execute("UPDATE schema_meta SET version=40")
    store = SqliteSessionStore(f.store.database_path)
    await store.initialize()
    assert await store.get_workflow_activity(key) == before
    with closing(store._connect()) as c:
        assert c.execute("SELECT version FROM schema_meta").fetchone()[0] == 42
        assert c.execute("SELECT COUNT(*) FROM workflow_adoption_executions").fetchone()[0] == 0


async def test_start_and_meter_identity_rollback_together(tmp_path):
    f, key = await ready(tmp_path)
    with closing(f.store._connect()) as c, c:
        c.execute(
            "CREATE TRIGGER fail_meter BEFORE INSERT ON workflow_adoption_executions BEGIN SELECT RAISE(ABORT,'meter start rollback'); END"
        )
    with pytest.raises(WorkflowStateError):
        await adapter(f).run_once(key)
    attempt = await f.store.get_workflow_activity(key)
    assert attempt.state is State.CLAIMED
    with closing(f.store._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_adoption_executions").fetchone()[0] == 0
        assert (
            c.execute("SELECT COUNT(*) FROM workflow_activity_events WHERE revision=2").fetchone()[
                0
            ]
            == 0
        )
    assert f.mutation.calls == []


async def test_completion_receipt_ack_loss_recovers_known_without_new_calls(tmp_path):
    f, key = await ready(tmp_path)
    real = meter._ExecutionMeter.complete

    async def lost_ack(self):
        await real(self)
        raise asyncio.CancelledError("measurement committed, ACK lost")

    with (
        patch.object(meter._ExecutionMeter, "complete", lost_ack),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter(f).run_once(key)
    assert len(f.mutation.calls) == 2
    store = SqliteSessionStore(f.store.database_path)
    result = await adapter(f, store=store).run_once(key)
    assert result.attempt.result.usage.tool_calls == 2
    assert result.attempt.result.usage.known
    assert (await store.get_workflow_run("workflow-run")).status is WorkflowStatus.WAITING
    before = await store.get_workflow_run("workflow-run")
    assert (await adapter(f, store=store).run_once(key)).attempt == result.attempt
    assert await store.get_workflow_run("workflow-run") == before
    assert len(f.mutation.calls) == 2


async def test_committed_call_ack_without_finished_scope_still_has_unknown_wall(tmp_path):
    f, key = await ready(tmp_path)
    real = meter._ExecutionMeter._ack

    async def lost_ack(self, ordinal, phase, value):
        await real(self, ordinal, phase, value)
        if ordinal == 2:
            raise asyncio.CancelledError("measurement partial commit, no completed scope")

    with (
        patch.object(meter._ExecutionMeter, "_ack", lost_ack),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter(f).run_once(key)
    store = SqliteSessionStore(f.store.database_path)
    with patch(
        "neuro_code.application.workflows.result_adoption.owner_is_alive", return_value=False
    ):
        result = await adapter(f, store=store).run_once(key)
    assert result.attempt.result.usage.tool_calls is None
    assert result.attempt.result.usage.wall_milliseconds is None
    assert len(f.mutation.calls) == 2
    assert (await store.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_controlled_wall_overrun_records_actual_usage_not_reservation(tmp_path):
    f, key = await ready(tmp_path, no_changes=True)
    with patch.object(meter.time, "perf_counter_ns", side_effect=[0, 100_000_000]):
        result = await adapter(f, max_wall_milliseconds=50).run_once(key)
    assert result.attempt.result.usage.tool_calls == 0
    assert result.attempt.result.usage.wall_milliseconds == 100
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION
    assert f.mutation.calls == []


@pytest.mark.parametrize(
    "field",
    [
        "adoption_id",
        "invocation_id",
        "reservation_id",
        "request_fingerprint",
        "owner_id",
        "revision",
    ],
)
async def test_rehashed_measurement_wrong_identity_fails_closed(tmp_path, field):
    f, key = await ready(tmp_path)
    await adapter(f).run_once(key)
    with closing(f.store._connect()) as c, c:
        c.execute("DROP TRIGGER workflow_adoption_executions_immutable_update")
        value = json.loads(
            c.execute("SELECT payload_json FROM workflow_adoption_executions").fetchone()[0]
        )
        value[field] = 4 if field == "revision" else "forged"
        c.execute(
            "UPDATE workflow_adoption_executions SET payload_json=?,payload_fingerprint=?",
            (canonical(value), digest(value)),
        )
    with pytest.raises(WorkflowStateError, match="binding"):
        await SqliteSessionStore(f.store.database_path).get_workflow_activity(key)


async def test_competing_first_start_cas_issues_only_one_execution_scope(tmp_path):
    f, key = await ready(tmp_path)
    request = await f.store.get_workflow_adoption_request(key)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )
    stores = [SqliteSessionStore(f.store.database_path) for _ in range(2)]

    async def run(store):
        async def dispatch(_attempt, port):
            await service(f, store=store, mutation=port).adopt(request)

        return await store.execute_workflow_adoption(
            key,
            expected_revision=1,
            owner_id="owner",
            owner_fence=claimed.owner_fence,
            updated_at=END,
            mutation=f.mutation,
            dispatch=dispatch,
        )

    results = await asyncio.gather(*(run(s) for s in stores), return_exceptions=True)
    assert sum(isinstance(r, WorkflowStateError) for r in results) == 1
    assert len(f.mutation.calls) == 2
    result = await adapter(f).run_once(key)
    assert result.attempt.result.usage.tool_calls == 2
    with closing(f.store._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM workflow_adoption_executions").fetchone()[0] == 1


@pytest.mark.parametrize("completed_measurement", [False, True])
async def test_real_process_restart_settles_only_durable_measurement(
    tmp_path, completed_measurement
):
    import sys

    f, key = await ready(tmp_path)
    if completed_measurement:
        with (
            patch.object(
                f.store, "reconcile_workflow_adoption", side_effect=RuntimeError("before result")
            ),
            pytest.raises(RuntimeError),
        ):
            await adapter(f).run_once(key)
    else:
        with (
            patch.object(
                meter._ExecutionMeter,
                "complete",
                side_effect=asyncio.CancelledError("before receipt"),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await adapter(f).run_once(key)
    code = """
import asyncio,json,sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
async def main():
    store=SqliteSessionStore(Path(sys.argv[1]))
    result=await store.reconcile_workflow_adoption(sys.argv[2],parent_session_id=sys.argv[3],parent_workspace_root=sys.argv[4],updated_at=datetime.fromisoformat(sys.argv[5]))
    run=await store.get_workflow_run(result.invocation.run_id)
    print(json.dumps({'usage':asdict(result.result.usage),'status':run.status.value}))
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(f.store.database_path),
        key,
        f.binding.runner.session_id,
        str(f.binding.workspace_root),
        (END + timedelta(seconds=2)).isoformat(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    data = json.loads(stdout)
    assert data["usage"]["tool_calls"] == (2 if completed_measurement else None)
    assert (data["usage"]["wall_milliseconds"] is not None) is completed_measurement
    assert data["status"] == ("WAITING" if completed_measurement else "NEEDS_ATTENTION")
    assert len(f.mutation.calls) == 2


async def test_constructed_or_reopened_measurement_scope_cannot_issue_receipt(tmp_path):
    f, key = await ready(tmp_path)
    with (
        patch.object(
            meter._ExecutionMeter, "complete", side_effect=asyncio.CancelledError("before seal")
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter(f).run_once(key)
    assert f.store._workflow_adoption_meter_scopes == {}
    attempt = await f.store.get_workflow_activity(key)
    with closing(f.store._connect()) as c:
        execution_id = c.execute(
            "SELECT execution_id FROM workflow_adoption_executions"
        ).fetchone()[0]
    for store in (f.store, SqliteSessionStore(f.store.database_path)):
        with pytest.raises(WorkflowStateError, match="not issued"):
            meter._ExecutionMeter(store, attempt, execution_id, f.mutation, object())
    result = await adapter(f).run_once(key)
    assert result.attempt.result.usage.tool_calls is None
    assert len(f.mutation.calls) == 2


async def test_partial_success_then_terminal_failure_retains_both_invocations(tmp_path):
    f, key = await ready(tmp_path)

    class PartialFailure(_RecordingMutation):
        async def apply(self, request, *, session_id):
            if self.calls:
                self.calls.append(request)
                raise ResultAdoptionError(
                    "permission denied for second target", kind="permission_denied"
                )
            return await super().apply(request, session_id=session_id)

    port = PartialFailure(f.parent)
    result = await adapter(f, mutation=port).run_once(key)
    assert result.attempt.state is State.FAILED
    assert result.attempt.result.usage.tool_calls == len(port.calls) == 2
    assert result.attempt.result.usage.known
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.WAITING


async def test_cancelled_run_can_account_observed_terminal_without_reopening(tmp_path):
    from neuro_code.domain.workflows.state import WorkflowChange, WorkflowEventKind

    f, key = await ready(tmp_path)
    real = meter._ExecutionMeter.complete

    async def cancel_then_seal(self):
        run = await f.store.get_workflow_run("workflow-run")
        await f.store.transition_workflow_run(
            run.run_id,
            WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            request_id="cancel-before-accounting",
            updated_at=END + timedelta(seconds=2),
        )
        await real(self)

    with patch.object(meter._ExecutionMeter, "complete", cancel_then_seal):
        result = await adapter(f).run_once(key)
    assert result.attempt.result.usage.tool_calls == 2
    assert result.attempt.result.usage.known
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.CANCELLED
    assert f.store._workflow_adoption_meter_scopes == {}
    calls = len(f.mutation.calls)
    assert (await adapter(f).run_once(key)).attempt == result.attempt
    assert len(f.mutation.calls) == calls


async def test_orphan_caller_constructed_receipt_never_authorizes_known_settlement(tmp_path):
    f, key = await ready(tmp_path)
    with (
        patch.object(meter._ExecutionMeter, "complete", return_value=None),
        patch.object(
            f.store,
            "reconcile_workflow_adoption",
            side_effect=RuntimeError("before historical settlement"),
        ),
        pytest.raises(RuntimeError),
    ):
        await adapter(f).run_once(key)
    with closing(f.store._connect()) as c, c:
        execution_id = c.execute(
            "SELECT execution_id FROM workflow_adoption_executions"
        ).fetchone()[0]
        receipt = {"tool_calls": 0, "wall_milliseconds": 0}
        c.execute(
            "INSERT INTO workflow_adoption_measurements VALUES (?,?,?)",
            (execution_id, canonical(receipt), digest(receipt)),
        )
    before = await f.store.get_workflow_run("workflow-run")
    with pytest.raises(WorkflowStateError, match="orphan"):
        await f.store.reconcile_workflow_adoption(
            key,
            parent_session_id=f.binding.runner.session_id,
            parent_workspace_root=str(f.binding.workspace_root),
            updated_at=END,
        )
    assert await f.store.get_workflow_run("workflow-run") == before
    assert (await f.store.get_workflow_activity(key)).state is State.RUNNING
    assert len(f.mutation.calls) == 2


async def test_live_measurement_result_ledger_journal_roll_back_together(tmp_path):
    f, key = await ready(tmp_path)
    with closing(f.store._connect()) as c, c:
        c.execute(
            "CREATE TRIGGER fail_live_result BEFORE INSERT ON workflow_activity_results BEGIN SELECT RAISE(ABORT,'live result crash'); END"
        )
    with pytest.raises(WorkflowStateError):
        await adapter(f).run_once(key)
    attempt = await f.store.get_workflow_activity(key)
    assert attempt.state is State.RUNNING
    assert attempt.result is None
    run = await f.store.get_workflow_run("workflow-run")
    reservation = next(
        r for r in run.ledger.reservations if r.reservation_id == attempt.reservation_id
    )
    assert reservation.consumed is None
    with closing(f.store._connect()) as c, c:
        assert c.execute("SELECT COUNT(*) FROM workflow_adoption_measurements").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM workflow_activity_results").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT COUNT(*) FROM workflow_transition_journal WHERE request_id=?",
                ("activity-settle:" + key,),
            ).fetchone()[0]
            == 0
        )
        c.execute("DROP TRIGGER fail_live_result")
    assert f.store._workflow_adoption_meter_scopes == {}
    result = await adapter(f).run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert result.attempt.result.usage.tool_calls is None
    assert result.attempt.result.usage.wall_milliseconds is None
    assert len(f.mutation.calls) == 2
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION
