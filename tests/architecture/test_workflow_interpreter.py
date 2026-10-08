"""DW5a real SQLite bounded control, crash/reopen, typed provenance and ownership."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

import pytest

from neuro_code.application.ports.workflow_interpreter import FakeActivityInvocation
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.workflows.fake_workflow_activity import (
    DeterministicFakeWorkflowActivity,
)
from neuro_code.application.workflows.workflow_interpreter import DurableWorkflowInterpreter
from neuro_code.domain.workflows.activity import WorkflowActivityResult, WorkflowActivityState
from neuro_code.domain.workflows.definition import compile_workflow
from neuro_code.domain.workflows.interpreter import (
    OutputKind,
    expansion_id,
    invocation_id,
    typed_json,
)
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import BudgetAmounts, StepIdentity, WorkflowStatus
from neuro_code.infrastructure.persistence import sqlite_session_workflow_interpreter as owner
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from tests.architecture.test_workflow_projection import END, finish
from tests.architecture.test_workflow_publication import FIXTURES, NOW


def source(name="minimal"):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def activity(step_id, kind="parent.verify", inputs=None):
    return {"kind": "Activity", "step_id": step_id, "activity": kind, "inputs": inputs or {}}


def engine(store, **kwargs):
    return DurableWorkflowInterpreter(
        state=store,
        facts=store,
        publication=store,
        projections=store,
        dags=store,
        activities=store,
        **kwargs,
    )


async def setup(tmp_path, data=None, value=None, *, ceiling=None):
    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.initialize()
    session = await store.create_session("/workspace", "provider", "model")
    definition = compile_workflow(canonical(data or source()))
    value = value if value is not None else {"targets": [], "objective": "中文目标"}
    input_json = typed_json(definition.input_schema, value)
    await store.insert_workflow_definition(definition)
    await store.create_workflow_run(
        "run",
        definition_fingerprint=definition.fingerprint,
        parent_session_id=session,
        input_fingerprint=digest(value),
        request_id="create",
        created_at=NOW,
        ceiling=ceiling,
    )
    await store.put_workflow_input("run", input_json)
    await store.claim_workflow_run(
        "run",
        expected_generation=0,
        expected_owner_fence=0,
        owner_id="owner",
        request_id="claim",
        updated_at=NOW,
    )
    return store


async def tick(store, interpreter=None):
    run = await store.get_workflow_run("run")
    result = await (interpreter or engine(store)).advance_once(
        "run",
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=END,
    )
    assert result.run.generation - run.generation in (0, 1)
    return result


async def external_activity_result(store):
    """Test-only owner; Interpreter never invokes this helper."""
    run = await store.get_workflow_run("run")
    step = next(s for s in run.steps if s.identity == run.position)
    key = invocation_id(run.run_id, step.identity, step.input_fingerprint)
    attempt = await store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="activity-owner",
        reserved=BudgetAmounts(),
        updated_at=END,
    )
    attempt = await store.start_workflow_activity(
        key,
        expected_revision=attempt.revision,
        owner_id=attempt.owner_id,
        owner_fence=attempt.owner_fence,
        updated_at=END,
    )
    invocation = attempt.invocation
    value = DeterministicFakeWorkflowActivity().evaluate(
        FakeActivityInvocation(
            key, run.run_id, step.identity, invocation.activity, invocation.request_json
        )
    )
    result = WorkflowActivityResult(
        key,
        invocation.request_fingerprint,
        invocation.activity,
        WorkflowActivityState.COMPLETED,
        "test-result:" + key,
        digest(value),
        BudgetAmounts(),
        END,
        value,
    )
    return await store.finish_workflow_activity(
        result,
        expected_revision=attempt.revision,
        owner_id=attempt.owner_id,
        owner_fence=attempt.owner_fence,
    )


async def drive(store, *, limit=80, interpreter=None):
    for _ in range(limit):
        result = await tick(store, interpreter)
        if result.action == "activity_waiting":
            await external_activity_result(store)
            continue
        if not result.progressed:
            return result
    raise AssertionError("bounded test driver did not settle")


async def terminal_projection(store, identity, state=None):
    expansion = await store.get_workflow_expansion(expansion_id("run", identity))
    dag = await store.get_task_dag(expansion.dag_id)
    if state is None:
        dag = await finish(store, dag)
    else:
        dag = await finish(store, dag, state=state, evidence=False)
    if state is None:
        run = await store.get_workflow_run("run")
        await store.project_workflow_result(
            expansion.expansion_id,
            run_id=run.run_id,
            parent_session_id=run.parent_session_id,
            created_at=END,
        )
    return dag


async def reopen(store):
    new = SqliteSessionStore(store.database_path)
    await new.initialize()
    return new


async def test_sequence_external_activities_and_completion_is_only_waiting(tmp_path):
    data = source()
    data["steps"] = [
        activity("adopt", "parent.adopt"),
        activity("verify"),
        activity("repair", "parent.repair"),
    ]
    store = await setup(tmp_path, data)
    result = await drive(store)
    assert result.action == "completion_pending"
    assert result.run.status is WorkflowStatus.WAITING
    assert "completion requirements pending" in result.run.waiting_reason
    assert all(s.status is WorkflowStatus.COMPLETED for s in result.run.steps)
    outputs = [
        await store.get_workflow_step_output("run", StepIdentity(name))
        for name in ("adopt", "verify", "repair")
    ]
    assert all(o.kind is OutputKind.ACTIVITY for o in outputs)
    assert all(json.loads(o.output_json)["status"] == "fake" for o in outputs)
    assert json.loads(outputs[0].output_json)["parent_workspace_changed"] is False
    assert result.run.ledger.committed.generated_tasks == 0
    assert await tick(store) == result


@pytest.mark.parametrize(
    ("condition", "selected"),
    [
        (
            {
                "op": "eq",
                "ref": {"kind": "input", "field_path": ["objective"]},
                "value": "中文目标",
            },
            "pass",
        ),
        (
            {"op": "eq", "ref": {"kind": "input", "field_path": ["objective"]}, "value": "other"},
            "fail",
        ),
        (
            {
                "op": "exists",
                "ref": {"kind": "result", "step_id": "verify", "field_path": ["status"]},
            },
            "pass",
        ),
        (
            {
                "op": "eq",
                "ref": {"kind": "result", "step_id": "verify", "field_path": ["status"]},
                "value": "fake",
            },
            "pass",
        ),
    ],
)
async def test_branch_persisted_selection_skips_other_path(tmp_path, condition, selected):
    data = source("branch")
    data["steps"][1]["condition"] = condition
    store = await setup(tmp_path, data)
    await tick(store)
    await tick(store)
    await external_activity_result(store)
    await tick(store)
    result = await tick(store)
    assert result.action == "record_branch"
    store = await reopen(store)  # committed decision, lost client acknowledgement
    with patch(
        "neuro_code.application.workflows.workflow_values.WorkflowValues.condition",
        side_effect=AssertionError("re-evaluation"),
    ):
        await drive(store)
    run = await store.get_workflow_run("run")
    names = {s.identity.step_id for s in run.steps}
    assert names == {"verify", "accept" if selected == "pass" else "repair"}
    events = await store.get_workflow_journal("run")
    decisions = [e for e in events if e.kind.value == "branch_decision"]
    assert len(decisions) == 1
    assert json.loads(decisions[0].payload_json)["change"]["selected_path"] == selected


@pytest.mark.parametrize("target", [1, 2, 3, 4])
async def test_repeat_post_body_durable_iterations_and_limit(tmp_path, target):
    data = source("repeat")
    data["steps"][0]["until"]["ref"]["field_path"] = ["workspace_generation"]
    data["steps"][0]["until"]["value"] = target
    store = await setup(tmp_path, data)
    for _ in range(30):
        result = await tick(store)
        store = await reopen(store)  # every fact can lose its acknowledgement
        if result.action == "activity_waiting":
            await external_activity_result(store)
            continue
        if not result.progressed:
            break
    run = await store.get_workflow_run("run")
    assert run.status is (WorkflowStatus.WAITING if target <= 3 else WorkflowStatus.FAILED)
    expected = min(target, 3)
    assert {s.identity.iteration for s in run.steps if s.identity.step_id == "verify"} == set(
        range(1, expected + 1)
    )
    events = await store.get_workflow_journal("run")
    assert len([e for e in events if e.kind.value == "iteration"]) == expected
    assert (
        len([e for e in events if '"operation":"consume_typed_output"' in e.payload_json])
        == expected
    )
    assert run.ledger.committed.generated_tasks == 0
    if target > 3:
        assert run.failure.code == "repeat_limit"


@pytest.mark.parametrize("selector", [None, 1])
async def test_repeat_result_resolves_exact_durable_body_output(tmp_path, selector):
    data = source("repeat")
    data["steps"][0]["until"]["ref"]["field_path"] = ["workspace_generation"]
    data["steps"][0]["until"]["value"] = 2
    ref = {
        "kind": "result",
        "step_id": "repair_loop",
        "field_path": ["last", "verify", "workspace_generation"]
        if selector is None
        else ["verify", "workspace_generation"],
    }
    if selector:
        ref["iteration"] = selector
    data["steps"].append(activity("consumer", inputs={"result": ref}))
    store = await setup(tmp_path, data)
    await drive(store)
    output = await store.get_workflow_step_output("run", StepIdentity("consumer"))
    expected = digest({"result": 2 if selector is None else 1})
    assert output.input_fingerprint == expected


@pytest.mark.parametrize("count", [0, 1, 7])
async def test_map_freeze_restart_item_isolation_and_empty_semantics(tmp_path, count):
    data = source("map")
    data["input_schema"]["properties"]["targets"]["max_items"] = 7
    data["steps"][0]["max_items"] = 7
    value = {"targets": [f"中文{i}" for i in range(count)], "objective": "目标"}
    store = await setup(tmp_path, data, value)
    assert (await tick(store)).action == "initialize_step"
    store = await reopen(store)
    result = await tick(store)
    identity = StepIdentity("expand")
    if count == 0:
        assert result.action == "empty_map"
        assert await store.get_workflow_expansion(expansion_id("run", identity)) is None
        output = await store.get_workflow_step_output("run", identity)
        assert output.output_json == canonical({"count": 0, "items": []})
    else:
        assert result.action == "publish_dag"
        first = await store.get_workflow_expansion(expansion_id("run", identity))
        store = await reopen(store)
        assert (await tick(store)).action == "waiting_projection"
        assert first == await store.get_workflow_expansion(first.expansion_id)
        dag = await store.get_task_dag(first.dag_id)
        assert len(dag.nodes) == count
        for i, member in enumerate(first.members):
            assert member.member_key.startswith(f"item-{i:02}-")
            assert member.input_fingerprint == digest({"target": f"中文{i}"})
            assert canonical({"target": f"中文{i}"}) in dag.node(member.node_id).prompt
        await terminal_projection(store, identity)
        assert (await tick(store)).action == "consume_projection"
        store = await reopen(store)
        await drive(store)
        assert (await store.get_workflow_step_output("run", identity)).kind is OutputKind.PROJECTION
    run = await store.get_workflow_run("run")
    assert run.ledger.committed.generated_tasks == count


async def test_multiple_tasks_map_dependencies_stay_within_item(tmp_path):
    data = source("map")
    data["steps"][0]["max_items"] = 4
    task = json.loads(json.dumps(data["steps"][0]["batch"]["tasks"][0]))
    task["task_id"] = "zfollow"
    task["depends_on"] = ["work"]
    data["steps"][0]["batch"]["tasks"].append(task)
    store = await setup(tmp_path, data, {"targets": ["甲", "乙"], "objective": "目标"})
    await tick(store)
    await tick(store)
    expansion = await store.get_workflow_expansion(expansion_id("run", StepIdentity("expand")))
    dag = await store.get_task_dag(expansion.dag_id)
    for key in {m.member_key for m in expansion.members}:
        bindings = {m.task_id: m.node_id for m in expansion.members if m.member_key == key}
        assert dag.node(bindings["zfollow"]).dependencies == (bindings["work"],)


async def test_taskbatch_projection_replay_resultref_and_no_second_dag(tmp_path):
    data = source()
    data["steps"].append(
        activity(
            "use",
            inputs={
                "answer": {
                    "kind": "result",
                    "step_id": "implement",
                    "field_path": ["tasks", "work", "response"],
                }
            },
        )
    )
    store = await setup(tmp_path, data)
    await tick(store)
    await tick(store)
    before = await store.get_workflow_expansion(expansion_id("run", StepIdentity("implement")))
    store = await reopen(store)  # publication commit before acknowledgement
    assert (await tick(store)).action == "waiting_projection"
    await terminal_projection(store, StepIdentity("implement"))
    assert (await tick(store)).action == "consume_projection"
    store = await reopen(store)  # consumption commit before acknowledgement
    await drive(store)
    after = await store.get_workflow_expansion(before.expansion_id)
    assert after == before
    output = await store.get_workflow_step_output("run", StepIdentity("use"))
    assert output.input_fingerprint == digest({"answer": "完整 worker response"})
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM task_dags").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workflow_step_outputs").fetchone()[0] == 2


@pytest.mark.parametrize("node_state", ["failed", "cancelled", "indeterminate", "skipped"])
async def test_terminal_unsuccessful_dag_never_consumed_as_success(tmp_path, node_state):
    from neuro_code.domain.task_dag import TaskDagNodeState

    store = await setup(tmp_path)
    await tick(store)
    await tick(store)
    await terminal_projection(store, StepIdentity("implement"), TaskDagNodeState(node_state))
    result = await tick(store)
    assert result.action == "dag_failure"
    assert result.run.status is (
        WorkflowStatus.NEEDS_ATTENTION if node_state == "indeterminate" else WorkflowStatus.FAILED
    )
    assert await store.get_workflow_step_output("run", StepIdentity("implement")) is None
    assert not (await tick(store)).progressed


@pytest.mark.parametrize("wrong", ["generation", "owner", "fence"])
async def test_stale_ownership_stops_before_activity_or_resolution(tmp_path, wrong):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    run = await store.get_workflow_run("run")
    params = {
        "expected_generation": run.generation,
        "owner_id": run.owner_id,
        "owner_fence": run.owner_fence,
    }
    params[
        {"generation": "expected_generation", "owner": "owner_id", "fence": "owner_fence"}[wrong]
    ] = 0 if wrong != "owner" else "other"
    with (
        patch.object(store, "get_workflow_input", side_effect=AssertionError("unsafe resolution")),
        pytest.raises(WorkflowStateError, match="stale"),
    ):
        await engine(store).advance_once("run", **params, updated_at=END)


async def test_concurrent_tick_and_owner_replacement_have_no_duplicate_output(tmp_path):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    other = await reopen(store)
    results = await asyncio.gather(tick(store), tick(other), return_exceptions=True)
    assert sum(not isinstance(r, Exception) and r.progressed for r in results) == 1
    run = await store.get_workflow_run("run")
    await store.claim_workflow_run(
        "run",
        expected_generation=run.generation,
        expected_owner_fence=run.owner_fence,
        owner_id="new-owner",
        request_id="reclaim",
        updated_at=END,
    )
    with pytest.raises(WorkflowStateError, match="stale"):
        await engine(store).advance_once(
            "run",
            expected_generation=run.generation,
            owner_id="owner",
            owner_fence=1,
            updated_at=END,
        )
    assert (await tick(store)).action == "stopped"  # reclaim requires explicit reconciliation
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_step_outputs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM workflow_activity_attempts").fetchone()[0] == 1
        )


async def test_output_transaction_rollback_and_commit_before_ack(tmp_path):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    await tick(store)
    await external_activity_result(store)
    before = await store.get_workflow_run("run")
    with (
        patch.object(owner, "_append_event", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError, match="crash"),
    ):
        await tick(store)
    assert await store.get_workflow_run("run") == before
    assert await store.get_workflow_step_output("run", StepIdentity("verify")) is None
    real = store.commit_workflow_step_output

    async def lost_ack(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("lost ack")

    with (
        patch.object(store, "commit_workflow_step_output", side_effect=lost_ack),
        pytest.raises(RuntimeError, match="lost ack"),
    ):
        await tick(store)
    store = await reopen(store)
    with patch(
        "neuro_code.application.workflows.fake_workflow_activity.DeterministicFakeWorkflowActivity.evaluate",
        side_effect=AssertionError("repeated activity"),
    ):
        assert (await tick(store)).action == "control_flow_exhausted"


async def test_input_snapshot_immutable_and_tamper_fails_closed(tmp_path):
    store = await setup(tmp_path)
    value = await store.get_workflow_input("run")
    await store.put_workflow_input("run", value)
    with pytest.raises(WorkflowStateError):
        await store.put_workflow_input("run", canonical({"targets": [], "objective": "changed"}))
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE workflow_run_inputs SET input_json = '{}' WHERE run_id = 'run'"
            )
        connection.execute("DROP TRIGGER workflow_run_inputs_immutable_update")
        connection.execute("UPDATE workflow_run_inputs SET input_json = '{}' WHERE run_id = 'run'")
    with pytest.raises(WorkflowStateError):
        await tick(store)


async def test_projection_tamper_rejected_before_control_progress(tmp_path):
    store = await setup(tmp_path)
    await tick(store)
    await tick(store)
    await terminal_projection(store, StepIdentity("implement"))
    before = await store.get_workflow_run("run")
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TRIGGER workflow_result_projections_immutable_update")
        connection.execute("UPDATE workflow_result_projections SET output_json = '{}' ")
    with pytest.raises(WorkflowStateError):
        await tick(store)
    assert await store.get_workflow_run("run") == before


async def test_step_output_tamper_and_conflicting_replay_rejected(tmp_path):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    await tick(store)
    await external_activity_result(store)
    await tick(store)
    output = await store.get_workflow_step_output("run", StepIdentity("verify"))
    run = await store.get_workflow_run("run")
    replay = await store.commit_workflow_step_output(
        output,
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=END,
    )
    assert replay.replayed
    with pytest.raises(WorkflowStateError, match="conflict"):
        await store.commit_workflow_step_output(
            replace(
                output, output_json=canonical({"status": "changed", "workspace_generation": 0})
            ),
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=END,
        )
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TRIGGER workflow_step_outputs_immutable_update")
        connection.execute("UPDATE workflow_step_outputs SET output_fingerprint = ?", ("a" * 64,))
    with pytest.raises(WorkflowStateError):
        await store.get_workflow_step_output("run", StepIdentity("verify"))


async def test_schema_38_upgrade_retains_previous_run_and_snapshot(tmp_path):
    store = await setup(tmp_path)
    before = await store.get_workflow_run("run")
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TABLE workflow_step_outputs")
        connection.execute("DROP TABLE workflow_run_inputs")
        connection.execute("UPDATE schema_meta SET version = 38")
    store = await reopen(store)
    assert SCHEMA_VERSION == 41
    assert await store.get_workflow_run("run") == before
    with pytest.raises(WorkflowStateError, match="missing"):
        await tick(store)  # no invented input for legacy runs


@pytest.mark.parametrize(
    "value",
    [
        {"targets": ["x"] * 5, "objective": "x"},
        {"targets": [], "objective": True},
        {"targets": [], "objective": "x", "extra": 1},
    ],
)
async def test_invalid_typed_input_not_persisted(tmp_path, value):
    with pytest.raises(ValueError, match="typed"):
        await setup(tmp_path, value=value)


async def test_resultref_from_exact_projection_drives_branch(tmp_path):
    data = source()
    branch = source("branch")["steps"][1]
    branch["condition"] = {
        "op": "eq",
        "ref": {
            "kind": "result",
            "step_id": "implement",
            "field_path": ["tasks", "work", "status"],
        },
        "value": "completed",
    }
    data["steps"].append(branch)
    store = await setup(tmp_path, data)
    await tick(store)
    await tick(store)
    await terminal_projection(store, StepIdentity("implement"))
    await tick(store)
    await drive(store)
    run = await store.get_workflow_run("run")
    assert {s.identity.step_id for s in run.steps} == {"implement", "accept"}
    assert run.status is WorkflowStatus.WAITING  # completed worker text never proves verification


async def test_repeat_task_budget_retained_after_restart(tmp_path):
    data = source("repeat")
    data["steps"][0]["steps"].insert(0, source()["steps"][0])
    data["steps"][0]["until"]["ref"]["field_path"] = ["workspace_generation"]
    data["steps"][0]["until"]["value"] = 2
    store = await setup(tmp_path, data)
    for iteration in (1, 2):
        result = await drive(store)
        assert result.action == "waiting_projection"
        await terminal_projection(store, StepIdentity("implement", iteration))
        assert (await tick(store)).action == "consume_projection"
        await tick(store)  # initialize verify
        await tick(store)  # publish verify
        await external_activity_result(store)
        await tick(store)  # consume durable verify
        await tick(store)  # next iteration or repeat exit
        store = await reopen(store)
    result = await drive(store)
    assert result.run.ledger.committed.generated_tasks == 2
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM task_dags").fetchone()[0] == 2


async def test_readonly_tick_waits_without_mutation_or_spin(tmp_path):
    store = await setup(tmp_path)
    await tick(store)
    await tick(store)
    before = await store.get_workflow_run("run")
    for _ in range(5):
        assert not (await tick(store)).progressed
    assert await store.get_workflow_run("run") == before


async def test_competing_publication_does_not_grant_replay_ownership(tmp_path):
    store = await setup(tmp_path)
    await tick(store)
    run = await store.get_workflow_run("run")
    other = await reopen(store)
    args = {
        "expected_generation": run.generation,
        "owner_id": run.owner_id,
        "owner_fence": run.owner_fence,
        "updated_at": END,
    }
    results = await asyncio.gather(
        engine(store).advance_once("run", **args),
        engine(other).advance_once("run", **args),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) and r.progressed for r in results) == 1
    assert sum(isinstance(r, WorkflowStateError) for r in results) == 1
    run = await store.get_workflow_run("run")
    assert run.ledger.committed.generated_tasks == 1
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM task_dags").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workflow_expansions").fetchone()[0] == 1


async def test_budget_exhaustion_rolls_back_publication(tmp_path):
    from neuro_code.domain.workflows.state import BudgetAmounts

    store = await setup(tmp_path, ceiling=BudgetAmounts())
    await tick(store)
    before = await store.get_workflow_run("run")
    with pytest.raises(WorkflowStateError, match="ceiling"):
        await tick(store)
    assert await store.get_workflow_run("run") == before
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM task_dags").fetchone()[0] == 0


@pytest.mark.parametrize("event_kind", ["branch_decision", "iteration"])
async def test_control_commit_before_lost_ack_restarts_without_duplicate_fact(tmp_path, event_kind):
    data = source("branch" if event_kind == "branch_decision" else "repeat")
    store = await setup(tmp_path, data)
    await tick(store)
    await tick(store)  # publish invocation / Repeat RUNNING
    if event_kind == "branch_decision":
        await external_activity_result(store)
        await tick(store)
    original = store.transition_workflow_run

    async def lost_ack(run_id, change, **kwargs):
        result = await original(run_id, change, **kwargs)
        if change.kind.value == event_kind:
            raise RuntimeError("lost ack")
        return result

    with (
        patch.object(store, "transition_workflow_run", side_effect=lost_ack),
        pytest.raises(RuntimeError, match="lost ack"),
    ):
        await tick(store)
    store = await reopen(store)
    assert (await tick(store)).action == "initialize_step"
    events = await store.get_workflow_journal("run")
    assert len([e for e in events if e.kind.value == event_kind]) == 1


@pytest.mark.parametrize(
    ("method", "expected_next"),
    [
        ("publish_workflow_expansion", "waiting_projection"),
        ("commit_workflow_step_output", "control_flow_exhausted"),
    ],
)
async def test_publication_and_projection_commit_before_ack(tmp_path, method, expected_next):
    store = await setup(tmp_path)
    await tick(store)
    if method == "commit_workflow_step_output":
        await tick(store)
        await terminal_projection(store, StepIdentity("implement"))
    original = getattr(store, method)

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("lost ack")

    with (
        patch.object(store, method, side_effect=lost_ack),
        pytest.raises(RuntimeError, match="lost ack"),
    ):
        await tick(store)
    store = await reopen(store)
    assert (await tick(store)).action == expected_next
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM task_dags").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workflow_expansions").fetchone()[0] == 1
        if method == "commit_workflow_step_output":
            assert (
                connection.execute("SELECT output_json FROM workflow_step_outputs").fetchone()[0]
                is None
            )


async def test_cancelled_run_noop_and_no_fake_activity(tmp_path):
    from neuro_code.domain.workflows.state import WorkflowChange, WorkflowEventKind

    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
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
    with patch(
        "neuro_code.application.workflows.fake_workflow_activity.DeterministicFakeWorkflowActivity.evaluate",
        side_effect=AssertionError("cancelled execution"),
    ):
        assert not (await tick(store)).progressed


async def test_missing_exact_projection_does_not_use_preview(tmp_path):
    store = await setup(tmp_path)
    await tick(store)
    await tick(store)
    expansion = await store.get_workflow_expansion(expansion_id("run", StepIdentity("implement")))
    dag = await store.get_task_dag(expansion.dag_id)
    await finish(store, dag)
    before = await store.get_workflow_run("run")
    result = await tick(store)
    assert result.action == "waiting_projection"
    assert result.run == before


async def test_literal_artifact_and_itemref_scope(tmp_path):
    from neuro_code.application.workflows.workflow_control import ControlFacts
    from neuro_code.application.workflows.workflow_values import WorkflowValues
    from neuro_code.domain.workflows.definition import ArtifactRef, ItemRef, Literal

    store = await setup(tmp_path)
    run = await store.get_workflow_run("run")
    definition = await store.get_workflow_definition(run.definition_fingerprint)
    values = WorkflowValues(
        store, ControlFacts(definition, run, ()), await store.get_workflow_input("run")
    )
    assert await values.resolve(Literal("中文")) == "中文"
    artifact = ArtifactRef("artifact", "a" * 64, "workspace")
    assert (await values.resolve(artifact))["artifact_id"] == "artifact"
    with pytest.raises(WorkflowStateError, match="outside Map"):
        await values.resolve(ItemRef(()))
    assert await values.resolve(ItemRef(("name",)), item={"name": "甲"}) == "甲"


@pytest.mark.parametrize("mode", ["max_items", "node_limit"])
async def test_map_runtime_bounds_fail_closed_even_with_untrusted_port(tmp_path, mode):
    data = source("map")
    if mode == "node_limit":
        data["steps"][0]["batch"]["tasks"].append(
            {**data["steps"][0]["batch"]["tasks"][0], "task_id": "second"}
        )
    store = await setup(tmp_path, data)
    with (
        patch.object(
            store,
            "get_workflow_input",
            return_value=canonical({"targets": list(range(5)), "objective": "x"}),
        ),
        pytest.raises(WorkflowStateError, match="bound"),
    ):
        await tick(store)
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM task_dags").fetchone()[0] == 0


async def test_invalid_completed_activity_output_transaction_rejected(tmp_path):
    data = source()
    data["steps"] = [activity("verify")]
    store = await setup(tmp_path, data)
    await tick(store)
    await tick(store)
    run = await store.get_workflow_run("run")
    key = invocation_id("run", run.position, run.steps[0].input_fingerprint)
    attempt = await store.claim_workflow_activity(
        key, expected_revision=0, owner_id="external", reserved=BudgetAmounts(), updated_at=END
    )
    attempt = await store.start_workflow_activity(
        key, expected_revision=1, owner_id="external", owner_fence=1, updated_at=END
    )
    before = await store.get_workflow_run("run")
    invalid = WorkflowActivityResult(
        key,
        attempt.invocation.request_fingerprint,
        attempt.invocation.activity,
        WorkflowActivityState.COMPLETED,
        "source",
        "a" * 64,
        BudgetAmounts(),
        END,
        canonical({"status": "PASS", "workspace_generation": "not integer"}),
    )
    with pytest.raises(WorkflowStateError):
        await store.finish_workflow_activity(
            invalid, expected_revision=2, owner_id="external", owner_fence=1
        )
    assert await store.get_workflow_run("run") == before
    assert await store.get_workflow_step_output("run", StepIdentity("verify")) is None
