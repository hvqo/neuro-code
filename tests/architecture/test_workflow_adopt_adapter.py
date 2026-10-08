"""Real shared adoption engine + SQLite, exact binding and crash recovery."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from neuro_code.application.ports.result_adoption import ResultAdoptionError
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.workflows.workflow_adopt import WorkflowAdoptActivityAdapter
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.interpreter import invocation_id
from neuro_code.domain.workflows.publication import canonical
from neuro_code.domain.workflows.state import BudgetAmounts, StepIdentity, WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from tests.architecture.test_workflow_adoption import END, fixture, service
from tests.architecture.test_workflow_interpreter import engine


async def ready(tmp_path, **kwargs):
    f, _request, _projection = await fixture(tmp_path, with_activity=True, **kwargs)
    interpreter = engine(f.store)
    for _ in range(3):
        run = await f.store.get_workflow_run("workflow-run")
        await interpreter.advance_once(
            run.run_id,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=END,
        )
    run = await f.store.get_workflow_run("workflow-run")
    step = next(s for s in run.steps if s.identity == StepIdentity("adopt"))
    key = invocation_id(run.run_id, step.identity, step.input_fingerprint)
    return f, key


def adapter(f, *, store=None, mutation=None, **kwargs):
    from neuro_code.application.workflows.completed_dag_adoption import (
        WorkflowCompletedDagSourceAdapter,
    )

    store = store or f.store
    return WorkflowAdoptActivityAdapter(
        activities=store,
        adoption_facts=store,
        state=store,
        adoptions=store,
        source_adapter=WorkflowCompletedDagSourceAdapter(
            projections=store, dags=store, leases=store
        ),
        service_factory=lambda port: service(
            f, store=store, mutation=port, clock=lambda: END + timedelta(seconds=1)
        ),
        mutation=mutation or f.mutation,
        parent_session_id=f.binding.runner.session_id,
        parent_workspace_root=f.binding.workspace_root,
        clock=lambda: END + timedelta(seconds=1),
        **kwargs,
    )


@pytest.mark.parametrize("mapped", [False, True])
async def test_real_adopt_and_later_consume(tmp_path, mapped):
    f, key = await ready(tmp_path, mapped=mapped)
    result = await adapter(f).run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert result.attempt.result.output_json == canonical(
        {"status": "completed", "parent_workspace_changed": True}
    )
    assert f.parent.current("U.txt").content == b"unrelated dirty\n"
    assert result.attempt.result.usage.model_calls == 0
    assert result.attempt.result.usage.tool_calls == len(f.mutation.calls)
    assert len(f.mutation.calls) > 0
    run = await f.store.get_workflow_run("workflow-run")
    assert run.status is WorkflowStatus.WAITING
    before = len(f.mutation.calls)
    assert (await adapter(f).run_once(key)).attempt == result.attempt
    assert len(f.mutation.calls) == before
    outcome = await engine(f.store).advance_once(
        run.run_id,
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=END + timedelta(seconds=2),
    )
    assert outcome.action == "consume_activity"


async def test_no_change_is_success(tmp_path):
    f, key = await ready(tmp_path, no_changes=True)
    result = await adapter(f).run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert not __import__("json").loads(result.attempt.result.output_json)[
        "parent_workspace_changed"
    ]
    assert f.mutation.calls == []


async def test_running_without_underlying_fact_never_redispatches(tmp_path):
    f, key = await ready(tmp_path)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="dead-owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )
    await f.store.start_workflow_activity(
        key, expected_revision=1, owner_id=claimed.owner_id, owner_fence=1, updated_at=END
    )
    result = await adapter(f).run_once(key)
    assert result.disposition == "needs_attention"
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION
    assert (await f.store.get_workflow_activity(key)).state is State.RUNNING
    assert f.mutation.calls == []


async def test_terminal_adoption_after_crash_reconciles_without_owner_or_resources(tmp_path):
    f, key = await ready(tmp_path)
    with (
        patch.object(
            f.store,
            "reconcile_workflow_adoption",
            side_effect=RuntimeError("crash before Activity result"),
        ),
        pytest.raises(RuntimeError),
    ):
        await adapter(f).run_once(key)
    calls = len(f.mutation.calls)
    reopened = SqliteSessionStore(f.store.database_path)
    with patch.object(f.worktrees, "inspect", side_effect=AssertionError("live resource access")):
        result = await adapter(f, store=reopened).run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert result.attempt.result.usage.tool_calls is None
    assert result.attempt.result.usage.wall_milliseconds is None
    assert len(f.mutation.calls) == calls
    assert (
        await reopened.get_workflow_run("workflow-run")
    ).status is WorkflowStatus.NEEDS_ATTENTION


@pytest.mark.parametrize("kwargs", [{"overlap": True}, {"parent_conflict": True}])
async def test_conflict_never_becomes_success(tmp_path, kwargs):
    f, key = await ready(tmp_path, **kwargs)
    # Overlap is rejected before creating a plan; parent conflict is durable.
    if kwargs.get("overlap"):
        with pytest.raises(ResultAdoptionError):
            await adapter(f).run_once(key)
    else:
        result = await adapter(f).run_once(key)
        assert result.attempt.state is State.BLOCKED
    assert f.mutation.calls == []


async def test_claim_budget_exhaustion_has_no_mutation(tmp_path):
    f, key = await ready(
        tmp_path,
        activity_ceiling=BudgetAmounts(generated_tasks=2, tool_calls=1, wall_milliseconds=600000),
    )
    with pytest.raises(WorkflowStateError, match="ceiling"):
        await adapter(f, max_operations=64, max_wall_milliseconds=300000).run_once(key)
    assert f.mutation.calls == []


async def test_real_git_parent_files_are_written_only_through_injected_port(tmp_path):
    from tests.test_result_adoption import _RecordingMutation

    f, key = await ready(tmp_path)

    class DiskMutation(_RecordingMutation):
        async def apply(self, request, *, session_id):
            observed = await f.store.get_workflow_activity(key)
            assert observed.state is State.RUNNING
            run = await f.store.get_workflow_run("workflow-run")
            reservation = next(
                r for r in run.ledger.reservations if r.reservation_id == observed.reservation_id
            )
            assert reservation.reserved.tool_calls > 0
            result = await super().apply(request, session_id=session_id)
            path = f.binding.workspace_root / request.path
            if request.desired is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(request.desired.content)
            return result

    mutation = DiskMutation(f.parent)
    result = await adapter(f, mutation=mutation).run_once(key)
    assert result.attempt.state is State.COMPLETED
    for request in mutation.calls:
        path = f.binding.workspace_root / request.path
        assert (path.read_bytes() if path.exists() else None) == (
            request.desired.content if request.desired else None
        )


async def test_mutation_before_ack_crash_recovery_never_rewrites_desired_image(tmp_path):
    import asyncio

    from tests.test_result_adoption import _RecordingMutation

    f, key = await ready(tmp_path)

    class CrashMutation(_RecordingMutation):
        async def apply(self, request, *, session_id):
            result = await super().apply(request, session_id=session_id)
            if len(self.calls) == 1:
                raise asyncio.CancelledError("process crashed after mutation")
            return result

    mutation = CrashMutation(f.parent)
    with pytest.raises(asyncio.CancelledError):
        await adapter(f, mutation=mutation).run_once(key)
    path = mutation.calls[0].path
    with patch(
        "neuro_code.application.workflows.result_adoption.owner_is_alive", return_value=False
    ):
        result = await adapter(
            f, store=SqliteSessionStore(f.store.database_path), mutation=mutation
        ).run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert sum(call.path == path for call in mutation.calls) == 1
    assert result.attempt.result.usage.tool_calls is None


async def test_plan_before_mutation_crash_recovers_with_same_plan(tmp_path):
    f, key = await ready(tmp_path)
    with (
        patch.object(
            f.store,
            "claim_result_adoption",
            side_effect=asyncio.CancelledError("crash after prepare"),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter(f).run_once(key)
    assert f.mutation.calls == []
    request = await f.store.get_workflow_adoption_request(key)
    before = await f.store.get_result_adoption(request.adoption_id)
    with patch(
        "neuro_code.application.workflows.result_adoption.owner_is_alive", return_value=False
    ):
        result = await adapter(f, store=SqliteSessionStore(f.store.database_path)).run_once(key)
    assert result.attempt.state is State.COMPLETED
    after = await f.store.get_result_adoption(request.adoption_id)
    assert after.plan == before.plan


async def test_running_start_ack_loss_never_dispatches(tmp_path):
    f, key = await ready(tmp_path)
    real = f.store.start_workflow_activity

    async def lost_ack(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("lost start ACK")

    with (
        patch.object(f.store, "start_workflow_activity", side_effect=lost_ack),
        pytest.raises(RuntimeError),
    ):
        await adapter(f).run_once(key)
    assert (await adapter(f).run_once(key)).disposition == "needs_attention"
    assert f.mutation.calls == []


async def test_result_commit_ack_loss_is_exact_replay_without_accounting(tmp_path):
    f, key = await ready(tmp_path)
    real = f.store.reconcile_workflow_adoption

    async def lost_ack(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("lost terminal ACK")

    with (
        patch.object(f.store, "reconcile_workflow_adoption", side_effect=lost_ack),
        pytest.raises(RuntimeError),
    ):
        await adapter(f).run_once(key)
    before = await f.store.get_workflow_run("workflow-run")
    calls = len(f.mutation.calls)
    assert (
        await adapter(f, store=SqliteSessionStore(f.store.database_path)).run_once(key)
    ).attempt.state is State.COMPLETED
    assert await f.store.get_workflow_run("workflow-run") == before
    assert len(f.mutation.calls) == calls


async def test_live_underlying_owner_is_not_bypassed(tmp_path):
    f, key = await ready(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    from tests.test_result_adoption import _RecordingMutation

    class SlowMutation(_RecordingMutation):
        async def apply(self, request, *, session_id):
            started.set()
            await release.wait()
            return await super().apply(request, session_id=session_id)

    mutation = SlowMutation(f.parent)
    original = asyncio.create_task(adapter(f, mutation=mutation).run_once(key))
    await asyncio.wait_for(started.wait(), timeout=10)
    try:
        second = await adapter(f, mutation=mutation).run_once(key)
        assert second.disposition == "busy"
        assert mutation.calls == []
    finally:
        release.set()
        await original
    assert [call.path for call in mutation.calls] == ["A.txt", "C.txt"]


@pytest.mark.parametrize("state", ["failed", "indeterminate"])
async def test_real_terminal_failure_mapping(tmp_path, state):
    f, key = await ready(tmp_path)

    class FailedMutation:
        async def apply(self, request, *, session_id):
            if state == "indeterminate":
                f.parent.set_entry(
                    replace(request.expected, content=b"third party\n"), path=request.path
                )
                raise OSError("uncertain mutation")
            raise ResultAdoptionError("permission denied", kind="permission_denied")

    outcome = await adapter(f, mutation=FailedMutation()).run_once(key)
    assert outcome.attempt.state is (State.FAILED if state == "failed" else State.INDETERMINATE)
    assert outcome.attempt.result.output_json is None


@pytest.mark.parametrize("binding", ["parent_session_id", "parent_workspace_root"])
async def test_terminal_reconciliation_wrong_parent_rejected(tmp_path, binding):
    f, key = await ready(tmp_path)
    await adapter(f).run_once(key)
    args = {
        "parent_session_id": f.binding.runner.session_id,
        "parent_workspace_root": str(f.binding.workspace_root),
        "updated_at": END + timedelta(seconds=3),
    }
    args[binding] = (
        "wrong-session" if binding == "parent_session_id" else str(tmp_path / "wrong-root")
    )
    before = await f.store.get_workflow_run("workflow-run")
    with pytest.raises(WorkflowStateError, match="binding"):
        await f.store.reconcile_workflow_adoption(key, **args)
    assert await f.store.get_workflow_run("workflow-run") == before


async def test_cancellation_before_dispatch_is_not_authority(tmp_path):
    from neuro_code.domain.workflows.state import WorkflowChange, WorkflowEventKind

    f, key = await ready(tmp_path)
    run = await f.store.get_workflow_run("workflow-run")
    await f.store.transition_workflow_run(
        run.run_id,
        WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="cancel",
        updated_at=END,
    )
    assert (await adapter(f).run_once(key)).disposition == "not_authorized"
    assert f.mutation.calls == []


async def test_late_terminal_reconciliation_cannot_reopen_cancelled_run(tmp_path):
    from neuro_code.domain.workflows.state import WorkflowChange, WorkflowEventKind

    f, key = await ready(tmp_path)
    with (
        patch.object(f.store, "reconcile_workflow_adoption", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError),
    ):
        await adapter(f).run_once(key)
    run = await f.store.get_workflow_run("workflow-run")
    await f.store.transition_workflow_run(
        run.run_id,
        WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="cancel",
        updated_at=END + timedelta(seconds=2),
    )
    assert (await adapter(f).run_once(key)).attempt.state is State.COMPLETED
    assert (await f.store.get_workflow_run(run.run_id)).status is WorkflowStatus.CANCELLED


async def test_direct_fake_adopt_result_is_not_a_terminal_proof(tmp_path):
    from neuro_code.domain.workflows.activity import WorkflowActivityResult
    from neuro_code.domain.workflows.definition import ActivityKind

    f, key = await ready(tmp_path)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )
    await f.store.start_workflow_activity(
        key, expected_revision=1, owner_id="owner", owner_fence=1, updated_at=END
    )
    fake = WorkflowActivityResult(
        key,
        claimed.invocation.request_fingerprint,
        ActivityKind.ADOPT,
        State.COMPLETED,
        "fake-adoption",
        "a" * 64,
        BudgetAmounts(),
        END,
        canonical({"status": "completed", "parent_workspace_changed": True}),
    )
    with pytest.raises(WorkflowStateError, match="terminal-proof"):
        await f.store.finish_workflow_activity(
            fake, expected_revision=2, owner_id="owner", owner_fence=1
        )
    with pytest.raises(WorkflowStateError, match="terminal fact"):
        await f.store.reconcile_workflow_adoption(
            key,
            parent_session_id=f.binding.runner.session_id,
            parent_workspace_root=str(f.binding.workspace_root),
            updated_at=END,
        )
    assert (await f.store.get_workflow_activity(key)).state is State.RUNNING


async def test_business_json_is_never_source_authority(tmp_path):
    f, key = await ready(tmp_path, activity_source={"kind": "literal", "value": "workflow-dag"})
    with pytest.raises(WorkflowStateError, match="ResultRef"):
        await adapter(f).run_once(key)
    assert (await f.store.get_workflow_activity(key)).state is State.READY
    assert f.mutation.calls == []


@pytest.mark.parametrize("node_state", ["failed", "cancelled", "indeterminate"])
async def test_terminal_but_noncompleted_dag_is_not_adoptable(tmp_path, node_state):
    from contextlib import closing

    from neuro_code.domain.task_dag import TaskDagNodeState

    f, key = await ready(tmp_path)
    with closing(f.store._connect()) as c, c:
        c.execute(
            "UPDATE task_dags SET state = ? WHERE dag_id = ?",
            (TaskDagNodeState(node_state).value, "workflow-dag"),
        )
    with pytest.raises(ResultAdoptionError):
        await adapter(f).run_once(key)
    assert (await f.store.get_workflow_activity(key)).state is State.READY
    assert f.mutation.calls == []


@pytest.mark.parametrize("change", ["lease", "worktree", "checkpoint", "head"])
async def test_live_source_safety_is_not_bypassed(tmp_path, change):
    from contextlib import closing

    f, key = await ready(tmp_path)
    if change == "lease":
        with closing(f.store._connect()) as c, c:
            c.execute("UPDATE writable_subagent_leases SET state = 'orphaned'")
    elif change == "worktree":
        f.worktrees.snapshots["wt-worker-0"] = replace(
            f.worktrees.snapshots["wt-worker-0"], base_commit_sha="f" * 40
        )
    elif change == "checkpoint":
        f.checkpoints.checkpoints.clear()
    else:
        f.parent.repository = replace(f.parent.repository, head_sha="f" * 40)
    with pytest.raises(ResultAdoptionError):
        await adapter(f).run_once(key)
    assert f.mutation.calls == []


async def test_terminal_reconciliation_needs_no_live_projection_lease_or_parent_head(tmp_path):
    f, key = await ready(tmp_path)
    with (
        patch.object(f.store, "reconcile_workflow_adoption", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError),
    ):
        await adapter(f).run_once(key)
    calls = len(f.mutation.calls)
    f.parent.repository = replace(f.parent.repository, head_sha="f" * 40)
    f.worktrees.snapshots.clear()
    f.checkpoints.checkpoints.clear()
    reopened = SqliteSessionStore(f.store.database_path)
    with patch.object(
        reopened, "get_workflow_result_projection", side_effect=AssertionError("live read")
    ):
        result = await adapter(f, store=reopened).run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert len(f.mutation.calls) == calls


async def test_terminal_evidence_tamper_is_rejected_on_recovery_read(tmp_path):
    from contextlib import closing

    f, key = await ready(tmp_path)
    await adapter(f).run_once(key)
    with closing(f.store._connect()) as c, c:
        c.execute("UPDATE result_adoption_targets SET observed_fingerprint = ?", ("f" * 64,))
    with pytest.raises(WorkflowStateError):
        await f.store.get_workflow_activity(key)


async def test_consumed_projection_index_tamper_is_not_source_authority(tmp_path):
    from contextlib import closing

    f, key = await ready(tmp_path)
    with closing(f.store._connect()) as c, c:
        c.execute("DROP TRIGGER workflow_step_outputs_immutable_update")
        c.execute("UPDATE workflow_step_outputs SET source_fingerprint = ?", ("f" * 64,))
    with pytest.raises(WorkflowStateError, match="consumed output"):
        await adapter(f).run_once(key)
    assert f.mutation.calls == []


async def test_reconciliation_settlement_rolls_back_as_one_transaction(tmp_path):
    from contextlib import closing

    f, key = await ready(tmp_path)
    with (
        patch.object(f.store, "reconcile_workflow_adoption", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError),
    ):
        await adapter(f).run_once(key)
    before = await f.store.get_workflow_run("workflow-run")
    request = await f.store.get_workflow_adoption_request(key)
    with closing(f.store._connect()) as c, c:
        c.execute(
            "CREATE TRIGGER fail_adopt_result BEFORE INSERT ON workflow_activity_results BEGIN SELECT RAISE(ABORT, 'crash window'); END"
        )
    with pytest.raises(WorkflowStateError):
        await adapter(f).run_once(key)
    assert await f.store.get_workflow_run("workflow-run") == before
    assert (await f.store.get_workflow_activity(key)).state is State.RUNNING
    assert (await f.store.get_result_adoption(request.adoption_id)).state.terminal
    with closing(f.store._connect()) as c, c:
        c.execute("DROP TRIGGER fail_adopt_result")
    calls = len(f.mutation.calls)
    assert (await adapter(f).run_once(key)).attempt.state is State.COMPLETED
    assert len(f.mutation.calls) == calls


@pytest.mark.parametrize("restart", [False, True])
async def test_operation_ceiling_survives_retry_and_does_not_guess_actual_usage(tmp_path, restart):
    f, key = await ready(tmp_path)

    class RetryableMutation:
        def __init__(self):
            self.calls = 0

        async def apply(self, request, *, session_id):
            self.calls += 1
            raise OSError("retryable port failure before change")

    mutation = RetryableMutation()
    controller = adapter(f, mutation=mutation, max_operations=2)
    assert (await controller.run_once(key)).disposition == "waiting_underlying"
    assert (await controller.run_once(key)).disposition == "waiting_underlying"
    if restart:
        controller = adapter(
            f, store=SqliteSessionStore(f.store.database_path), mutation=mutation, max_operations=2
        )
    with patch(
        "neuro_code.application.workflows.result_adoption.owner_is_alive", return_value=False
    ):
        result = await controller.run_once(key)
    assert result.disposition == "needs_attention"
    assert mutation.calls == 2
    assert (await f.store.get_workflow_activity(key)).state is State.RUNNING
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_wall_ceiling_stops_before_next_mutation(tmp_path):
    from tests.test_result_adoption import _RecordingMutation

    f, key = await ready(tmp_path)
    ticks = [END + timedelta(seconds=1)]

    class ExpiringMutation(_RecordingMutation):
        async def apply(self, request, *, session_id):
            result = await super().apply(request, session_id=session_id)
            ticks[0] += timedelta(milliseconds=101)
            return result

    mutation = ExpiringMutation(f.parent)
    controller = adapter(f, mutation=mutation, max_wall_milliseconds=100)
    controller.clock = lambda: ticks[0]
    result = await controller.run_once(key)
    assert result.disposition == "needs_attention"
    assert len(mutation.calls) == 1
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_claimed_owner_is_not_borrowed(tmp_path):
    f, key = await ready(tmp_path)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="other-owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )
    assert (await adapter(f).run_once(key)).disposition == "busy"
    assert await f.store.get_workflow_activity(key) == claimed
    assert f.mutation.calls == []


async def test_consume_ack_loss_does_not_redispatch_or_reaccount(tmp_path):
    f, key = await ready(tmp_path)
    await adapter(f).run_once(key)
    real = f.store.commit_workflow_step_output

    async def lost_ack(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("lost consume ACK")

    run = await f.store.get_workflow_run("workflow-run")
    with (
        patch.object(f.store, "commit_workflow_step_output", side_effect=lost_ack),
        pytest.raises(RuntimeError),
    ):
        await engine(f.store).advance_once(
            run.run_id,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=END + timedelta(seconds=2),
        )
    persisted = await f.store.get_workflow_run("workflow-run")
    calls = len(f.mutation.calls)
    await adapter(f).run_once(key)
    assert await f.store.get_workflow_run("workflow-run") == persisted
    assert len(f.mutation.calls) == calls


async def test_expired_dispatch_ceiling_allows_only_existing_desired_image_observation(tmp_path):
    from neuro_code.domain.result_adoption import ResultAdoptionState

    f, key = await ready(tmp_path)
    real = f.store.transition_result_adoption

    async def crash_before_complete(record, **kwargs):
        if record.state is ResultAdoptionState.COMPLETED:
            raise asyncio.CancelledError("crash before final ACK")
        return await real(record, **kwargs)

    with (
        patch.object(f.store, "transition_result_adoption", side_effect=crash_before_complete),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter(f).run_once(key)
    calls = len(f.mutation.calls)
    controller = adapter(f, store=SqliteSessionStore(f.store.database_path))
    controller.clock = lambda: END + timedelta(minutes=2)
    with patch(
        "neuro_code.application.workflows.result_adoption.owner_is_alive", return_value=False
    ):
        result = await controller.run_once(key)
    assert result.attempt.state is State.COMPLETED
    assert result.attempt.result.usage.tool_calls is None
    assert len(f.mutation.calls) == calls
