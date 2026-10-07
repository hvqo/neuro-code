"""DW4b real SQLite provenance, shared adoption safety and recovery contracts."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from neuro_code.application.ports.result_adoption import ResultAdoptionError
from neuro_code.application.workflows.completed_dag_adoption import (
    WorkflowCompletedDagSourceAdapter,
)
from neuro_code.application.workflows.result_adoption import ResultAdoptionApplicationService
from neuro_code.domain.agents.profile import AgentCapability
from neuro_code.domain.completed_dag_adoption import (
    CompletedDagAdoptionSource,
    CompletedDagSourceKind,
    WorkflowAdoptionSourceRef,
)
from neuro_code.domain.conversation.messages import Role
from neuro_code.domain.parent_context_relay import ParentContextRelay, ParentContextRelayItem
from neuro_code.domain.result_adoption import (
    ResultAdoptionPlan,
    ResultAdoptionRequest,
    ResultAdoptionState,
)
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.task_dag import (
    TaskDag,
    TaskDagNode,
)
from neuro_code.domain.task_dag import (
    TaskDagNodeState as NodeState,
)
from neuro_code.domain.task_dag import (
    TaskDagState as DagState,
)
from neuro_code.domain.task_dag_result import TaskDagResultEvidence
from neuro_code.domain.workflows import compile_workflow
from neuro_code.domain.workflows.publication import ExpansionMember, WorkflowExpansionIntent
from neuro_code.domain.workflows.state import StepIdentity
from neuro_code.domain.writable_subagent import WritableSubagentLeaseScope
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.infrastructure.persistence.sqlite_session_subagents import (
    _parent_context_relay_values,
    _writable_lease_values,
)
from tests.architecture.test_workflow_publication import INPUT, NOW
from tests.test_result_adoption import _make_fixture, _RecordingMutation

END = NOW + timedelta(seconds=30)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflows"


def service(f, *, store=None, mutation=None):
    store = store or f.store
    return ResultAdoptionApplicationService(
        store=store,
        dags=store,
        leases=store,
        source_adapter=WorkflowCompletedDagSourceAdapter(
            projections=store, dags=store, leases=store
        ),
        worktrees=f.worktrees,
        checkpoints=f.checkpoints,
        parent_reader=f.parent,
        mutation=mutation or f.mutation,
        parent_binding=f.binding,
    )


async def fixture(tmp_path, *, mapped=False, state=NodeState.COMPLETED, **kwargs):
    f = await _make_fixture(tmp_path, **kwargs)
    data = json.loads(
        (FIXTURES / ("map.json" if mapped else "minimal.json")).read_text(encoding="utf-8")
    )
    if not mapped:
        task = data["steps"][0]["tasks"][0]
        data["steps"][0]["tasks"] = [dict(task, task_id=f"work-{i}") for i in range(2)]
    definition = compile_workflow(json.dumps(data))
    await f.store.insert_workflow_definition(definition)
    run = (
        await f.store.create_workflow_run(
            "workflow-run",
            definition_fingerprint=definition.fingerprint,
            parent_session_id=f.binding.runner.session_id,
            input_fingerprint=INPUT,
            request_id="create",
            created_at=NOW,
        )
    ).run
    run = (
        await f.store.claim_workflow_run(
            run.run_id,
            expected_generation=run.generation,
            expected_owner_fence=0,
            owner_id="workflow-owner",
            request_id="claim",
            updated_at=NOW,
        )
    ).run
    members = tuple(
        ExpansionMember(
            f"item-{i}" if mapped else "batch",
            "work" if mapped else f"work-{i}",
            n.node_id,
            INPUT,
            "writable_worker",
            (AgentCapability.WORKSPACE_READ, AgentCapability.WORKSPACE_WRITE),
        )
        for i, n in enumerate(f.graph.dag.nodes)
    )
    dag = TaskDag.create(
        dag_id="workflow-dag",
        parent_session_id=run.parent_session_id,
        nodes=tuple(TaskDagNode(n.node_id, i, n.prompt) for i, n in enumerate(f.graph.dag.nodes)),
        created_at=NOW,
        max_parallel=1,
    )
    await f.store.publish_workflow_expansion(
        WorkflowExpansionIntent(
            "workflow-expansion",
            run.run_id,
            StepIdentity("expand" if mapped else "implement"),
            INPUT,
            members,
            dag,
        ),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=NOW,
    )
    dag = await f.store.compare_and_transition_task_dag(
        replace(dag, state=DagState.RUNNING, generation=1, updated_at=END),
        expected_generation=0,
        expected_state=DagState.READY,
    )
    for node in dag.nodes:
        original = f.graph.dag.node(node.node_id)
        child = await f.store.create_session("/worker", "fixture", "model")
        lease = replace(
            f.graph.leases[original.lease_id],
            child_session_id=child,
            execution_scope=WritableSubagentLeaseScope.TASK_DAG,
        )
        f.graph.leases[lease.lease_id] = lease
        task = SessionTask(
            lease.parent_task_id, SessionTaskKind.SUBAGENT, SessionTaskStatus.COMPLETED, NOW, END
        )
        await f.store.create_session_task(run.parent_session_id, task)
        relay = ParentContextRelay.create(
            relay_id=original.relay_id,
            parent_session_id=run.parent_session_id,
            parent_task_id=lease.parent_task_id,
            child_session_id=child,
            lease_id=lease.lease_id,
            worktree_id=lease.worktree_id,
            baseline_checkpoint_id=lease.baseline_checkpoint_id,
            base_commit_sha=lease.base_commit_sha,
            capability_fingerprint=lease.capability_fingerprint,
            grant_fingerprint=lease.grant_fingerprint,
            task_prompt_fingerprint=node.prompt_fingerprint,
            source_item_count=1,
            items=(ParentContextRelayItem(0, Role.USER, "bounded context", False),),
            truncated=False,
            created_at=NOW,
        )
        with closing(f.store._connect()) as c, c:
            c.execute(
                "INSERT INTO writable_subagent_leases VALUES ("
                + ",".join("?" for _ in range(33))
                + ")",
                _writable_lease_values(lease),
            )
            c.execute(
                "INSERT INTO parent_context_relays VALUES ("
                + ",".join("?" for _ in range(20))
                + ")",
                (*_parent_context_relay_values(relay), "ready"),
            )
        ready = node
        if node.state is NodeState.PENDING:
            ready = replace(node, state=NodeState.READY, generation=node.generation + 1)
            await f.store.compare_and_transition_task_dag_node(
                dag.dag_id,
                ready,
                expected_generation=node.generation,
                expected_state=node.state,
            )
        running = replace(
            ready,
            state=NodeState.RUNNING,
            generation=ready.generation + 1,
            parent_task_id=lease.parent_task_id,
        )
        dag = await f.store.claim_task_dag_node(
            dag.dag_id,
            running,
            expected_generation=ready.generation,
            expected_state=ready.state,
            updated_at=END,
        )
        terminal = replace(
            original, state=state, generation=running.generation + 1, child_session_id=child
        )
        dag = await f.store.finish_task_dag_node(
            dag.dag_id,
            terminal,
            expected_generation=running.generation,
            expected_state=running.state,
            updated_at=END,
            result_evidence=TaskDagResultEvidence(
                lease.parent_task_id, child, "Exact result, not verification PASS"
            ),
        )
    target = {
        NodeState.COMPLETED: DagState.COMPLETED,
        NodeState.FAILED: DagState.FAILED,
        NodeState.CANCELLED: DagState.CANCELLED,
        NodeState.INDETERMINATE: DagState.INDETERMINATE,
    }[state]
    dag = await f.store.compare_and_transition_task_dag(
        replace(dag, state=target, generation=dag.generation + 1, updated_at=END),
        expected_generation=dag.generation,
        expected_state=dag.state,
    )
    projection = await f.store.project_workflow_result(
        "workflow-expansion",
        run_id=run.run_id,
        parent_session_id=run.parent_session_id,
        created_at=END,
    )
    request = ResultAdoptionRequest(
        "adopt-workflow",
        workflow_source=WorkflowAdoptionSourceRef(
            run.run_id, projection.expansion_id, projection.projection_id, projection.fingerprint
        ),
    )
    return f, request, projection


@pytest.mark.parametrize("mapped", [False, True])
async def test_completed_workflow_adopts_using_shared_core_without_advancing_workflow(
    tmp_path, mapped
):
    f, request, projection = await fixture(tmp_path, mapped=mapped)
    before = await f.store.get_workflow_run(request.workflow_source.run_id)
    result = await service(f).adopt(request)
    assert result.state is ResultAdoptionState.COMPLETED
    assert result.parent_workspace_changed
    assert result.plan.source.kind is CompletedDagSourceKind.WORKFLOW
    assert result.plan.source.workflow.projection_fingerprint == projection.fingerprint
    assert result.plan.swarm_run_id is None
    assert "swarm_run_id" not in result.plan.to_dict()
    assert f.parent.current("U.txt").content == b"unrelated dirty\n"
    assert await f.store.get_workflow_run(request.workflow_source.run_id) == before
    count = len(f.mutation.calls)
    reopened = SqliteSessionStore(f.store.database_path)
    await reopened.initialize()
    replay = await service(f, store=reopened).adopt(request)
    assert replay.plan_fingerprint == result.plan_fingerprint
    assert len(f.mutation.calls) == count == 2
    with closing(reopened._connect()) as c:
        assert c.execute("SELECT count(*) FROM orchestration_swarm_runs").fetchone()[0] == 0


@pytest.mark.parametrize("option", ["overlap", "parent_conflict", "stale_worker"])
async def test_workflow_reuses_existing_workspace_safety(tmp_path, option):
    f, request, _ = await fixture(tmp_path, **{option: True})
    if option == "parent_conflict":
        result = await service(f).adopt(request)
        assert result.state is ResultAdoptionState.CONFLICT
    else:
        with pytest.raises(ResultAdoptionError):
            await service(f).adopt(request)
    assert f.mutation.calls == []


@pytest.mark.parametrize("state", [NodeState.FAILED, NodeState.CANCELLED, NodeState.INDETERMINATE])
async def test_noncompleted_projection_cannot_authorize_adoption(tmp_path, state):
    f, request, _ = await fixture(tmp_path, state=state)
    with pytest.raises(ResultAdoptionError, match="completed DAG"):
        await service(f).adopt(request)
    assert not f.mutation.calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "wrong-run"),
        ("expansion_id", "wrong-expansion"),
        ("projection_id", "wrong-projection"),
        ("projection_fingerprint", "f" * 64),
    ],
)
async def test_wrong_workflow_reference_rejected_even_for_existing_adoption(tmp_path, field, value):
    f, request, _ = await fixture(tmp_path)
    await service(f).adopt(request)
    count = len(f.mutation.calls)
    with pytest.raises(ResultAdoptionError):
        await service(f).adopt(
            replace(request, workflow_source=replace(request.workflow_source, **{field: value}))
        )
    assert len(f.mutation.calls) == count


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", 99),
        ("final_workspace_fingerprint", "a" * 64),
        ("child_session_id", "wrong-child"),
    ],
)
async def test_durable_node_tamper_fails_closed(tmp_path, field, value):
    f, request, _ = await fixture(tmp_path)
    with closing(f.store._connect()) as c, c:
        c.execute(
            f"UPDATE task_dag_nodes SET {field} = ? WHERE dag_id = ? AND ordinal = 0",
            (value, "workflow-dag"),
        )
    with pytest.raises(ResultAdoptionError):
        await service(f).adopt(request)
    assert not f.mutation.calls


@pytest.mark.parametrize(
    "change", ["lease", "checkpoint", "worktree", "parent_head", "parent_root"]
)
async def test_stale_workspace_bindings_rejected(tmp_path, change):
    f, request, _ = await fixture(tmp_path)
    if change == "lease":
        with closing(f.store._connect()) as c, c:
            c.execute(
                "UPDATE writable_subagent_leases SET state = 'orphaned' WHERE lease_id = ?",
                ("lease-worker-0",),
            )
    elif change == "checkpoint":
        f.checkpoints.checkpoints.clear()
    elif change == "worktree":
        key = "wt-worker-0"
        f.worktrees.snapshots[key] = replace(f.worktrees.snapshots[key], base_commit_sha="f" * 40)
    elif change == "parent_head":
        f.parent.repository = replace(f.parent.repository, head_sha="f" * 40)
    else:
        f.parent.repository = replace(f.parent.repository, source_worktree=tmp_path / "wrong")

        async def inspect(root):
            return f.parent.snapshot()

        f.parent.inspect = inspect
    with pytest.raises(ResultAdoptionError):
        await service(f).adopt(request)
    assert not f.mutation.calls


async def test_crash_window_uses_existing_forward_recovery(tmp_path):
    class SimulatedCrash(BaseException):
        pass

    class CrashMutation(_RecordingMutation):
        crash = True

        async def apply(self, request, *, session_id):
            result = await super().apply(request, session_id=session_id)
            if self.crash:
                self.crash = False
                raise SimulatedCrash
            return result

    f, request, _ = await fixture(tmp_path)
    mutation = CrashMutation(f.parent)
    controller = service(f, mutation=mutation)
    controller._owner_pid = 2_147_483_647
    with pytest.raises(SimulatedCrash):
        await controller.adopt(request)
    initial = await f.store.get_result_adoption(request.adoption_id)
    assert initial.state is ResultAdoptionState.APPLYING
    assert len(mutation.calls) == 1
    reopened = SqliteSessionStore(f.store.database_path)
    await reopened.initialize()
    resumed = await service(f, store=reopened, mutation=mutation).adopt(request)
    assert resumed.state is ResultAdoptionState.COMPLETED
    assert [call.path for call in mutation.calls] == ["A.txt", "C.txt"]


async def test_workflow_and_swarm_cannot_share_adoption_identity(tmp_path):
    f, request, _ = await fixture(tmp_path)
    await service(f).prepare(request)
    collision = ResultAdoptionRequest(request.adoption_id, f.graph.swarm.swarm_run_id)
    with pytest.raises(ResultAdoptionError, match="different parent"):
        await f.service.adopt(collision)


async def test_legacy_swarm_json_and_fingerprint_survive_reopen_without_rewrite(tmp_path):
    f = await _make_fixture(tmp_path)
    record = await f.service.prepare(f.request)
    legacy_json = json.dumps(
        record.plan.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    assert set(record.plan.to_dict()) == {
        "adoption_id",
        "parent_session_id",
        "parent_workspace_root",
        "parent_repository",
        "parent_head_sha",
        "swarm_run_id",
        "dag_id",
        "dag_generation",
        "dag_definition_fingerprint",
        "sources",
        "targets",
        "created_at",
    }
    assert "completed_source" not in legacy_json
    reopened = SqliteSessionStore(f.store.database_path)
    await reopened.initialize()
    loaded = await reopened.get_result_adoption(record.adoption_id)
    assert loaded.plan.fingerprint == record.plan.fingerprint
    assert loaded.plan.to_dict() == json.loads(legacy_json)
    assert loaded.plan.source.kind is CompletedDagSourceKind.SWARM
    with closing(reopened._connect()) as c:
        assert c.execute("SELECT plan_json, plan_fingerprint FROM result_adoptions").fetchone() == (
            legacy_json,
            record.plan.fingerprint,
        )
        assert (
            c.execute("SELECT version FROM schema_meta WHERE singleton = 1").fetchone()[0]
            == SCHEMA_VERSION
        )


async def test_typed_source_plan_roundtrip_and_invalid_mixed_identities(tmp_path):
    f, request, _ = await fixture(tmp_path)
    record = await service(f).prepare(request)
    assert ResultAdoptionPlan.from_dict(record.plan.to_dict()) == record.plan
    with pytest.raises(ValueError, match="completed source"):
        replace(record.plan, swarm_run_id="fake-swarm")
    with pytest.raises(ValueError, match="exactly one"):
        replace(request, swarm_run_id="fake-swarm")
    raw = record.plan.completed_source.to_dict()
    raw["extra"] = 1
    with pytest.raises(ValueError, match="fields"):
        CompletedDagAdoptionSource.from_dict(raw)


async def test_wrong_parent_session_rejected_before_any_plan_or_mutation(tmp_path):
    from tests.test_result_adoption import _ParentRunner

    f, request, _ = await fixture(tmp_path)
    other = await f.store.create_session(str(f.binding.workspace_root), "fixture", "model")
    f.binding = replace(f.binding, runner=_ParentRunner(other))
    with pytest.raises(ResultAdoptionError, match="integrity"):
        await service(f).adopt(request)
    assert await f.store.get_result_adoption(request.adoption_id) is None
    assert not f.mutation.calls


@pytest.mark.parametrize("prepared", [False, True])
async def test_exact_dag_drift_rejected_on_creation_and_replay(tmp_path, prepared):
    f, request, _ = await fixture(tmp_path)
    if prepared:
        await service(f).prepare(request)
    with closing(f.store._connect()) as c, c:
        c.execute(
            "UPDATE task_dags SET generation = generation + 1 WHERE dag_id = ?", ("workflow-dag",)
        )
    with pytest.raises(ResultAdoptionError):
        await service(f).adopt(request)
    assert not f.mutation.calls


async def test_completed_replay_returns_durable_fact_after_parent_commit(tmp_path):
    f, request, _ = await fixture(tmp_path)
    completed = await service(f).adopt(request)
    calls = len(f.mutation.calls)
    f.parent.repository = replace(f.parent.repository, head_sha="f" * 40)
    reopened = SqliteSessionStore(f.store.database_path)
    await reopened.initialize()
    replay = await service(f, store=reopened).adopt(request)
    assert replay == completed
    assert len(f.mutation.calls) == calls


@pytest.mark.parametrize("terminal", [state for state in ResultAdoptionState if state.terminal])
async def test_terminal_replay_needs_no_live_source_or_parent(tmp_path, monkeypatch, terminal):
    f, request, _ = await fixture(tmp_path)
    controller = service(f)
    if terminal is ResultAdoptionState.COMPLETED:
        original = await controller.adopt(request)
    else:
        prepared = await controller.prepare(request)
        original = await controller._terminate(
            prepared, terminal, ResultAdoptionError("terminal fact", kind="conflict")
        )
    calls = len(f.mutation.calls)
    # Resource cleanup is legitimate after terminal adoption. It cannot revoke
    # the durable historical fact or make replay authorize another mutation.
    with closing(f.store._connect()) as c, c:
        c.execute("UPDATE writable_subagent_leases SET state = 'orphaned'")
    f.worktrees.snapshots.clear()
    f.checkpoints.checkpoints.clear()
    reopened = SqliteSessionStore(f.store.database_path)
    await reopened.initialize()
    replay_controller = service(f, store=reopened)

    async def unavailable(*args, **kwargs):
        raise AssertionError("terminal replay must not inspect live resources")

    monkeypatch.setattr(replay_controller._source_adapter, "resolve", unavailable)
    monkeypatch.setattr(reopened, "get_workflow_result_projection", unavailable)
    monkeypatch.setattr(f.parent, "inspect", unavailable)
    assert await replay_controller.prepare(request) == original
    assert await replay_controller.adopt(request) == original
    assert len(f.mutation.calls) == calls


@pytest.mark.parametrize("state", [state for state in ResultAdoptionState if not state.terminal])
@pytest.mark.parametrize("change", ["lease", "parent_head"])
async def test_nonterminal_replay_still_revalidates_source_and_parent(tmp_path, state, change):
    f, request, _ = await fixture(tmp_path)
    controller = service(f)
    record = await controller.prepare(request)
    for next_state in (
        ResultAdoptionState.VERIFIED,
        ResultAdoptionState.APPLYING,
        ResultAdoptionState.VERIFYING,
    ):
        if record.state is state:
            break
        record = await controller._transition_adoption(record, next_state)
    assert record.state is state
    if change == "lease":
        with closing(f.store._connect()) as c, c:
            c.execute("UPDATE writable_subagent_leases SET state = 'orphaned'")
    else:
        f.parent.repository = replace(f.parent.repository, head_sha="f" * 40)
    reopened = SqliteSessionStore(f.store.database_path)
    await reopened.initialize()
    with pytest.raises(ResultAdoptionError):
        await service(f, store=reopened).adopt(request)
    assert await reopened.get_result_adoption(request.adoption_id) == record
    assert not f.mutation.calls


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("tamper", ["plan", "fingerprint"])
async def test_workflow_plan_provenance_tamper_rejected_after_reopen(tmp_path, completed, tamper):
    f, request, _ = await fixture(tmp_path)
    controller = service(f)
    if completed:
        await controller.adopt(request)
    else:
        await controller.prepare(request)
    calls = len(f.mutation.calls)
    with closing(f.store._connect()) as c, c:
        if tamper == "plan":
            raw = json.loads(c.execute("SELECT plan_json FROM result_adoptions").fetchone()[0])
            raw["completed_source"]["projection_source_fingerprint"] = "f" * 64
            c.execute("UPDATE result_adoptions SET plan_json = ?", (json.dumps(raw),))
        else:
            c.execute("UPDATE result_adoptions SET plan_fingerprint = ?", ("f" * 64,))
    reopened = SqliteSessionStore(f.store.database_path)
    await reopened.initialize()
    with pytest.raises(ResultAdoptionError, match="integrity"):
        await service(f, store=reopened).adopt(request)
    assert len(f.mutation.calls) == calls


@pytest.mark.parametrize("change", ["session", "root"])
async def test_terminal_replay_rejects_wrong_parent_binding(tmp_path, change):
    from tests.test_result_adoption import _ParentRunner

    f, request, _ = await fixture(tmp_path)
    completed = await service(f).adopt(request)
    calls = len(f.mutation.calls)
    if change == "session":
        other = await f.store.create_session(str(f.binding.workspace_root), "fixture", "model")
        f.binding = replace(f.binding, runner=_ParentRunner(other))
    else:
        root = tmp_path / "other-parent"
        f.binding = replace(
            f.binding,
            workspace_root=root,
            capabilities=replace(f.binding.capabilities, cwd=root, workspace_roots=(root,)),
        )
    with pytest.raises(ResultAdoptionError, match="different parent"):
        await service(f).adopt(request)
    assert await f.store.get_result_adoption(request.adoption_id) == completed
    assert len(f.mutation.calls) == calls


async def test_concurrent_workflow_prepare_reuses_one_plan_with_distinct_clocks(tmp_path):
    import asyncio

    f, request, _ = await fixture(tmp_path)

    class BarrierStore:
        def __init__(self):
            self.readers = 0
            self.ready = asyncio.Event()

        def __getattr__(self, name):
            return getattr(f.store, name)

        async def get_result_adoption(self, adoption_id):
            record = await f.store.get_result_adoption(adoption_id)
            if record is None:
                self.readers += 1
                if self.readers == 2:
                    self.ready.set()
                await self.ready.wait()
            return record

    store = BarrierStore()
    first, second = service(f, store=store), service(f, store=store)
    first._clock = lambda: END
    second._clock = lambda: END + timedelta(seconds=1)
    results = await asyncio.wait_for(
        asyncio.gather(first.prepare(request), second.prepare(request)), timeout=10
    )
    assert results[0].plan_fingerprint == results[1].plan_fingerprint
    assert not f.mutation.calls
    with closing(f.store._connect()) as c:
        assert c.execute("SELECT count(*) FROM result_adoptions").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM result_adoption_targets").fetchone()[0] == 2
