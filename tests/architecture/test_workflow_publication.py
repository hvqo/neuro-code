"""DW3 real SQLite publication invariants; no worker execution."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from neuro_code.application.ports.workflow_publication import WorkflowPublicationStore
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.agents.profile import AgentCapability
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagNodeState, TaskDagState
from neuro_code.domain.workflows import compile_workflow
from neuro_code.domain.workflows.publication import ExpansionMember, WorkflowExpansionIntent
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    StepIdentity,
    WorkflowChange,
    WorkflowFailure,
    WorkflowStepInstance,
)
from neuro_code.domain.workflows.state import (
    WorkflowEventKind as Kind,
)
from neuro_code.domain.workflows.state import (
    WorkflowStatus as Status,
)
from neuro_code.infrastructure.persistence import sqlite_session_workflow_publication as owner
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflows"
NOW = datetime(2026, 10, 6, tzinfo=UTC)
INPUT = "a" * 64


async def setup(tmp_path, *, mapped=False, ceiling=None, two_batches=False):
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    session = await store.create_session("/workspace", "provider", "model")
    data = json.loads(
        (FIXTURES / ("map.json" if mapped else "minimal.json")).read_text(encoding="utf-8")
    )
    if mapped:
        data["input_schema"]["properties"]["targets"]["max_items"] = 8
        data["steps"][0]["max_items"] = 8
        data["steps"][0]["batch"]["max_parallel"] = 4
    if two_batches:
        other = json.loads(json.dumps(data["steps"][0]))
        other["step_id"] = "second"
        data["steps"].append(other)
    definition = compile_workflow(json.dumps(data, ensure_ascii=False))
    await store.insert_workflow_definition(definition)
    run = (
        await store.create_workflow_run(
            "run-1",
            definition_fingerprint=definition.fingerprint,
            parent_session_id=session,
            input_fingerprint=INPUT,
            request_id="create",
            created_at=NOW,
            ceiling=ceiling,
        )
    ).run
    run = (
        await store.claim_workflow_run(
            "run-1",
            expected_generation=0,
            expected_owner_fence=0,
            owner_id="owner-a",
            request_id="claim",
            updated_at=NOW,
        )
    ).run
    return store, run


def intent(
    run, *, mapped=False, count=1, expansion_id="expansion-1", step_id=None, prompt="inspect source"
):
    keys = tuple(f"item-{i:02}" for i in range(count)) if mapped else ("batch",)
    members = tuple(
        ExpansionMember(
            key,
            "work",
            f"node-{i}",
            INPUT,
            "writable_worker",
            (AgentCapability.WORKSPACE_READ, AgentCapability.WORKSPACE_WRITE),
        )
        for i, key in enumerate(keys)
    )
    dag = TaskDag.create(
        dag_id="dag-" + expansion_id,
        parent_session_id=run.parent_session_id,
        nodes=tuple(TaskDagNode(m.node_id, i, prompt) for i, m in enumerate(members)),
        created_at=NOW,
        max_parallel=4 if mapped else 1,
    )
    return WorkflowExpansionIntent(
        expansion_id,
        run.run_id,
        StepIdentity(step_id or ("expand" if mapped else "implement")),
        INPUT,
        members,
        dag,
    )


async def publish(store, run, proposal, *, owner_id=None):
    return await store.publish_workflow_expansion(
        proposal,
        expected_generation=run.generation,
        owner_id=owner_id or run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=max(run.updated_at, NOW + timedelta(seconds=10)),
    )


def counts(store):
    with closing(sqlite3.connect(store.database_path)) as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "task_dags",
                "task_dag_nodes",
                "workflow_expansions",
                "workflow_step_instances",
                "workflow_budget_reservations",
                "workflow_transition_journal",
                "session_tasks",
            )
        )


async def transition(store, run, change):
    return (
        await store.transition_workflow_run(
            run.run_id,
            change,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            request_id=f"transition-{run.generation}",
            updated_at=NOW + timedelta(seconds=20),
        )
    ).run


async def test_single_task_publication_is_one_atomic_durable_fact(tmp_path):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    typed: WorkflowPublicationStore = store
    result = await publish(typed, run, proposal)
    assert result.dag == proposal.dag
    assert result.run.generation == run.generation + 1
    assert result.run.status is Status.WAITING
    assert result.run.position == proposal.step
    assert result.run.steps[0].status is Status.WAITING
    assert result.run.ledger.committed.generated_tasks == 1
    assert result.run.ledger.consumed.generated_tasks == 1
    assert result.run.ledger.reservations[0].reserved == result.run.ledger.reservations[0].consumed
    assert result.expansion.generated_tasks == 1
    assert result.expansion.created_generation == result.run.generation
    journal = await store.get_workflow_journal(run.run_id)
    assert journal[-1].kind is Kind.PUBLISHED
    assert json.loads(journal[-1].payload_json)["generated_tasks"] == 1
    assert counts(store) == (1, 1, 1, 1, 1, 3, 0)
    reopened = SqliteSessionStore(store.database_path)
    assert await reopened.get_workflow_expansion(proposal.expansion_id) == result.expansion
    assert await reopened.get_workflow_run(run.run_id) == result.run
    assert await reopened.get_task_dag(proposal.dag.dag_id) == result.dag
    assert await reopened.get_workflow_expansion("missing") is None


async def test_seven_member_fanout_and_canonical_order(tmp_path):
    store, run = await setup(tmp_path, mapped=True)
    proposal = intent(run, mapped=True, count=7)
    reordered = replace(proposal, members=tuple(reversed(proposal.members)))
    assert reordered == proposal
    assert reordered.member_fingerprint == proposal.member_fingerprint
    assert reordered.identity_fingerprint == proposal.identity_fingerprint
    result = await publish(store, run, reordered)
    assert result.expansion.members == proposal.members
    assert result.expansion.generated_tasks == 7
    assert result.dag.max_parallel == 4
    assert counts(store) == (1, 7, 1, 1, 1, 3, 0)


async def test_more_than_eight_nodes_rejected_before_storage(tmp_path):
    store, run = await setup(tmp_path, mapped=True)
    with pytest.raises(ValueError, match="too many nodes"):
        intent(run, mapped=True, count=9)
    assert counts(store) == (0, 0, 0, 0, 0, 2, 0)


async def test_exact_retry_after_commit_and_restart_returns_original_without_writes(tmp_path):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    first = await publish(store, run, proposal)
    resumed = await SqliteSessionStore(store.database_path).publish_workflow_expansion(
        proposal,
        expected_generation=0,
        owner_id="old-owner",
        owner_fence=0,
        updated_at=NOW + timedelta(seconds=99),
    )
    assert resumed.replayed
    assert resumed.expansion == first.expansion
    assert resumed.dag == first.dag
    assert resumed.run == first.run
    assert counts(store) == (1, 1, 1, 1, 1, 3, 0)


@pytest.mark.parametrize("changed", ["input", "dag", "parallel", "members", "run", "time"])
async def test_same_identity_conflicting_payload_fails_without_writes(tmp_path, changed):
    store, run = await setup(tmp_path, mapped=True)
    proposal = intent(run, mapped=True, count=2)
    first = await publish(store, run, proposal)
    if changed == "input":
        bad = replace(proposal, input_fingerprint="b" * 64)
    elif changed == "dag":
        bad = intent(run, mapped=True, count=2, prompt="different")
    elif changed == "parallel":
        bad = replace(proposal, dag=replace(proposal.dag, max_parallel=2))
    elif changed == "members":
        bad = replace(
            proposal,
            members=tuple(replace(m, input_fingerprint="b" * 64) for m in proposal.members),
        )
    elif changed == "run":
        bad = replace(proposal, run_id="other")
    else:
        bad = replace(
            proposal,
            dag=replace(
                proposal.dag,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
            ),
        )
    with pytest.raises(WorkflowStateError, match="payload conflict"):
        await publish(store, first.run, bad)
    assert counts(store) == (1, 2, 1, 1, 1, 3, 0)


@pytest.mark.parametrize("changed", ["generation", "fence", "owner"])
async def test_stale_publication_is_rejected(tmp_path, changed):
    store, run = await setup(tmp_path)
    kwargs = {
        "expected_generation": run.generation,
        "owner_id": run.owner_id,
        "owner_fence": run.owner_fence,
        "updated_at": NOW,
    }
    kwargs[
        {"generation": "expected_generation", "fence": "owner_fence", "owner": "owner_id"}[changed]
    ] = 0 if changed != "owner" else "owner-b"
    with pytest.raises(WorkflowStateError, match="stale"):
        await store.publish_workflow_expansion(intent(run), **kwargs)
    assert await store.get_workflow_run(run.run_id) == run
    assert counts(store) == (0, 0, 0, 0, 0, 2, 0)


@pytest.mark.parametrize(
    "location",
    [
        "before_dag",
        "after_dag",
        "after_linkage",
        "after_expansion",
        "before_journal",
        "after_journal",
    ],
)
async def test_crash_window_rolls_back_every_table_and_exact_retry_succeeds(tmp_path, location):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    function = {
        "before_dag": "_insert_task_dag",
        "after_dag": "_insert_task_dag",
        "after_linkage": "_save_run",
        "after_expansion": "_insert_expansion",
        "before_journal": "_append_event",
        "after_journal": "_append_event",
    }[location]
    original = getattr(owner, function)

    def interrupted(*args, **kwargs):
        if not location.startswith("before"):
            original(*args, **kwargs)
        raise RuntimeError("crash window")

    with (
        patch.object(owner, function, interrupted),
        pytest.raises(RuntimeError, match="crash window"),
    ):
        await publish(store, run, proposal)
    reopened = SqliteSessionStore(store.database_path)
    assert await reopened.get_workflow_run(run.run_id) == run
    assert counts(store) == (0, 0, 0, 0, 0, 2, 0)
    assert await reopened.get_workflow_expansion(proposal.expansion_id) is None
    assert not (await publish(reopened, run, proposal)).replayed
    assert counts(store) == (1, 1, 1, 1, 1, 3, 0)


def _publisher_process(database, proposal, owner_id, ready, start, output):
    ready.put(owner_id)
    if not start.wait(20):
        output.put("timeout")
        return

    async def work():
        store = SqliteSessionStore(Path(database))
        try:
            result = await store.publish_workflow_expansion(
                proposal,
                expected_generation=1,
                owner_id=owner_id,
                owner_fence=1,
                updated_at=NOW + timedelta(seconds=10),
            )
            output.put("replayed" if result.replayed else "published")
        except WorkflowStateError as error:
            output.put(error.kind)

    asyncio.run(work())


async def test_competing_owner_processes_publish_at_most_one_expansion(tmp_path):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(
            target=_publisher_process,
            args=(str(store.database_path), proposal, name, ready, start, output),
        )
        for name in ("owner-a", "owner-b")
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            await asyncio.to_thread(ready.get, timeout=30)
        start.set()
        results = [await asyncio.to_thread(output.get, timeout=30) for _ in processes]
        assert results.count("published") == 1
        assert set(results) <= {"published", "replayed", "concurrent_modification"}
        for process in processes:
            await asyncio.to_thread(process.join, 30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
        output.close()
    assert counts(store) == (1, 1, 1, 1, 1, 3, 0)


async def test_concurrent_publication_budget_cannot_be_oversold(tmp_path):
    store, run = await setup(tmp_path, two_batches=True, ceiling=BudgetAmounts(generated_tasks=1))
    proposals = (intent(run), intent(run, expansion_id="expansion-2", step_id="second"))
    results = await asyncio.gather(
        *(publish(SqliteSessionStore(store.database_path), run, p) for p in proposals),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert (
        sum(
            isinstance(r, WorkflowStateError) and r.kind == "concurrent_modification"
            for r in results
        )
        == 1
    )
    current = await store.get_workflow_run(run.run_id)
    remaining = (
        proposals[1]
        if await store.get_workflow_expansion(proposals[0].expansion_id)
        else proposals[0]
    )
    with pytest.raises(WorkflowStateError) as raised:
        await publish(store, current, remaining)
    assert raised.value.kind == "budget_exceeded"
    assert current.ledger.consumed.generated_tasks == 1
    assert counts(store) == (1, 1, 1, 1, 1, 3, 0)


async def test_concurrent_existing_reservation_and_publication_share_budget_cas(tmp_path):
    store, run = await setup(tmp_path, ceiling=BudgetAmounts(generated_tasks=1))
    reserve = WorkflowChange(
        Kind.RESERVED, reservation_id="other", amounts=BudgetAmounts(generated_tasks=1)
    )
    results = await asyncio.gather(
        publish(store, run, intent(run)),
        transition(SqliteSessionStore(store.database_path), run, reserve),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    current = await store.get_workflow_run(run.run_id)
    assert current.ledger.committed.generated_tasks == 1
    if counts(store)[0] == 0:
        with pytest.raises(WorkflowStateError) as raised:
            await publish(store, current, intent(current))
        assert raised.value.kind == "budget_exceeded"


async def test_definition_step_and_member_contracts_fail_closed(tmp_path):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    for bad in (
        replace(proposal, step=StepIdentity("missing")),
        replace(proposal, members=(replace(proposal.members[0], task_id="missing"),)),
        replace(proposal, members=(replace(proposal.members[0], member_key="item"),)),
        replace(proposal, dag=replace(proposal.dag, max_parallel=2)),
        replace(proposal, dag=replace(proposal.dag, parent_session_id="other")),
    ):
        with pytest.raises(WorkflowStateError):
            await publish(store, run, bad)
    assert counts(store) == (0, 0, 0, 0, 0, 2, 0)


async def test_member_count_cannot_widen_map_bound(tmp_path):
    store, run = await setup(tmp_path)
    data = json.loads((FIXTURES / "map.json").read_text(encoding="utf-8"))
    data["steps"][0]["batch"]["max_parallel"] = 4
    definition = compile_workflow(json.dumps(data, ensure_ascii=False))
    await store.insert_workflow_definition(definition)
    run = (
        await store.create_workflow_run(
            "run-2",
            definition_fingerprint=definition.fingerprint,
            parent_session_id=run.parent_session_id,
            input_fingerprint=INPUT,
            request_id="create",
            created_at=NOW,
        )
    ).run
    run = (
        await store.claim_workflow_run(
            "run-2",
            expected_generation=0,
            expected_owner_fence=0,
            owner_id="owner-a",
            request_id="claim",
            updated_at=NOW,
        )
    ).run
    with pytest.raises(WorkflowStateError, match="member bound"):
        await publish(store, run, intent(run, mapped=True, count=7))
    assert counts(store)[:5] == (0, 0, 0, 0, 0)


async def test_dag_identity_and_step_slot_cannot_be_reused(tmp_path):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    first = await publish(store, run, proposal)
    with pytest.raises(WorkflowStateError, match="already published"):
        await publish(store, first.run, intent(first.run, expansion_id="other"))
    other_store, other_run = await setup(tmp_path / "other")
    await other_store.insert_task_dag(intent(other_run).dag)
    with pytest.raises(WorkflowStateError, match="new DAG identity"):
        await publish(other_store, other_run, intent(other_run))
    assert counts(other_store) == (1, 1, 0, 0, 0, 2, 0)


async def test_waiting_step_blocks_completion_and_publication_does_not_execute(tmp_path):
    store, run = await setup(tmp_path)
    result = await publish(store, run, intent(run))
    with pytest.raises(WorkflowStateError, match="unresolved"):
        await transition(
            store, result.run, WorkflowChange(Kind.TRANSITION, status=Status.COMPLETED)
        )
    assert result.dag.state is TaskDagState.READY
    assert result.dag.nodes[0].state is TaskDagNodeState.READY
    assert result.dag.nodes[0].parent_task_id is None
    assert result.dag.nodes[0].execution_owner_token is None
    assert counts(store)[-1] == 0


@pytest.mark.parametrize("status", [Status.CANCELLED, Status.FAILED, Status.NEEDS_ATTENTION])
async def test_terminal_or_uncertain_run_cannot_publish(tmp_path, status):
    store, run = await setup(tmp_path)
    run = await transition(
        store,
        run,
        WorkflowChange(
            Kind.TRANSITION,
            status=status,
            failure=WorkflowFailure("unknown", "requires review")
            if status is not Status.CANCELLED
            else None,
        ),
    )
    with pytest.raises(WorkflowStateError, match="current state"):
        await publish(store, run, intent(run))
    assert counts(store)[:5] == (0, 0, 0, 0, 0)


async def test_existing_step_input_and_state_must_match(tmp_path):
    store, run = await setup(tmp_path)
    initial = WorkflowStepInstance(StepIdentity("implement"), Status.READY, INPUT)
    run = await transition(store, run, WorkflowChange(Kind.STEP, step=initial))
    with pytest.raises(WorkflowStateError, match="step input/state"):
        await publish(store, run, replace(intent(run), input_fingerprint="b" * 64))
    result = await publish(store, run, intent(run))
    assert len(result.run.steps) == 1


async def test_immutable_expansion_and_linkage_corruption_detection(tmp_path):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    result = await publish(store, run, proposal)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        for sql in (
            "UPDATE workflow_expansions SET generated_tasks=2",
            "DELETE FROM workflow_expansions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql)
        connection.execute("DROP TRIGGER workflow_expansions_immutable_update")
        connection.execute("UPDATE workflow_expansions SET generated_tasks=2")
    with pytest.raises(WorkflowStateError, match="linkage mismatch"):
        await store.get_workflow_expansion(result.expansion.expansion_id)


async def test_replay_after_dag_lifecycle_change_preserves_original_definition(tmp_path):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    first = await publish(store, run, proposal)
    updated = replace(
        first.dag, state=TaskDagState.RUNNING, generation=1, updated_at=NOW + timedelta(seconds=11)
    )
    await store.compare_and_transition_task_dag(
        updated, expected_generation=0, expected_state=TaskDagState.READY
    )
    replay = await publish(store, run, proposal)
    assert replay.replayed
    assert replay.dag.state is TaskDagState.RUNNING
    assert replay.dag.definition_fingerprint == first.dag.definition_fingerprint
    assert replay.run == first.run
    assert counts(store) == (1, 1, 1, 1, 1, 3, 0)


async def test_migration_36_to_37_preserves_dw2_state(tmp_path):
    store, run = await setup(tmp_path)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TABLE workflow_expansions")
        connection.execute("UPDATE schema_meta SET version=36")
    await asyncio.gather(store.initialize(), SqliteSessionStore(store.database_path).initialize())
    assert await store.get_workflow_run(run.run_id) == run
    assert SCHEMA_VERSION == 41
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (SCHEMA_VERSION,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_migration_failure_rolls_back_ddl_and_version(tmp_path):
    store, run = await setup(tmp_path)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TABLE workflow_expansions")
        connection.execute("UPDATE schema_meta SET version=36")

    def interrupted(connection):
        connection.execute("CREATE TABLE workflow_partial(value TEXT)")
        raise RuntimeError("crash")

    with (
        patch(
            "neuro_code.infrastructure.persistence.sqlite_session_core._ensure_workflow_publication_schema",
            interrupted,
        ),
        pytest.raises(RuntimeError, match="crash"),
    ):
        await store.initialize()
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (36,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='workflow_partial'"
            ).fetchone()
            is None
        )
    assert await store.get_workflow_run(run.run_id) == run
    await store.initialize()


async def test_eight_maximum_size_prompts_use_bounded_journal_fact(tmp_path):
    store, run = await setup(tmp_path, mapped=True)
    proposal = intent(run, mapped=True, count=8, prompt="x" * 8192)
    result = await publish(store, run, proposal)
    assert result.expansion.generated_tasks == 8
    event = (await store.get_workflow_journal(run.run_id))[-1]
    assert len(event.payload_json.encode("utf-8")) < 32768
    assert result.dag == proposal.dag


async def test_full_template_coverage_and_dependencies_within_each_fanout_item(tmp_path):
    store, run = await setup(tmp_path)
    data = json.loads((FIXTURES / "map.json").read_text(encoding="utf-8"))
    batch = data["steps"][0]["batch"]
    batch["tasks"][0]["task_id"] = "alpha"
    tail = json.loads(json.dumps(batch["tasks"][0]))
    tail["task_id"] = "zeta"
    tail["depends_on"] = ["alpha"]
    batch["tasks"].append(tail)
    batch["max_parallel"] = 2
    definition = compile_workflow(json.dumps(data))
    await store.insert_workflow_definition(definition)
    run = (
        await store.create_workflow_run(
            "run-2",
            definition_fingerprint=definition.fingerprint,
            parent_session_id=run.parent_session_id,
            input_fingerprint=INPUT,
            request_id="create",
            created_at=NOW,
        )
    ).run
    run = (
        await store.claim_workflow_run(
            run.run_id,
            expected_generation=0,
            expected_owner_fence=0,
            owner_id="owner-a",
            request_id="claim",
            updated_at=NOW,
        )
    ).run
    members = tuple(
        ExpansionMember(
            key,
            task,
            key + ":" + task,
            INPUT,
            "writable_worker",
            (AgentCapability.WORKSPACE_READ, AgentCapability.WORKSPACE_WRITE),
        )
        for key in ("中文", "目标")
        for task in ("alpha", "zeta")
    )
    nodes = tuple(
        TaskDagNode(
            m.node_id, i, "inspect", (m.member_key + ":alpha",) if m.task_id == "zeta" else ()
        )
        for i, m in enumerate(members)
    )
    dag = TaskDag.create(
        dag_id="dag-two",
        parent_session_id=run.parent_session_id,
        nodes=nodes,
        created_at=NOW,
        max_parallel=2,
    )
    proposal = WorkflowExpansionIntent(
        "two", run.run_id, StepIdentity("expand"), INPUT, members, dag
    )
    wrong_nodes = tuple(
        replace(n, dependencies=("中文:alpha",)) if n.node_id == "目标:zeta" else n
        for n in dag.nodes
    )
    with pytest.raises(WorkflowStateError, match="dependencies"):
        await publish(store, run, replace(proposal, dag=replace(dag, nodes=wrong_nodes)))
    incomplete = WorkflowExpansionIntent(
        "two",
        run.run_id,
        proposal.step,
        INPUT,
        members[:1],
        TaskDag.create(
            dag_id=dag.dag_id,
            parent_session_id=dag.parent_session_id,
            nodes=(nodes[0],),
            created_at=NOW,
        ),
    )
    with pytest.raises(WorkflowStateError, match="cover declared tasks"):
        await publish(store, run, incomplete)
    result = await publish(store, run, proposal)
    assert result.dag.ready_node_ids() == ("中文:alpha", "目标:alpha")
    assert result.run.ledger.consumed.generated_tasks == 4


@pytest.mark.parametrize("field", ["generation", "state", "result", "updated_at"])
async def test_intent_rejects_nonfresh_dag_projection(tmp_path, field):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    changes = {
        "generation": {"generation": 1},
        "state": {"state": TaskDagState.RUNNING},
        "result": {
            "nodes": (replace(proposal.dag.nodes[0], response_preview="untrusted completed text"),)
        },
        "updated_at": {"updated_at": NOW + timedelta(seconds=1)},
    }
    with pytest.raises(ValueError, match="fresh immutable"):
        replace(proposal, dag=replace(proposal.dag, **changes[field]))
    assert counts(store) == (0, 0, 0, 0, 0, 2, 0)


async def test_existing_unknown_accounting_prevents_publication(tmp_path):
    store, run = await setup(tmp_path)
    run = await transition(
        store,
        run,
        WorkflowChange(
            Kind.RESERVED, reservation_id="unknown", amounts=BudgetAmounts(tool_calls=1)
        ),
    )
    run = await transition(
        store,
        run,
        WorkflowChange(
            Kind.CONSUMED, reservation_id="unknown", amounts=BudgetAmounts(tool_calls=None)
        ),
    )
    with pytest.raises(WorkflowStateError, match="current state"):
        await publish(store, run, intent(run))
    assert run.ledger.consumed.tool_calls is None
    assert counts(store)[:4] == (0, 0, 0, 0)


async def test_generation_reclaim_fences_previous_owner_publication(tmp_path):
    store, run = await setup(tmp_path)
    new = (
        await store.claim_workflow_run(
            run.run_id,
            expected_generation=run.generation,
            expected_owner_fence=run.owner_fence,
            owner_id="owner-b",
            request_id="reclaim",
            updated_at=NOW,
        )
    ).run
    with pytest.raises(WorkflowStateError, match="stale"):
        await publish(store, replace(run, generation=new.generation), intent(run))
    assert new.status is Status.NEEDS_ATTENTION
    assert counts(store)[:5] == (0, 0, 0, 0, 0)


async def test_publication_has_no_second_transaction_api(tmp_path):
    store, run = await setup(tmp_path)
    with patch.object(
        store, "insert_task_dag", side_effect=AssertionError("public DAG API called")
    ):
        result = await publish(store, run, intent(run))
    assert result.expansion.generated_tasks == 1
    with closing(store._connect()) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("DELETE FROM workflow_transition_journal WHERE generation=2")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _crash_process(database, proposal, after_insert):
    import os

    original = owner._insert_task_dag

    def crash(connection, dag):
        if after_insert:
            original(connection, dag)
        os._exit(67)

    owner._insert_task_dag = crash

    async def work():
        store = SqliteSessionStore(Path(database))
        await store.publish_workflow_expansion(
            proposal,
            expected_generation=1,
            owner_id="owner-a",
            owner_fence=1,
            updated_at=NOW + timedelta(seconds=10),
        )

    asyncio.run(work())


@pytest.mark.parametrize("after_insert", [False, True])
async def test_real_process_death_leaves_no_orphan_publication(tmp_path, after_insert):
    store, run = await setup(tmp_path)
    proposal = intent(run)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_process, args=(str(store.database_path), proposal, after_insert)
    )
    process.start()
    try:
        await asyncio.to_thread(process.join, 30)
        assert not process.is_alive()
        assert process.exitcode == 67
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
    reopened = SqliteSessionStore(store.database_path)
    assert await reopened.get_workflow_run(run.run_id) == run
    assert counts(reopened) == (0, 0, 0, 0, 0, 2, 0)
    assert not (await publish(reopened, run, proposal)).replayed
