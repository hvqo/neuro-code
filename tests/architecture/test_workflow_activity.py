"""DW5b-1 real SQLite invocation, ownership, budget, recovery and result contracts."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import (
    WorkflowActivityResult,
)
from neuro_code.domain.workflows.activity import (
    WorkflowActivityState as State,
)
from neuro_code.domain.workflows.interpreter import OutputKind, WorkflowStepOutput, invocation_id
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    StepIdentity,
    WorkflowChange,
    WorkflowEventKind,
    WorkflowStatus,
)
from neuro_code.infrastructure.persistence import sqlite_session_workflow_activity as owner
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from tests.architecture.test_workflow_interpreter import (
    activity,
    engine,
    reopen,
    setup,
    source,
    tick,
)
from tests.architecture.test_workflow_projection import END


async def ready(tmp_path, *, ceiling=None, inputs=None):
    data = source()
    data["steps"] = [activity("verify", inputs=inputs)]
    store = await setup(tmp_path, data, ceiling=ceiling)
    await tick(store)
    await tick(store)
    run = await store.get_workflow_run("run")
    key = invocation_id("run", StepIdentity("verify"), run.steps[0].input_fingerprint)
    return store, await store.get_workflow_activity(key)


async def claim(store, attempt, *, reserved=None):
    reserved = (
        reserved
        if reserved is not None
        else BudgetAmounts(model_calls=1, tool_calls=2, wall_milliseconds=10)
    )
    return await store.claim_workflow_activity(
        attempt.invocation.invocation_id,
        expected_revision=attempt.revision,
        owner_id="external",
        reserved=reserved,
        updated_at=END,
    )


async def start(store, attempt):
    return await store.start_workflow_activity(
        attempt.invocation.invocation_id,
        expected_revision=attempt.revision,
        owner_id=attempt.owner_id,
        owner_fence=attempt.owner_fence,
        updated_at=END,
    )


def result(
    attempt,
    state=State.COMPLETED,
    *,
    usage=None,
):
    usage = (
        usage
        if usage is not None
        else BudgetAmounts(model_calls=1, tool_calls=1, wall_milliseconds=5)
    )
    invocation = attempt.invocation
    return WorkflowActivityResult(
        invocation.invocation_id,
        invocation.request_fingerprint,
        invocation.activity,
        state,
        "external-operation",
        "a" * 64,
        usage,
        END,
        canonical({"status": "recorded", "workspace_generation": 0})
        if state is State.COMPLETED
        else None,
    )


async def finish(store, attempt, value=None):
    return await store.finish_workflow_activity(
        value or result(attempt),
        expected_revision=attempt.revision,
        owner_id=attempt.owner_id,
        owner_fence=attempt.owner_fence,
    )


def counts(store):
    with closing(sqlite3.connect(store.database_path)) as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "workflow_activity_attempts",
                "workflow_activity_results",
                "workflow_activity_events",
                "workflow_transition_journal",
                "workflow_step_outputs",
            )
        )


async def test_publish_waiting_and_readonly_tick_no_side_effects(tmp_path):
    store, attempt = await ready(tmp_path)
    run = await store.get_workflow_run("run")
    assert attempt.state is State.READY
    assert attempt.owner_id is None
    assert not run.ledger.reservations
    assert run.status is WorkflowStatus.WAITING
    assert run.steps[0].status is WorkflowStatus.WAITING
    before = counts(store)
    for _ in range(5):
        waiting = await tick(store)
        assert waiting.action == "activity_waiting"
        assert not waiting.progressed
    assert counts(store) == before
    assert await store.get_workflow_run("run") == run


@pytest.mark.parametrize("point", ["_append_event", "_save_attempt"])
async def test_publish_crash_rolls_back_all_facts(tmp_path, point):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    before = await store.get_workflow_run("run")
    with patch.object(owner, point, side_effect=RuntimeError("crash")), pytest.raises(RuntimeError):
        await tick(store)
    assert await store.get_workflow_run("run") == before
    assert counts(store)[:3] == (0, 0, 0)


async def test_publish_ack_loss_reopen_and_exact_replay(tmp_path):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    real = store.publish_workflow_activity

    async def lost_ack(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("ack loss")

    with (
        patch.object(store, "publish_workflow_activity", side_effect=lost_ack),
        pytest.raises(RuntimeError),
    ):
        await tick(store)
    store = await reopen(store)
    assert (await tick(store)).action == "activity_waiting"
    run = await store.get_workflow_run("run")
    key = invocation_id("run", run.position, run.steps[0].input_fingerprint)
    attempt = await store.get_workflow_activity(key)
    before = counts(store)
    replay = await store.publish_workflow_activity(
        replace(attempt.invocation, created_at=END + timedelta(seconds=1)),
        expected_generation=0,
        owner_id="gone",
        owner_fence=0,
        updated_at=END,
    )
    assert replay.replayed
    assert counts(store) == before
    with pytest.raises(WorkflowStateError, match="conflict"):
        await store.publish_workflow_activity(
            replace(attempt.invocation, parent_session_id="other"),
            expected_generation=0,
            owner_id="gone",
            owner_fence=0,
            updated_at=END,
        )


async def test_missing_waiting_invocation_is_integrity_failure(tmp_path):
    store, _ = await ready(tmp_path)
    with (
        patch.object(store, "get_workflow_activity", return_value=None),
        pytest.raises(WorkflowStateError, match="no durable invocation"),
    ):
        await tick(store)


def _claim_process(database, key, name, ready_queue, gate, output):
    ready_queue.put(name)
    if not gate.wait(15):
        output.put("timeout")
        return

    async def run():
        store = SqliteSessionStore(Path(database))
        try:
            attempt = await store.claim_workflow_activity(
                key,
                expected_revision=0,
                owner_id=name,
                reserved=BudgetAmounts(model_calls=1),
                updated_at=END,
            )
            output.put(attempt.owner_id)
        except WorkflowStateError as error:
            output.put(error.kind)

    asyncio.run(run())


async def test_competing_process_claims_one_owner_and_one_reservation(tmp_path):
    store, attempt = await ready(tmp_path)
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    queue = context.Queue()
    output = context.Queue()
    processes = [
        context.Process(
            target=_claim_process,
            args=(
                str(store.database_path),
                attempt.invocation.invocation_id,
                name,
                queue,
                gate,
                output,
            ),
        )
        for name in ("a", "b")
    ]
    try:
        for process in processes:
            process.start()
        assert {await asyncio.to_thread(queue.get, True, 20) for _ in processes} == {"a", "b"}
        gate.set()
        outcomes = [await asyncio.to_thread(output.get, True, 20) for _ in processes]
        assert outcomes.count("concurrent_modification") == 1
        for process in processes:
            await asyncio.to_thread(process.join, 20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        queue.close()
        output.close()
    run = await store.get_workflow_run("run")
    assert len(run.ledger.reservations) == 1


@pytest.mark.parametrize("point", ["_save_run", "_append_event", "_save_attempt"])
async def test_claim_budget_and_owner_rollback_together(tmp_path, point):
    store, attempt = await ready(tmp_path)
    before = await store.get_workflow_run("run")
    with patch.object(owner, point, side_effect=RuntimeError("crash")), pytest.raises(RuntimeError):
        await claim(store, attempt)
    assert await store.get_workflow_run("run") == before
    assert await store.get_workflow_activity(attempt.invocation.invocation_id) == attempt


async def test_claim_ceiling_exhaustion_never_dispatches(tmp_path):
    store, attempt = await ready(tmp_path, ceiling=BudgetAmounts())
    with pytest.raises(WorkflowStateError, match="ceiling"):
        await claim(store, attempt)
    assert (
        await store.get_workflow_activity(attempt.invocation.invocation_id)
    ).state is State.READY
    assert not (await store.get_workflow_run("run")).ledger.reservations


async def test_claim_ack_loss_reservation_replay_and_running_no_reset(tmp_path):
    store, attempt = await ready(tmp_path)
    claimed = await claim(store, attempt)
    before = counts(store)
    store = await reopen(store)
    assert await claim(store, attempt) == claimed
    assert counts(store) == before
    assert claimed.state is State.CLAIMED  # no effect has started
    assert (await store.get_workflow_run("run")).ledger.reservations[0].consumed is None
    running = await start(store, claimed)
    assert await start(store, claimed) == running  # historical ACK replay, not dispatch permission
    with pytest.raises(WorkflowStateError):
        await claim(store, attempt)
    store = await reopen(store)
    assert (
        await store.get_workflow_activity(attempt.invocation.invocation_id)
    ).state is State.RUNNING


@pytest.mark.parametrize("wrong", ["owner", "fence", "revision"])
async def test_stale_activity_owner_start_and_finish_rejected(tmp_path, wrong):
    store, attempt = await ready(tmp_path)
    attempt = await claim(store, attempt)
    args = {
        "expected_revision": attempt.revision,
        "owner_id": attempt.owner_id,
        "owner_fence": attempt.owner_fence,
    }
    args[{"owner": "owner_id", "fence": "owner_fence", "revision": "expected_revision"}[wrong]] = (
        "other" if wrong == "owner" else 0
    )
    with pytest.raises(WorkflowStateError):
        await store.start_workflow_activity(
            attempt.invocation.invocation_id, updated_at=END, **args
        )
    with pytest.raises(WorkflowStateError):
        await store.finish_workflow_activity(result(attempt, State.BLOCKED), **args)
    assert await store.get_workflow_activity(attempt.invocation.invocation_id) == attempt


async def test_cancel_before_start_rejects_boundary_but_preserves_late_accounting(tmp_path):
    store, attempt = await ready(tmp_path)
    attempt = await claim(store, attempt)
    run = await store.get_workflow_run("run")
    await store.transition_workflow_run(
        "run",
        WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="cancel",
        updated_at=END,
    )
    with pytest.raises(WorkflowStateError, match="start boundary"):
        await start(store, attempt)
    await finish(store, attempt, result(attempt, State.BLOCKED, usage=BudgetAmounts()))
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.CANCELLED


@pytest.mark.parametrize(
    "state", [State.COMPLETED, State.FAILED, State.BLOCKED, State.INDETERMINATE]
)
async def test_terminal_fact_settlement_reopen_replay_and_later_tick(tmp_path, state):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    value = result(attempt, state)
    terminal = await finish(store, attempt, value)
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.WAITING
    before = counts(store)
    store = await reopen(store)
    assert (
        await store.finish_workflow_activity(
            value, expected_revision=0, owner_id="no-live-owner", owner_fence=0
        )
        == terminal
    )
    assert counts(store) == before
    assert (await store.get_workflow_run("run")).ledger.committed == value.usage
    with pytest.raises(WorkflowStateError, match="conflict"):
        await store.finish_workflow_activity(
            replace(value, source_id="different"),
            expected_revision=0,
            owner_id="gone",
            owner_fence=0,
        )
    consumed = await tick(store)
    expected = {
        State.COMPLETED: WorkflowStatus.RUNNING,
        State.FAILED: WorkflowStatus.FAILED,
        State.BLOCKED: WorkflowStatus.NEEDS_ATTENTION,
        State.INDETERMINATE: WorkflowStatus.NEEDS_ATTENTION,
    }[state]
    assert consumed.run.status is expected
    assert consumed.progressed
    output = await store.get_workflow_step_output("run", StepIdentity("verify"))
    if state is State.COMPLETED:
        assert output.kind is OutputKind.ACTIVITY
        assert output.source_fingerprint == value.fingerprint
        assert json.loads(output.output_json)["status"] == "recorded"  # not verification PASS
    else:
        assert output is None
    assert (await store.get_workflow_activity(value.invocation_id)).result == value
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE workflow_activity_attempts SET state = 'ready'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE workflow_activity_results SET payload_json = '{}'")


@pytest.mark.parametrize("point", ["_save_run", "_append_event", "_save_attempt"])
async def test_result_and_budget_crash_rollback(tmp_path, point):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    before = await store.get_workflow_run("run")
    before_counts = counts(store)
    with patch.object(owner, point, side_effect=RuntimeError("crash")), pytest.raises(RuntimeError):
        await finish(store, attempt)
    assert await store.get_workflow_run("run") == before
    assert counts(store) == before_counts
    assert await store.get_workflow_activity(attempt.invocation.invocation_id) == attempt


async def test_result_commit_before_ack_and_consumption_before_ack(tmp_path):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    real = store.finish_workflow_activity

    async def lost_ack(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("ack")

    with (
        patch.object(store, "finish_workflow_activity", side_effect=lost_ack),
        pytest.raises(RuntimeError),
    ):
        await finish(store, attempt)
    store = await reopen(store)
    real_output = store.commit_workflow_step_output

    async def lost_output(*args, **kwargs):
        await real_output(*args, **kwargs)
        raise RuntimeError("ack")

    with (
        patch.object(store, "commit_workflow_step_output", side_effect=lost_output),
        pytest.raises(RuntimeError),
    ):
        await tick(store)
    store = await reopen(store)
    assert (await tick(store)).action == "control_flow_exhausted"
    assert counts(store)[1] == 1
    assert counts(store)[4] == 1
    assert len((await store.get_workflow_run("run")).ledger.reservations) == 1


@pytest.mark.parametrize("usage", [BudgetAmounts(model_calls=None), BudgetAmounts(model_calls=101)])
async def test_unknown_and_overrun_truth_never_reopens_progress(tmp_path, usage):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    await finish(store, attempt, result(attempt, usage=usage))
    run = await store.get_workflow_run("run")
    assert run.status is WorkflowStatus.NEEDS_ATTENTION
    assert run.ledger.reservations[0].consumed == usage
    assert not (await tick(store)).progressed
    assert await store.get_workflow_step_output("run", StepIdentity("verify")) is None


@pytest.mark.parametrize("field", ["request_fingerprint", "activity", "output_json"])
async def test_result_mismatch_or_invalid_schema_fail_closed(tmp_path, field):
    from neuro_code.domain.workflows.definition import ActivityKind

    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    wrong = {
        "request_fingerprint": "b" * 64,
        "activity": ActivityKind.ADOPT,
        "output_json": canonical({"status": "PASS", "workspace_generation": "bad"}),
    }[field]
    with pytest.raises(WorkflowStateError):
        await finish(store, attempt, replace(result(attempt), **{field: wrong}))
    assert (
        await store.get_workflow_activity(attempt.invocation.invocation_id)
    ).state is State.RUNNING


@pytest.mark.parametrize(
    "table", ["workflow_activity_attempts", "workflow_activity_results", "workflow_activity_events"]
)
async def test_persisted_activity_tamper_fails_closed(tmp_path, table):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    await finish(store, attempt)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        if table == "workflow_activity_attempts":
            connection.execute("DROP TRIGGER workflow_activity_attempts_guard_update")
            connection.execute(
                "UPDATE workflow_activity_attempts SET snapshot_fingerprint = ?", ("b" * 64,)
            )
        else:
            connection.execute(f"DROP TRIGGER {table}_immutable_update")
            connection.execute(f"UPDATE {table} SET payload_json = '{{}}'")
    with pytest.raises(WorkflowStateError):
        await tick(store)


async def test_schema39_fake_output_preserved_without_rewrite(tmp_path):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    run = await store.get_workflow_run("run")
    step = run.steps[0]
    key = invocation_id("run", step.identity, step.input_fingerprint)
    old = WorkflowStepOutput(
        "run",
        step.identity,
        step.input_fingerprint,
        OutputKind.FAKE_ACTIVITY,
        key,
        digest([key, step.input_fingerprint]),
        canonical({"status": "fake", "workspace_generation": 0}),
    )
    await store.commit_workflow_step_output(
        old,
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=END,
    )
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        before = connection.execute("SELECT * FROM workflow_step_outputs").fetchall()
        connection.execute("ALTER TABLE workflow_step_outputs RENAME TO old_output")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'old_output'"
        ).fetchone()[0]
        sql = sql.replace('"old_output"', "workflow_step_outputs").replace(", 'activity'", "")
        connection.execute(sql)
        connection.execute("INSERT INTO workflow_step_outputs SELECT * FROM old_output")
        connection.execute("DROP TABLE old_output")
        connection.execute("DROP TABLE workflow_activity_results")
        connection.execute("DROP TABLE workflow_activity_events")
        connection.execute("DROP TABLE workflow_activity_attempts")
        connection.execute("UPDATE schema_meta SET version = 39")
    store = await reopen(store)
    assert SCHEMA_VERSION == 40
    assert await store.get_workflow_step_output("run", step.identity) == old
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT * FROM workflow_step_outputs").fetchall() == before
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert (await tick(store)).action == "control_flow_exhausted"


async def test_activity_reference_request_schema_and_literal_validation(tmp_path):
    store, attempt = await ready(
        tmp_path,
        inputs={
            "objective": {"kind": "input", "field_path": ["objective"]},
            "literal": {"kind": "literal", "value": True},
        },
    )
    assert json.loads(attempt.invocation.request_json) == {"objective": "中文目标", "literal": True}
    for wrong in ({"objective": False, "literal": True}, {"objective": "x", "literal": 1}, {}):
        definition = await store.get_workflow_definition(attempt.invocation.definition_fingerprint)
        with pytest.raises((WorkflowStateError, ValueError)):
            owner._validate_request(
                definition,
                definition.steps[0],
                replace(
                    attempt.invocation,
                    invocation_id=invocation_id("run", attempt.invocation.step, digest(wrong)),
                    request_json=canonical(wrong),
                    request_fingerprint=digest(wrong),
                    input_fingerprint=digest(wrong),
                ),
            )


async def test_production_interpreter_never_calls_fake(tmp_path):
    store, _ = await ready(tmp_path)
    with patch(
        "neuro_code.application.workflows.fake_workflow_activity.DeterministicFakeWorkflowActivity.evaluate",
        side_effect=AssertionError("fake executed"),
    ):
        assert not (await tick(store, engine(store))).progressed


@pytest.mark.parametrize("state", [State.FAILED, State.BLOCKED, State.INDETERMINATE])
async def test_failure_consumption_rollback_and_ack_loss(tmp_path, state):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    await finish(store, attempt, result(attempt, state))
    before = await store.get_workflow_run("run")
    with (
        patch.object(owner, "_append_event", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError),
    ):
        await tick(store)
    assert await store.get_workflow_run("run") == before
    real = store.consume_workflow_activity_failure

    async def lost_ack(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("ack")

    with (
        patch.object(store, "consume_workflow_activity_failure", side_effect=lost_ack),
        pytest.raises(RuntimeError),
    ):
        await tick(store)
    store = await reopen(store)
    assert not (await tick(store)).progressed
    after = counts(store)
    run = await store.get_workflow_run("run")
    value = result(attempt, state)
    replay = await store.consume_workflow_activity_failure(
        value.invocation_id,
        value.fingerprint,
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=END,
    )
    assert replay.replayed
    assert counts(store) == after


async def test_failure_consumption_wrong_result_or_stale_owner_rejected(tmp_path):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    value = result(attempt, State.FAILED)
    await finish(store, attempt, value)
    run = await store.get_workflow_run("run")
    for fingerprint, generation in (("b" * 64, run.generation), (value.fingerprint, 0)):
        with pytest.raises(WorkflowStateError):
            await store.consume_workflow_activity_failure(
                value.invocation_id,
                fingerprint,
                expected_generation=generation,
                owner_id=run.owner_id,
                owner_fence=run.owner_fence,
                updated_at=END,
            )
    assert await store.get_workflow_run("run") == run


async def test_result_insert_failure_rolls_back_terminal_and_settlement(tmp_path):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    before = await store.get_workflow_run("run")
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_result BEFORE INSERT ON workflow_activity_results BEGIN SELECT RAISE(ABORT, 'crash window'); END"
        )
    with pytest.raises(WorkflowStateError):
        await finish(store, attempt)
    assert await store.get_workflow_run("run") == before
    assert await store.get_workflow_activity(attempt.invocation.invocation_id) == attempt


async def test_unknown_result_survives_explicit_existing_ledger_reconciliation(tmp_path):
    store, attempt = await ready(tmp_path)
    attempt = await start(store, await claim(store, attempt))
    value = result(attempt, usage=BudgetAmounts(model_calls=None))
    terminal = await finish(store, attempt, value)
    run = await store.get_workflow_run("run")
    await store.transition_workflow_run(
        "run",
        WorkflowChange(
            WorkflowEventKind.RECONCILED,
            reservation_id=terminal.reservation_id,
            amounts=BudgetAmounts(model_calls=1),
        ),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="reconcile",
        updated_at=END,
    )
    assert (await store.get_workflow_activity(value.invocation_id)).result == value
    assert (await store.get_workflow_run("run")).ledger.committed.model_calls == 1


@pytest.mark.parametrize("wrong", ["generation", "owner", "fence"])
async def test_stale_interpreter_publication_rejected(tmp_path, wrong):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    run = await store.get_workflow_run("run")
    from neuro_code.domain.workflows.activity import WorkflowActivityInvocation

    step = run.steps[0]
    invocation = WorkflowActivityInvocation(
        invocation_id("run", step.identity, step.input_fingerprint),
        "run",
        run.definition_fingerprint,
        run.parent_session_id,
        step.identity,
        (await store.get_workflow_definition(run.definition_fingerprint)).steps[0].activity,
        "{}",
        digest({}),
        step.input_fingerprint,
        END,
    )
    args = {
        "expected_generation": run.generation,
        "owner_id": run.owner_id,
        "owner_fence": run.owner_fence,
    }
    args[
        {"generation": "expected_generation", "owner": "owner_id", "fence": "owner_fence"}[wrong]
    ] = "other" if wrong == "owner" else 0
    with pytest.raises(WorkflowStateError, match="stale"):
        await store.publish_workflow_activity(invocation, **args, updated_at=END)
    assert counts(store)[0] == 0


async def test_timestamps_and_start_boundary_fail_closed(tmp_path):
    store, attempt = await ready(tmp_path)
    with pytest.raises(WorkflowStateError, match="time"):
        await store.claim_workflow_activity(
            attempt.invocation.invocation_id,
            expected_revision=0,
            owner_id="external",
            reserved=BudgetAmounts(),
            updated_at=END - timedelta(seconds=1),
        )
    attempt = await claim(store, attempt)
    with pytest.raises(WorkflowStateError, match="start boundary"):
        await finish(store, attempt)  # cannot publish COMPLETED before RUNNING
    with pytest.raises(WorkflowStateError):
        await store.start_workflow_activity(
            attempt.invocation.invocation_id,
            expected_revision=1,
            owner_id="external",
            owner_fence=1,
            updated_at=END - timedelta(seconds=1),
        )


async def test_artifact_request_is_exact_and_domain_types_are_bounded(tmp_path):
    store, attempt = await ready(
        tmp_path,
        inputs={
            "artifact": {
                "kind": "artifact",
                "artifact_id": "artifact",
                "integrity_fingerprint": "a" * 64,
            }
        },
    )
    assert json.loads(attempt.invocation.request_json)["artifact"]["workspace_identity"] is None
    with pytest.raises(ValueError, match=r"activity|result"):
        replace(attempt.invocation, request_json='{"x": 1}')
    with pytest.raises(ValueError, match=r"activity|result"):
        replace(attempt, owner_fence=1)
    with pytest.raises(ValueError, match=r"activity|result"):
        replace(attempt, revision=5)
    with pytest.raises(ValueError, match=r"activity|result"):
        replace(result(await start(store, await claim(store, attempt))), state=State.READY)
