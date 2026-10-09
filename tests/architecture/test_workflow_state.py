"""DW2 database consistency contracts, including real competing processes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from neuro_code.application.ports.workflow_state import WorkflowStateError, WorkflowStateStore
from neuro_code.domain.workflows import ArtifactRef, compile_workflow
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
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflows"
NOW = datetime(2026, 10, 6, tzinfo=UTC)
INPUT = "a" * 64


def _claim_process(database, owner, ready, start, output):
    ready.put(owner)
    if not start.wait(10):
        output.put("timeout")
        return

    async def claim():
        try:
            store = SqliteSessionStore(Path(database))
            result = await store.claim_workflow_run(
                "run-1",
                expected_generation=0,
                expected_owner_fence=0,
                owner_id=owner,
                request_id=owner,
                updated_at=NOW,
            )
            output.put(result.run.owner_id)
        except WorkflowStateError as error:
            output.put(error.kind)

    asyncio.run(claim())


async def database(tmp_path, fixture="minimal", *, ceiling=None):
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    session = await store.create_session("/workspace", "provider", "model")
    definition = compile_workflow((FIXTURES / f"{fixture}.json").read_text(encoding="utf-8"))
    await store.insert_workflow_definition(definition)
    result = await store.create_workflow_run(
        "run-1",
        definition_fingerprint=definition.fingerprint,
        parent_session_id=session,
        input_fingerprint=INPUT,
        request_id="create",
        created_at=NOW,
        ceiling=ceiling,
    )
    return store, definition, result.run


async def owned(tmp_path, fixture="minimal", *, ceiling=None):
    store, definition, run = await database(tmp_path, fixture, ceiling=ceiling)
    result = await store.claim_workflow_run(
        run.run_id,
        expected_generation=0,
        expected_owner_fence=0,
        owner_id="owner-a",
        request_id="claim",
        updated_at=NOW,
    )
    return store, definition, result.run


async def advance(store, run, change, *, request_id=None):
    result = await store.transition_workflow_run(
        run.run_id,
        change,
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id=request_id or f"request-{run.generation}",
        updated_at=max(run.updated_at, NOW + timedelta(seconds=run.generation)),
    )
    return result.run


def step(identity=None, status=Status.READY, **kwargs):
    return WorkflowStepInstance(
        identity or StepIdentity("implement"),
        status,
        kwargs.pop("input_fingerprint", INPUT),
        **kwargs,
    )


def fact_step(value):
    return WorkflowChange(Kind.STEP, step=value)


def budget(kind, name="reserve-1", **amounts):
    return WorkflowChange(kind, reservation_id=name, amounts=BudgetAmounts(**amounts))


async def test_definition_roundtrip_duplicate_and_immutable_conflict(tmp_path):
    store, definition, _ = await database(tmp_path)
    assert await store.insert_workflow_definition(definition) == definition
    assert await store.get_workflow_definition(definition.fingerprint) == definition
    assert await store.get_workflow_definition("f" * 64) is None
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE workflow_definitions SET compiler_version = 2")
        # Simulate corrupt pre-existing data; same hash may not replace it.
        connection.execute("DROP TRIGGER workflow_definitions_immutable")
        connection.execute("UPDATE workflow_definitions SET compiler_version = 2")
    with pytest.raises(WorkflowStateError) as raised:
        await store.insert_workflow_definition(definition)
    assert raised.value.kind == "conflict"
    with pytest.raises(WorkflowStateError, match="integrity"):
        await store.get_workflow_definition(definition.fingerprint)


async def test_v35_upgrade_preserves_session_and_is_idempotent(tmp_path):
    store = SqliteSessionStore(tmp_path / "old.db")
    await store.initialize()
    session = await store.create_session("/old", "old-provider", "old-model")
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        for table in (
            "workflow_verification_evidence",
            "workflow_verification_executions",
            "workflow_activity_results",
            "workflow_activity_events",
            "workflow_adoption_executions",
            "workflow_adoption_dispatch_events",
            "workflow_adoption_measurements",
            "workflow_activity_attempts",
            "workflow_step_outputs",
            "workflow_run_inputs",
            "workflow_result_projections",
            "workflow_expansions",
            "workflow_transition_journal",
            "workflow_budget_reservations",
            "workflow_step_instances",
            "workflow_runs",
            "workflow_definitions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET version = 35")
    await asyncio.gather(store.initialize(), SqliteSessionStore(store.database_path).initialize())
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (SCHEMA_VERSION,)
        assert connection.execute(
            "SELECT cwd, provider, model FROM sessions WHERE id = ?", (session,)
        ).fetchone() == ("/old", "old-provider", "old-model")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'workflow_%'"
            )
        } == {
            "workflow_definitions",
            "workflow_runs",
            "workflow_step_instances",
            "workflow_budget_reservations",
            "workflow_transition_journal",
            "workflow_expansions",
            "workflow_result_projections",
            "workflow_run_inputs",
            "workflow_step_outputs",
            "workflow_activity_attempts",
            "workflow_verification_evidence",
            "workflow_verification_executions",
            "workflow_activity_results",
            "workflow_activity_events",
            "workflow_adoption_executions",
            "workflow_adoption_dispatch_events",
            "workflow_adoption_measurements",
        }


async def test_migration_failure_rolls_back_schema_and_version(tmp_path):
    store = SqliteSessionStore(tmp_path / "old.db")
    await store.initialize()
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        for table in (
            "workflow_verification_evidence",
            "workflow_verification_executions",
            "workflow_activity_results",
            "workflow_activity_events",
            "workflow_adoption_executions",
            "workflow_adoption_dispatch_events",
            "workflow_adoption_measurements",
            "workflow_activity_attempts",
            "workflow_step_outputs",
            "workflow_run_inputs",
            "workflow_result_projections",
            "workflow_expansions",
            "workflow_transition_journal",
            "workflow_budget_reservations",
            "workflow_step_instances",
            "workflow_runs",
            "workflow_definitions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET version = 35")

    def interrupted(connection):
        connection.execute("CREATE TABLE workflow_partial(value TEXT)")
        raise RuntimeError("crash")

    with (
        patch(
            "neuro_code.infrastructure.persistence.sqlite_session_core._ensure_workflow_state_schema",
            interrupted,
        ),
        pytest.raises(RuntimeError, match="crash"),
    ):
        await store.initialize()
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (35,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='workflow_partial'"
            ).fetchone()
            is None
        )
    await store.initialize()


async def test_create_idempotency_and_identity_payload_conflicts(tmp_path):
    store, definition, run = await database(tmp_path)
    args = {
        "definition_fingerprint": definition.fingerprint,
        "parent_session_id": run.parent_session_id,
        "input_fingerprint": INPUT,
        "request_id": "create",
        "created_at": NOW,
    }
    repeat = await store.create_workflow_run("run-1", **args)
    assert repeat.replayed
    assert repeat.run == run
    assert await store.get_workflow_run("absent") is None
    with pytest.raises(WorkflowStateError, match="payload conflict"):
        await store.create_workflow_run("run-1", **{**args, "input_fingerprint": "b" * 64})
    with pytest.raises(WorkflowStateError, match="already exists"):
        await store.create_workflow_run("run-1", **{**args, "request_id": "other"})
    independent = await store.create_workflow_run("run-2", **args)
    assert independent.run.definition_fingerprint == run.definition_fingerprint
    assert independent.run.run_id != run.run_id


async def test_cas_and_owner_fence_block_stale_advance(tmp_path):
    store, _, run = await owned(tmp_path)
    updated = await advance(
        store,
        run,
        WorkflowChange(Kind.TRANSITION, status=Status.WAITING, waiting_reason="approval pending"),
    )
    assert updated.generation == run.generation + 1
    with pytest.raises(WorkflowStateError) as raised:
        await advance(
            store, run, WorkflowChange(Kind.TRANSITION, status=Status.RUNNING), request_id="stale"
        )
    assert raised.value.kind == "concurrent_modification"
    for wrong_owner, wrong_fence in (("owner-b", updated.owner_fence), (updated.owner_id, 0)):
        with pytest.raises(WorkflowStateError, match="fenced out"):
            await store.transition_workflow_run(
                run.run_id,
                WorkflowChange(Kind.TRANSITION, status=Status.RUNNING),
                expected_generation=updated.generation,
                owner_id=wrong_owner,
                owner_fence=wrong_fence,
                request_id="wrong",
                updated_at=NOW + timedelta(seconds=3),
            )
    assert await store.get_workflow_run(run.run_id) == updated


async def test_competing_owner_processes_exactly_one_claim(tmp_path):
    store, _, _ = await database(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(
            target=_claim_process, args=(str(store.database_path), owner, ready, start, output)
        )
        for owner in ("owner-a", "owner-b")
    ]
    try:
        for process in processes:
            process.start()
        assert {
            await asyncio.to_thread(ready.get, timeout=20),
            await asyncio.to_thread(ready.get, timeout=20),
        } == {"owner-a", "owner-b"}
        start.set()
        results = [await asyncio.to_thread(output.get, timeout=20) for _ in processes]
        assert results.count("concurrent_modification") == 1
        assert any(value in {"owner-a", "owner-b"} for value in results)
        for process in processes:
            await asyncio.to_thread(process.join, 20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
        output.close()
    run = await store.get_workflow_run("run-1")
    assert run.generation == 1
    assert len(await store.get_workflow_journal("run-1")) == 2


async def test_recovery_claim_preserves_uncertain_work_and_fences_old_owner(tmp_path):
    store, _, run = await owned(tmp_path)
    run = await advance(store, run, fact_step(step()))
    run = await advance(store, run, fact_step(step(status=Status.RUNNING)))
    run = await advance(store, run, budget(Kind.RESERVED, tool_calls=2, wall_milliseconds=3000))
    old = run
    restarted = SqliteSessionStore(store.database_path)
    result = await restarted.claim_workflow_run(
        run.run_id,
        expected_generation=run.generation,
        expected_owner_fence=run.owner_fence,
        owner_id="owner-new",
        request_id="resume",
        updated_at=NOW + timedelta(seconds=10),
    )
    run = result.run
    assert run.status is Status.NEEDS_ATTENTION
    assert run.steps == old.steps
    assert run.ledger == old.ledger
    assert run.owner_fence == old.owner_fence + 1
    with pytest.raises(WorkflowStateError, match="fenced out"):
        await advance(
            store,
            replace(old, generation=run.generation, updated_at=run.updated_at),
            budget(Kind.CONSUMED, tool_calls=1),
            request_id="old-owner",
        )
    assert (await store.get_workflow_journal(run.run_id))[-1].kind is Kind.RESUMED


async def test_atomic_journal_failure_rolls_back_snapshot_steps_and_ledger(tmp_path):
    store, _, run = await owned(tmp_path)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON workflow_transition_journal BEGIN SELECT RAISE(ABORT, 'crash window'); END"
        )
    for change in (fact_step(step()), budget(Kind.RESERVED, model_calls=1)):
        with pytest.raises(WorkflowStateError, match="storage"):
            await advance(store, run, change)
        assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
        assert len(await store.get_workflow_journal(run.run_id)) == 2
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TRIGGER reject_event")
    # The exact request is retryable because its interrupted transaction committed nothing.
    run = await advance(store, run, fact_step(step()))
    assert len(run.steps) == 1


async def test_duplicate_transition_after_later_commit_does_not_reapply(tmp_path):
    store, _, original = await owned(tmp_path)
    change = fact_step(step())
    run = await advance(store, original, change, request_id="stable")
    run = await advance(store, run, budget(Kind.RESERVED, model_calls=1))
    result = await store.transition_workflow_run(
        original.run_id,
        change,
        expected_generation=original.generation,
        owner_id=original.owner_id,
        owner_fence=original.owner_fence,
        request_id="stable",
        updated_at=NOW + timedelta(seconds=original.generation),
    )
    assert result.replayed
    assert result.committed_generation == original.generation + 1
    assert result.run == run
    with pytest.raises(WorkflowStateError, match="payload conflict"):
        await advance(
            store, original, fact_step(step(input_fingerprint="b" * 64)), request_id="stable"
        )


async def test_step_identity_state_result_failure_and_restart(tmp_path):
    store, _, run = await owned(tmp_path)
    run = await advance(store, run, fact_step(step()))
    run = await advance(store, run, fact_step(step(status=Status.RUNNING)))
    ref = ArtifactRef("artifact-1", "d" * 64, "workspace-1")
    run = await advance(store, run, fact_step(step(status=Status.COMPLETED, result_refs=(ref,))))
    assert run.steps[0].result_refs == (ref,)
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
    with pytest.raises(WorkflowStateError, match="conflict"):
        await advance(store, run, fact_step(step(status=Status.RUNNING)))
    run = await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.COMPLETED))
    with pytest.raises(WorkflowStateError, match="terminal"):
        await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.RUNNING))


@pytest.mark.parametrize(
    "status", [Status.CANCELLED, Status.NEEDS_ATTENTION, Status.FAILED, Status.WAITING]
)
async def test_terminal_waiting_and_attention_facts_survive_reopen(tmp_path, status):
    store, _, run = await owned(tmp_path)
    reason = (
        WorkflowFailure("failure", "bounded diagnostic")
        if status in {Status.NEEDS_ATTENTION, Status.FAILED}
        else None
    )
    run = await advance(
        store,
        run,
        WorkflowChange(
            Kind.TRANSITION,
            status=status,
            failure=reason,
            waiting_reason="approval" if status is Status.WAITING else None,
        ),
    )
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
    assert (
        json.loads((await store.get_workflow_journal(run.run_id))[-1].payload_json)["change"][
            "status"
        ]
        == status.value
    )


async def test_branch_decision_is_durable_and_cannot_be_rewritten(tmp_path):
    store, _, run = await owned(tmp_path, "branch")
    selected = WorkflowChange(Kind.BRANCH, position=StepIdentity("route"), selected_path="pass")
    run = await advance(store, run, selected)
    for change in (
        replace(selected, selected_path="fail"),
        replace(selected, selected_path="missing"),
    ):
        with pytest.raises(WorkflowStateError):
            await advance(store, run, change)
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
    run = await advance(store, run, fact_step(step(identity=StepIdentity("accept"))))
    assert run.steps[0].identity.step_id == "accept"


async def test_repeat_iteration_and_map_item_identities(tmp_path):
    store, _, run = await owned(tmp_path, "repeat")
    for i in (1, 2, 3):
        run = await advance(
            store, run, WorkflowChange(Kind.ITERATION, position=StepIdentity("repair_loop", i))
        )
        run = await advance(store, run, fact_step(step(identity=StepIdentity("verify", i))))
    assert len({s.identity.key for s in run.steps}) == 3
    with pytest.raises(WorkflowStateError, match="one declared round"):
        await advance(
            store, run, WorkflowChange(Kind.ITERATION, position=StepIdentity("repair_loop", 3))
        )
    other = tmp_path / "map"
    other.mkdir()
    store, _, run = await owned(other, "map")
    for item_key in ("中文文件", "a/b", "a-b"):
        run = await advance(
            store, run, fact_step(step(identity=StepIdentity("item_batch", item_key=item_key)))
        )
    assert len({s.identity.key for s in run.steps}) == 3
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run


@pytest.mark.parametrize(
    ("fixture", "identity"),
    [
        ("minimal", StepIdentity("missing")),
        ("minimal", StepIdentity("implement", 1)),
        ("minimal", StepIdentity("implement", item_key="a")),
        ("repeat", StepIdentity("verify")),
        ("map", StepIdentity("item_batch")),
    ],
)
async def test_invalid_source_position_rolls_back(tmp_path, fixture, identity):
    store, _, run = await owned(tmp_path, fixture)
    with pytest.raises(WorkflowStateError):
        await advance(store, run, fact_step(step(identity=identity)))
    assert await store.get_workflow_run(run.run_id) == run


async def test_budget_known_unknown_reconcile_and_ceiling_restart(tmp_path):
    store, _, run = await owned(tmp_path, ceiling=BudgetAmounts(2, 3, 5, 100, 50, 10000))
    run = await advance(
        store, run, budget(Kind.RESERVED, model_calls=2, input_tokens=30, wall_milliseconds=2000)
    )
    assert run.ledger.committed.model_calls == 2
    assert run.ledger.consumed.model_calls is None
    with pytest.raises(WorkflowStateError, match="ceiling exceeded"):
        await advance(store, run, budget(Kind.RESERVED, "reserve-2", model_calls=2))
    run = await advance(
        store, run, budget(Kind.CONSUMED, model_calls=1, input_tokens=None, wall_milliseconds=1800)
    )
    assert run.ledger.consumed.input_tokens is None
    assert run.ledger.consumed.wall_milliseconds == 1800
    assert run.status is Status.NEEDS_ATTENTION
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
    with pytest.raises(WorkflowStateError, match="unknown usage"):
        await advance(store, run, budget(Kind.RESERVED, "reserve-2", model_calls=1))
    with pytest.raises(WorkflowStateError, match="reconciled before progress"):
        await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.RUNNING))
    with pytest.raises(WorkflowStateError, match="known consumption"):
        await advance(
            store,
            run,
            budget(Kind.RECONCILED, model_calls=0, input_tokens=20, wall_milliseconds=1800),
        )
    run = await advance(
        store, run, budget(Kind.RECONCILED, model_calls=1, input_tokens=20, wall_milliseconds=1800)
    )
    run = await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.RUNNING))
    assert run.ledger.consumed.input_tokens == 20
    run = await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.COMPLETED))
    assert run.status is Status.COMPLETED


async def test_actual_overrun_is_recorded_and_not_erased(tmp_path):
    store, _, run = await owned(tmp_path, ceiling=BudgetAmounts(model_calls=2))
    run = await advance(store, run, budget(Kind.RESERVED, model_calls=1))
    run = await advance(store, run, budget(Kind.CONSUMED, model_calls=3))
    assert run.status is Status.NEEDS_ATTENTION
    assert run.ledger.consumed.model_calls == 3
    with pytest.raises(WorkflowStateError, match="reconciled before progress"):
        await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.RUNNING))
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run


async def test_budget_identities_and_completion_with_pending_work(tmp_path):
    store, _, run = await owned(tmp_path)
    with pytest.raises(WorkflowStateError, match="missing"):
        await advance(store, run, budget(Kind.CONSUMED, tool_calls=1))
    run = await advance(store, run, budget(Kind.RESERVED, tool_calls=3))
    with pytest.raises(WorkflowStateError, match="already exists"):
        await advance(store, run, budget(Kind.RESERVED, tool_calls=1))
    with pytest.raises(WorkflowStateError, match="unresolved"):
        await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.COMPLETED))
    run = await advance(store, run, budget(Kind.CONSUMED, tool_calls=0))
    with pytest.raises(WorkflowStateError, match="already consumed"):
        await advance(store, run, budget(Kind.CONSUMED, tool_calls=0))
    with pytest.raises(WorkflowStateError, match="only unknown"):
        await advance(store, run, budget(Kind.RECONCILED, tool_calls=0))
    run = await advance(store, run, fact_step(step()))
    with pytest.raises(WorkflowStateError, match="unresolved"):
        await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.COMPLETED))


async def test_bounds_are_durable_and_rejected_mutations_do_not_commit(tmp_path, monkeypatch):
    store, _, run = await owned(tmp_path)
    monkeypatch.setattr("neuro_code.domain.workflows.state.MAX_BUDGET_RESERVATIONS", 1)
    run = await advance(store, run, budget(Kind.RESERVED, "one"))
    with pytest.raises(WorkflowStateError):
        await advance(store, run, budget(Kind.RESERVED, "two"))
    assert await store.get_workflow_run(run.run_id) == run
    monkeypatch.setattr("neuro_code.domain.workflows.state.MAX_STEP_INSTANCES", 0)
    with pytest.raises(WorkflowStateError):
        await advance(store, run, fact_step(step()))
    assert await store.get_workflow_run(run.run_id) == run


async def test_journal_pagination_digest_and_immutable_evidence(tmp_path):
    store, _, run = await owned(tmp_path)
    run = await advance(store, run, fact_step(step()))
    page = await store.get_workflow_journal(run.run_id, limit=1)
    assert page[0].generation == 0
    page2 = await store.get_workflow_journal(run.run_id, after_generation=0)
    assert [e.generation for e in page2] == [1, 2]
    for event in (*page, *page2):
        assert (
            hashlib.sha256(event.payload_json.encode("utf-8")).hexdigest()
            == event.payload_fingerprint
        )
    with (
        closing(sqlite3.connect(store.database_path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute("UPDATE workflow_transition_journal SET kind = 'changed'")
    assert await store.get_workflow_journal("missing") == ()


async def test_no_effects_no_publication_and_session_delete_cascade(tmp_path):
    store, _, run = await owned(tmp_path)
    typed_port: WorkflowStateStore = store
    assert await typed_port.get_workflow_run(run.run_id) == run
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        assert connection.execute("SELECT count(*) FROM task_dags").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM session_tasks").fetchone() == (0,)
    await store.delete_session(run.parent_session_id)
    assert await store.get_workflow_run(run.run_id) is None
    assert await store.get_workflow_journal(run.run_id) == ()
    assert await store.get_workflow_definition(run.definition_fingerprint) is not None


async def test_create_missing_parent_or_definition_fails_atomically(tmp_path):
    store, definition, run = await database(tmp_path)
    for missing in ({"parent_session_id": "absent"}, {"definition_fingerprint": "f" * 64}):
        with pytest.raises(WorkflowStateError):
            await store.create_workflow_run(
                "other",
                **{
                    "definition_fingerprint": definition.fingerprint,
                    "parent_session_id": run.parent_session_id,
                    "input_fingerprint": INPUT,
                    "request_id": "create",
                    "created_at": NOW,
                    **missing,
                },
            )
        assert await store.get_workflow_run("other") is None
        assert await store.get_workflow_journal("other") == ()
    with pytest.raises(WorkflowStateError, match="widen"):
        await store.create_workflow_run(
            "wider",
            definition_fingerprint=definition.fingerprint,
            parent_session_id=run.parent_session_id,
            input_fingerprint=INPUT,
            request_id="create",
            created_at=NOW,
            ceiling=BudgetAmounts(model_calls=101),
        )


async def test_claim_retry_conflict_and_wrong_fence(tmp_path):
    store, _, run = await owned(tmp_path)
    replay = await store.claim_workflow_run(
        run.run_id,
        expected_generation=0,
        expected_owner_fence=0,
        owner_id="owner-a",
        request_id="claim",
        updated_at=NOW,
    )
    assert replay.replayed
    assert replay.run == run
    with pytest.raises(WorkflowStateError, match="payload conflict"):
        await store.claim_workflow_run(
            run.run_id,
            expected_generation=0,
            expected_owner_fence=0,
            owner_id="owner-b",
            request_id="claim",
            updated_at=NOW,
        )
    with pytest.raises(WorkflowStateError, match="fence changed"):
        await store.claim_workflow_run(
            run.run_id,
            expected_generation=run.generation,
            expected_owner_fence=0,
            owner_id="owner-b",
            request_id="takeover",
            updated_at=NOW,
        )
    assert await store.get_workflow_run(run.run_id) == run


async def test_error_after_journal_insert_rolls_back_complete_transaction(tmp_path):
    from neuro_code.infrastructure.persistence import sqlite_session_workflows as owner

    store, _, run = await owned(tmp_path)
    original = owner._append_event

    def interrupted(*args):
        original(*args)
        raise RuntimeError("after journal write")

    with (
        patch.object(owner, "_append_event", interrupted),
        pytest.raises(RuntimeError, match="after journal"),
    ):
        await advance(store, run, fact_step(step()))
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
    assert len(await store.get_workflow_journal(run.run_id)) == 2


async def test_missing_run_and_backwards_time_fail_without_fact(tmp_path):
    store, _, run = await owned(tmp_path)
    with pytest.raises(WorkflowStateError, match="missing"):
        await store.claim_workflow_run(
            "missing",
            expected_generation=0,
            expected_owner_fence=0,
            owner_id="owner",
            request_id="claim",
            updated_at=NOW,
        )
    with pytest.raises(WorkflowStateError, match="backwards"):
        await store.transition_workflow_run(
            run.run_id,
            fact_step(step()),
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            request_id="backwards",
            updated_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(WorkflowStateError, match="invalid run state"):
        await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.READY))
    assert await store.get_workflow_run(run.run_id) == run


async def test_step_creation_input_and_failure_contracts(tmp_path):
    store, _, run = await owned(tmp_path)
    with pytest.raises(WorkflowStateError, match="start READY"):
        await advance(store, run, fact_step(step(status=Status.RUNNING)))
    run = await advance(store, run, fact_step(step()))
    with pytest.raises(WorkflowStateError, match="identity/input/state"):
        await advance(
            store, run, fact_step(step(input_fingerprint="b" * 64, status=Status.RUNNING))
        )
    run = await advance(store, run, fact_step(step(status=Status.RUNNING)))
    run = await advance(
        store, run, fact_step(step(status=Status.WAITING, waiting_reason="parent verification"))
    )
    run = await advance(
        store,
        run,
        fact_step(
            step(
                status=Status.FAILED,
                failure=WorkflowFailure("tool_error", "Unknown external effect"),
            )
        ),
    )
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
    assert run.steps[0].failure.code == "tool_error"


async def test_invalid_branch_or_iteration_kind_cannot_publish_fact(tmp_path):
    store, _, run = await owned(tmp_path)
    for change in (
        WorkflowChange(Kind.BRANCH, position=StepIdentity("implement"), selected_path="pass"),
        WorkflowChange(Kind.ITERATION, position=StepIdentity("implement")),
    ):
        with pytest.raises(WorkflowStateError):
            await advance(store, run, change)
    assert len(await store.get_workflow_journal(run.run_id)) == 2


async def test_snapshot_corruption_is_not_silently_loaded(tmp_path):
    store, _, run = await owned(tmp_path)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("UPDATE workflow_runs SET generation = generation + 1")
    with pytest.raises(WorkflowStateError) as raised:
        await store.get_workflow_run(run.run_id)
    assert raised.value.kind == "integrity"
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("UPDATE workflow_runs SET snapshot_json = '{}' ")
    with pytest.raises(WorkflowStateError) as raised:
        await store.get_workflow_run(run.run_id)
    assert raised.value.kind == "integrity"


async def test_storage_error_is_bounded_and_not_database_sql_details(tmp_path):
    store = SqliteSessionStore(tmp_path / "missing.db")
    with pytest.raises(WorkflowStateError) as raised:
        await store.get_workflow_run("run-1")
    assert raised.value.kind == "storage"
    assert "SELECT" not in str(raised.value)
    assert "no such table" not in str(raised.value)


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("get_workflow_run", {"run_id": "../path"}),
        ("get_workflow_definition", {"fingerprint": "bad"}),
        ("get_workflow_journal", {"run_id": "run", "limit": 101}),
        ("get_workflow_journal", {"run_id": "run", "after_generation": -2}),
    ],
)
async def test_invalid_external_queries_fail_before_sql(tmp_path, method, kwargs):
    store = SqliteSessionStore(tmp_path / "not-created.db")
    with pytest.raises(ValueError, match=r"invalid|limit"):
        await getattr(store, method)(**kwargs)
    assert not store.database_path.exists()


def test_unknown_usage_is_distinct_from_outstanding_and_zero():
    from neuro_code.domain.workflows.state import BudgetReservation, WorkflowBudgetLedger

    unresolved = BudgetReservation("reserve", BudgetAmounts(tool_calls=3), NOW)
    unknown = replace(unresolved, consumed=BudgetAmounts(tool_calls=None), settled_at=NOW)
    zero = replace(unresolved, consumed=BudgetAmounts(), settled_at=NOW)
    assert (
        WorkflowBudgetLedger(BudgetAmounts(tool_calls=3), (unresolved,)).committed.tool_calls == 3
    )
    assert (
        WorkflowBudgetLedger(BudgetAmounts(tool_calls=3), (unresolved,)).consumed.tool_calls is None
    )
    assert (
        WorkflowBudgetLedger(BudgetAmounts(tool_calls=3), (unknown,)).committed.tool_calls is None
    )
    assert WorkflowBudgetLedger(BudgetAmounts(tool_calls=3), (zero,)).committed.tool_calls == 0


@pytest.mark.parametrize(
    "factory",
    [
        lambda: BudgetAmounts(model_calls=True),
        lambda: BudgetAmounts(tool_calls=-1),
        lambda: StepIdentity("implement", iteration=4),
        lambda: StepIdentity("implement", item_key="\x1b[1m"),
        lambda: WorkflowStepInstance(StepIdentity("a"), Status.COMPLETED, "invalid"),
        lambda: WorkflowStepInstance(
            StepIdentity("a"), Status.RUNNING, INPUT, result_refs=(ArtifactRef("a", INPUT),)
        ),
        lambda: WorkflowStepInstance(StepIdentity("a"), Status.FAILED, INPUT),
        lambda: WorkflowStepInstance(
            StepIdentity("a"), Status.RUNNING, INPUT, waiting_reason="waiting"
        ),
        lambda: WorkflowChange(Kind.CLAIMED),
        lambda: WorkflowChange(Kind.STEP),
        lambda: WorkflowChange(
            Kind.RESERVED, reservation_id="a", amounts=BudgetAmounts(tool_calls=None)
        ),
        lambda: WorkflowChange(Kind.BRANCH, position=StepIdentity("a")),
        lambda: WorkflowChange(Kind.ITERATION),
        lambda: WorkflowChange(Kind.TRANSITION, status=Status.RUNNING, selected_path="path"),
    ],
)
def test_ambiguous_or_mutable_facts_are_rejected(factory):
    with pytest.raises(ValueError, match=r"invalid|expected|must|requires|only|unsupported"):
        factory()


async def test_late_usage_after_cancel_is_durable_without_reopening_run(tmp_path):
    store, _, run = await owned(tmp_path)
    run = await advance(store, run, budget(Kind.RESERVED, tool_calls=2))
    run = await advance(store, run, WorkflowChange(Kind.TRANSITION, status=Status.CANCELLED))
    run = await advance(store, run, budget(Kind.CONSUMED, tool_calls=None))
    assert run.status is Status.CANCELLED
    assert run.ledger.consumed.tool_calls is None
    run = await advance(store, run, budget(Kind.RECONCILED, tool_calls=1))
    assert run.status is Status.CANCELLED
    assert run.ledger.consumed.tool_calls == 1
    for change in (
        budget(Kind.RESERVED, "new", tool_calls=1),
        WorkflowChange(Kind.TRANSITION, status=Status.RUNNING),
    ):
        with pytest.raises(WorkflowStateError, match="terminal"):
            await advance(store, run, change)
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run


async def test_waiting_resume_preserves_repeat_control_position(tmp_path):
    store, _, run = await owned(tmp_path, "repeat")
    run = await advance(
        store, run, WorkflowChange(Kind.ITERATION, position=StepIdentity("repair_loop", 1))
    )
    position = run.position
    run = await advance(
        store,
        run,
        WorkflowChange(Kind.TRANSITION, status=Status.WAITING, waiting_reason="parent result"),
    )
    assert run.position == position
    assert await SqliteSessionStore(store.database_path).get_workflow_run(run.run_id) == run
    run = await advance(
        store, run, WorkflowChange(Kind.TRANSITION, status=Status.RUNNING, position=position)
    )
    assert run.position == position


@pytest.mark.parametrize("table", ["workflow_step_instances", "workflow_budget_reservations"])
async def test_child_row_identity_mismatch_fails_closed(tmp_path, table):
    store, _, run = await owned(tmp_path)
    run = await advance(store, run, fact_step(step()))
    run = await advance(store, run, budget(Kind.RESERVED))
    column = "instance_key" if table == "workflow_step_instances" else "reservation_id"
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute(f"UPDATE {table} SET {column} = 'wrong-key'")
    with pytest.raises(WorkflowStateError, match="identity mismatch"):
        await store.get_workflow_run(run.run_id)
