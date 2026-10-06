"""DW4a exact source identity and real SQLite result persistence invariants."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from neuro_code.application.ports.task_dag import TaskDagError
from neuro_code.application.ports.workflow_projection import WorkflowProjectionStore
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.checkpoints import CheckpointId
from neuro_code.domain.conversation.messages import Role
from neuro_code.domain.parent_context_relay import ParentContextRelay, ParentContextRelayItem
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.task_dag import TaskDag, TaskDagNode
from neuro_code.domain.task_dag import TaskDagNodeState as NodeState
from neuro_code.domain.task_dag import TaskDagState as DagState
from neuro_code.domain.task_dag_result import TaskDagResultEvidence
from neuro_code.domain.workflows.definition import FieldSchema, SchemaKind, compile_workflow
from neuro_code.domain.workflows.projection import validate_output
from neuro_code.domain.workflows.publication import ExpansionMember, WorkflowExpansionIntent
from neuro_code.domain.workflows.state import WorkflowStatus
from neuro_code.domain.worktree import WorktreeId, WorktreeRepositoryIdentity
from neuro_code.domain.writable_subagent import (
    WritableSubagentLeaseScope,
    WritableSubagentWorkspaceLease,
    WritableSubagentWorkspaceState,
)
from neuro_code.infrastructure.persistence import sqlite_session_core as core
from neuro_code.infrastructure.persistence import sqlite_session_dag as dag_owner
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.infrastructure.persistence.sqlite_session_subagents import (
    _parent_context_relay_values,
    _writable_lease_values,
)
from tests.architecture.test_workflow_publication import INPUT, NOW, intent, publish, setup

END = NOW + timedelta(seconds=30)


async def finish(
    store,
    dag,
    state=NodeState.COMPLETED,
    *,
    response="完整 worker response",
    truncated=False,
    evidence=True,
):
    if dag.state is DagState.READY:
        dag = await store.compare_and_transition_task_dag(
            replace(dag, state=DagState.RUNNING, generation=dag.generation + 1, updated_at=END),
            expected_generation=dag.generation,
            expected_state=dag.state,
        )
    for node in dag.nodes:
        if state in {NodeState.SKIPPED, NodeState.CANCELLED}:
            dag = await store.compare_and_transition_task_dag_node(
                dag.dag_id,
                replace(node, state=state, generation=node.generation + 1),
                expected_generation=node.generation,
                expected_state=node.state,
            )
            continue
        if node.state is NodeState.PENDING:
            ready_node = replace(node, state=NodeState.READY, generation=node.generation + 1)
            dag = await store.compare_and_transition_task_dag_node(
                dag.dag_id,
                ready_node,
                expected_generation=node.generation,
                expected_state=NodeState.PENDING,
            )
            node = ready_node
        node = replace(
            node,
            state=NodeState.RUNNING,
            generation=node.generation + 1,
            parent_task_id="task-" + node.node_id,
        )
        dag = await store.claim_task_dag_node(
            dag.dag_id,
            node,
            expected_generation=node.generation - 1,
            expected_state=NodeState.READY,
            updated_at=END,
        )
        child = await store.create_session("/worker", "provider", "model")
        task = SessionTask(
            node.parent_task_id, SessionTaskKind.SUBAGENT, SessionTaskStatus.COMPLETED, NOW, END
        )
        await store.create_session_task(dag.parent_session_id, task)
        root = store.database_path.parent
        repository = WorktreeRepositoryIdentity(root, root, root / "git", "a" * 40)
        lease = WritableSubagentWorkspaceLease(
            lease_id="lease-" + node.node_id,
            parent_session_id=dag.parent_session_id,
            parent_task_id=node.parent_task_id,
            child_session_id=child,
            worktree_id=WorktreeId("worktree-" + node.node_id),
            parent_capability_fingerprint=INPUT,
            parent_workspace_root=root,
            parent_repository=repository,
            base_commit_sha="a" * 40,
            canonical_child_root=root / child,
            state=WritableSubagentWorkspaceState.PRESERVED,
            created_at=NOW,
            updated_at=END,
            baseline_checkpoint_id=CheckpointId("cp-" + node.node_id),
            capability_fingerprint=INPUT,
            grant_fingerprint=INPUT,
            final_workspace_fingerprint="b" * 64,
            changed_file_count=0,
            execution_scope=WritableSubagentLeaseScope.TASK_DAG,
        )
        relay = ParentContextRelay.create(
            relay_id="relay-" + node.node_id,
            parent_session_id=dag.parent_session_id,
            parent_task_id=node.parent_task_id,
            child_session_id=child,
            lease_id=lease.lease_id,
            worktree_id=lease.worktree_id,
            baseline_checkpoint_id=lease.baseline_checkpoint_id,
            base_commit_sha=lease.base_commit_sha,
            capability_fingerprint=INPUT,
            grant_fingerprint=INPUT,
            task_prompt_fingerprint=node.prompt_fingerprint,
            source_item_count=1,
            items=(ParentContextRelayItem(0, Role.USER, "bounded source", False),),
            truncated=False,
            created_at=NOW,
        )
        # Seed completed worker evidence without executing a worker or touching a workspace.
        with closing(store._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO writable_subagent_leases VALUES ("
                + ",".join("?" for _ in range(33))
                + ")",
                _writable_lease_values(lease),
            )
            connection.execute(
                "INSERT INTO parent_context_relays VALUES ("
                + ",".join("?" for _ in range(20))
                + ")",
                (*_parent_context_relay_values(relay), "ready"),
            )
        proposed = replace(
            node,
            state=state,
            generation=node.generation + 1,
            child_session_id=child,
            lease_id=lease.lease_id,
            worktree_id=lease.worktree_id.value,
            baseline_checkpoint_id=lease.baseline_checkpoint_id.value,
            relay_id=relay.relay_id,
            final_workspace_fingerprint=lease.final_workspace_fingerprint,
            changed_file_count=0,
            response_preview=response.encode("utf-8")[:8192].decode("utf-8", errors="ignore")
            if evidence
            else None,
        )
        dag = await store.finish_task_dag_node(
            dag.dag_id,
            proposed,
            expected_generation=node.generation,
            expected_state=NodeState.RUNNING,
            updated_at=END,
            result_evidence=TaskDagResultEvidence(node.parent_task_id, child, response, truncated)
            if evidence
            else None,
        )
    target = (
        DagState.COMPLETED
        if state is NodeState.COMPLETED
        else DagState.INDETERMINATE
        if state is NodeState.INDETERMINATE
        else DagState.CANCELLED
        if state is NodeState.CANCELLED
        else DagState.FAILED
    )
    return await store.compare_and_transition_task_dag(
        replace(dag, state=target, generation=dag.generation + 1, updated_at=END),
        expected_generation=dag.generation,
        expected_state=dag.state,
    )


async def ready(tmp_path, **kwargs):
    mapped = kwargs.pop("mapped", False)
    count = kwargs.pop("count", 1)
    store, run = await setup(tmp_path, mapped=mapped)
    publication = await publish(store, run, intent(run, mapped=mapped, count=count))
    dag = await finish(store, publication.dag, **kwargs)
    return store, publication, dag


async def project(store, publication, **kwargs):
    typed: WorkflowProjectionStore = store
    return await typed.project_workflow_result(
        publication.expansion.expansion_id,
        run_id=publication.run.run_id,
        parent_session_id=publication.run.parent_session_id,
        created_at=END,
        **kwargs,
    )


async def test_completed_projection_exact_response_and_no_workflow_advance(tmp_path):
    response = "已完成。" * 1800  # exceeds preview but fits exact result contract
    store, publication, dag = await ready(tmp_path, response=response)
    before = await store.get_workflow_run(publication.run.run_id)
    journal = await store.get_workflow_journal(publication.run.run_id)
    result = await project(store, publication)
    assert json.loads(result.output_json) == {
        "tasks": {"work": {"status": "completed", "response": response}}
    }
    source = json.loads(result.source_json)
    assert source["dag_generation"] == dag.generation
    assert source["nodes"][0]["node"]["generation"] == dag.nodes[0].generation
    assert source["nodes"][0]["response_kind"] == "exact_redacted_worker_response"
    assert "verification" not in result.output_json
    assert before == await store.get_workflow_run(publication.run.run_id)
    assert before.status is WorkflowStatus.WAITING
    assert journal == await store.get_workflow_journal(publication.run.run_id)
    assert await project(store, publication) == result
    reopened = SqliteSessionStore(store.database_path)
    assert (
        await reopened.get_workflow_result_projection(
            result.expansion_id, run_id=result.run_id, parent_session_id=result.parent_session_id
        )
        == result
    )


async def test_map_frozen_bindings_determine_items_and_tasks(tmp_path):
    store, publication, _ = await ready(tmp_path, mapped=True, count=7)
    result = await project(store, publication)
    output = json.loads(result.output_json)
    assert output["count"] == 7
    source = json.loads(result.source_json)
    assert [s["member"]["member_key"] for s in source["nodes"]] == [
        f"item-{i:02}" for i in range(7)
    ]
    assert all(item["tasks"]["work"]["status"] == "completed" for item in output["items"])


@pytest.mark.parametrize(
    "state", [NodeState.FAILED, NodeState.SKIPPED, NodeState.CANCELLED, NodeState.INDETERMINATE]
)
async def test_failure_and_absence_are_not_success(tmp_path, state):
    store, publication, _ = await ready(tmp_path, state=state, evidence=False)
    output = json.loads((await project(store, publication)).output_json)
    assert output["tasks"]["work"] == {"status": state.value, "response": ""}


@pytest.mark.parametrize(("truncated", "evidence"), [(True, True), (False, False)])
async def test_truncated_or_missing_success_response_rejected(tmp_path, truncated, evidence):
    store, publication, _ = await ready(tmp_path, truncated=truncated, evidence=evidence)
    with pytest.raises(WorkflowStateError, match=r"truncated|missing"):
        await project(store, publication)


async def test_nonterminal_dag_and_nodes_rejected(tmp_path):
    store, run = await setup(tmp_path)
    publication = await publish(store, run, intent(run))
    with pytest.raises(WorkflowStateError, match="terminal"):
        await project(store, publication)
    assert (
        await store.get_workflow_result_projection(
            publication.expansion.expansion_id,
            run_id=run.run_id,
            parent_session_id=run.parent_session_id,
        )
        is None
    )


async def test_competing_projection_calls_create_one_fact(tmp_path):
    store, publication, _ = await ready(tmp_path)
    other = SqliteSessionStore(store.database_path)
    results = await asyncio.gather(project(store, publication), project(other, publication))
    assert results[0] == results[1]
    with closing(store._connect()) as connection:
        assert connection.execute(
            "SELECT count(*) FROM workflow_result_projections"
        ).fetchone() == (1,)


@pytest.mark.parametrize("scope", ["run_id", "parent_session_id", "expansion_id"])
async def test_cross_scope_and_missing_expansion_rejected(tmp_path, scope):
    store, publication, _ = await ready(tmp_path)
    values = {
        "expansion_id": publication.expansion.expansion_id,
        "run_id": publication.run.run_id,
        "parent_session_id": publication.run.parent_session_id,
        "created_at": END,
    }
    values[scope] = "another-identity"
    with pytest.raises(WorkflowStateError):
        await store.project_workflow_result(**values)


async def test_expected_source_conflict_and_replay(tmp_path):
    store, publication, _ = await ready(tmp_path)
    result = await project(store, publication)
    assert (
        await project(store, publication, expected_source_fingerprint=result.source_fingerprint)
        == result
    )
    with pytest.raises(WorkflowStateError, match="source conflict"):
        await project(store, publication, expected_source_fingerprint="f" * 64)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("generation", 999),
        ("child_session_id", "other"),
        ("final_workspace_fingerprint", "f" * 64),
        ("response_preview", "changed"),
        ("prompt", "changed"),
    ],
)
async def test_node_tamper_rejected_on_reopen(tmp_path, column, value):
    store, publication, _ = await ready(tmp_path)
    await project(store, publication)
    with closing(store._connect()) as connection, connection:
        connection.execute(
            f"UPDATE task_dag_nodes SET {column}=? WHERE dag_id=?", (value, publication.dag.dag_id)
        )
    with pytest.raises((WorkflowStateError, TaskDagError, ValueError)):
        await project(SqliteSessionStore(store.database_path), publication)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("output_json", '{"tasks":{}}'),
        ("source_json", "{}"),
        ("source_fingerprint", "f" * 64),
        ("run_id", "other"),
        ("step_key", "other"),
        ("projection_fingerprint", "f" * 64),
    ],
)
async def test_projection_tamper_detected(tmp_path, column, value):
    store, publication, _ = await ready(tmp_path)
    await project(store, publication)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TRIGGER workflow_result_projections_immutable_update")
        connection.execute(f"UPDATE workflow_result_projections SET {column}=?", (value,))
    with pytest.raises(WorkflowStateError, match="integrity"):
        await project(store, publication)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("task_dag_result_evidence", "payload_json", "null"),
        ("writable_subagent_leases", "parent_session_id", "other"),
        ("parent_context_relays", "content_fingerprint", "f" * 64),
    ],
)
async def test_result_worker_source_tamper_detected(tmp_path, table, column, value):
    store, publication, _ = await ready(tmp_path)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        if table == "task_dag_result_evidence":
            connection.execute("DROP TRIGGER task_dag_result_evidence_immutable_update")
        connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    with pytest.raises(WorkflowStateError):
        await project(store, publication)


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
@pytest.mark.parametrize("table", ["workflow_result_projections", "task_dag_result_evidence"])
async def test_result_facts_are_immutable(tmp_path, table, operation):
    store, publication, _ = await ready(tmp_path)
    await project(store, publication)
    with closing(store._connect()) as connection, connection:
        sql = (
            f"DELETE FROM {table}" if operation == "DELETE" else f"UPDATE {table} SET dag_id=dag_id"
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(sql)


async def test_projection_insert_rollback_does_not_touch_workflow(tmp_path):
    store, publication, _ = await ready(tmp_path)
    before = await store.get_workflow_run(publication.run.run_id)
    with closing(store._connect()) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_projection BEFORE INSERT ON workflow_result_projections BEGIN SELECT RAISE(ABORT,'crash window'); END"
        )
    with pytest.raises(WorkflowStateError):
        await project(store, publication)
    assert await store.get_workflow_run(publication.run.run_id) == before
    with closing(store._connect()) as connection:
        assert connection.execute(
            "SELECT count(*) FROM workflow_result_projections"
        ).fetchone() == (0,)


async def test_terminal_result_capture_rolls_back_with_node_finish(tmp_path):
    store, run = await setup(tmp_path)
    publication = await publish(store, run, intent(run))
    original = dag_owner.persist_result_evidence

    def crash(connection, *args):
        original(connection, *args)
        raise RuntimeError("after exact-result insert")

    with patch.object(dag_owner, "persist_result_evidence", crash), pytest.raises(RuntimeError):
        await finish(store, publication.dag)
    current = await store.get_task_dag(publication.dag.dag_id)
    assert current.nodes[0].state is NodeState.RUNNING
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT count(*) FROM task_dag_result_evidence").fetchone() == (
            0,
        )


async def test_migration_37_to_38_and_rollback(tmp_path):
    store, run = await setup(tmp_path)
    with closing(store._connect()) as connection, connection:
        connection.execute("DROP TABLE workflow_result_projections")
        connection.execute("DROP TABLE task_dag_result_evidence")
        connection.execute("UPDATE schema_meta SET version=37")
    original = core._ensure_workflow_projection_schema

    def crash(connection):
        original(connection)
        raise RuntimeError("migration crash")

    with (
        patch.object(core, "_ensure_workflow_projection_schema", crash),
        pytest.raises(RuntimeError),
    ):
        await store.initialize()
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT version FROM schema_meta").fetchone() == (37,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='task_dag_result_evidence'"
            ).fetchone()
            is None
        )
    await store.initialize()
    assert SCHEMA_VERSION == 38
    assert await store.get_workflow_run(run.run_id) == run
    with closing(store._connect()) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        (FieldSchema(SchemaKind.STRING), 1),
        (FieldSchema(SchemaKind.INTEGER), True),
        (FieldSchema(SchemaKind.OBJECT), {"unknown": 1}),
        (
            FieldSchema(SchemaKind.ARRAY, items=FieldSchema(SchemaKind.STRING), max_items=1),
            ["a", "b"],
        ),
        (FieldSchema(SchemaKind.BOOLEAN), True),
    ],
)
def test_closed_schema_validation_rejects_coercion_and_extra_fields(schema, value):
    with pytest.raises(ValueError, match=r"projection|unsupported"):
        validate_output(schema, value)


async def test_multiple_tasks_map_response_by_frozen_task_binding(tmp_path):
    store, _ = await setup(tmp_path)
    from tests.architecture.test_workflow_publication import FIXTURES

    data = json.loads((FIXTURES / "minimal.json").read_text(encoding="utf-8"))
    task = data["steps"][0]["tasks"][0]
    other = {**task, "task_id": "alpha"}
    data["steps"][0]["tasks"] = [task, other]
    definition = compile_workflow(json.dumps(data))
    await store.insert_workflow_definition(definition)
    session = await store.create_session("/parent", "provider", "model")
    run = (
        await store.create_workflow_run(
            "two-tasks",
            definition_fingerprint=definition.fingerprint,
            parent_session_id=session,
            input_fingerprint=INPUT,
            request_id="create-two",
            created_at=NOW,
        )
    ).run
    run = (
        await store.claim_workflow_run(
            run.run_id,
            expected_generation=0,
            expected_owner_fence=0,
            owner_id="owner",
            request_id="claim-two",
            updated_at=NOW,
        )
    ).run
    from neuro_code.domain.task_dag import TaskDag, TaskDagNode
    from neuro_code.domain.workflows.state import StepIdentity

    # IDs and prompts deliberately differ from task order/names.
    members = (
        ExpansionMember("batch", "work", "unrelated-a", INPUT),
        ExpansionMember("batch", "alpha", "unrelated-z", INPUT),
    )
    dag = TaskDag.create(
        dag_id="two-dag",
        parent_session_id=session,
        nodes=(
            TaskDagNode("unrelated-z", 0, "alpha prompt"),
            TaskDagNode("unrelated-a", 1, "work prompt"),
        ),
        max_parallel=1,
        created_at=NOW,
    )
    proposal = WorkflowExpansionIntent(
        "two-expansion", run.run_id, StepIdentity("implement"), INPUT, members, dag
    )
    publication = await publish(store, run, proposal)
    await finish(store, publication.dag, response="shared exact text")
    result = await project(store, publication)
    source = json.loads(result.source_json)
    assert [(f["member"]["task_id"], f["node"]["prompt"]) for f in source["nodes"]] == [
        ("alpha", "alpha prompt"),
        ("work", "work prompt"),
    ]
    assert set(json.loads(result.output_json)["tasks"]) == {"alpha", "work"}


async def test_no_parent_workspace_mutation_and_no_execution_entry(tmp_path):
    sentinel = tmp_path / "parent-content.txt"
    sentinel.write_text("原始 workspace", encoding="utf-8")
    store, publication, _ = await ready(tmp_path)
    before = sentinel.read_bytes()
    with patch(
        "neuro_code.application.workflows.task_dag.TaskDagApplicationService.run_task_dag",
        side_effect=AssertionError("projection must not execute"),
    ):
        await project(store, publication)
    assert sentinel.read_bytes() == before
    assert not list(tmp_path.glob("**/.git"))


async def test_legacy_preview_is_not_full_response(tmp_path):
    store, publication, _ = await ready(tmp_path)
    with closing(store._connect()) as connection, connection:
        connection.execute("DROP TRIGGER task_dag_result_evidence_immutable_delete")
        connection.execute("DELETE FROM task_dag_result_evidence")
    with pytest.raises(WorkflowStateError, match="missing"):
        await project(store, publication)


@pytest.mark.parametrize(
    ("field", "value"), [("parent_task_id", "different"), ("child_session_id", "different")]
)
async def test_exact_result_capture_identity_mismatch_rolls_back(tmp_path, field, value):
    store, run = await setup(tmp_path)
    publication = await publish(store, run, intent(run))
    original = dag_owner.persist_result_evidence

    def mismatch(connection, dag_id, node, evidence):
        return original(connection, dag_id, node, replace(evidence, **{field: value}))

    with (
        patch.object(dag_owner, "persist_result_evidence", mismatch),
        pytest.raises(TaskDagError, match="identity mismatch"),
    ):
        await finish(store, publication.dag)
    assert (await store.get_task_dag(publication.dag.dag_id)).nodes[0].state is NodeState.RUNNING


async def test_terminal_dag_with_nonterminal_node_or_inconsistent_status_rejected(tmp_path):
    store, run = await setup(tmp_path)
    publication = await publish(store, run, intent(run))
    with closing(store._connect()) as connection, connection:
        connection.execute(
            "UPDATE task_dags SET state='completed' WHERE dag_id=?", (publication.dag.dag_id,)
        )
    with pytest.raises(WorkflowStateError, match="terminal"):
        await project(store, publication)
    store2, publication2, _ = await ready(tmp_path / "consistent")
    with closing(store2._connect()) as connection, connection:
        connection.execute(
            "UPDATE task_dags SET state='failed' WHERE dag_id=?", (publication2.dag.dag_id,)
        )
    with pytest.raises(WorkflowStateError, match="disagree"):
        await project(store2, publication2)


async def test_failed_result_scope_is_verified_too(tmp_path):
    store, publication, _ = await ready(tmp_path, state=NodeState.FAILED)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("UPDATE session_tasks SET session_id='another-session'")
    with pytest.raises(WorkflowStateError, match="scope mismatch"):
        await project(store, publication)


async def test_failure_without_result_does_not_admit_cross_session_reference(tmp_path):
    store, publication, _ = await ready(tmp_path, state=NodeState.FAILED, evidence=False)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute(
            "UPDATE writable_subagent_leases SET parent_session_id='another-session'"
        )
    with pytest.raises(WorkflowStateError, match="scope mismatch"):
        await project(store, publication)


@pytest.mark.parametrize(
    ("field", "value"), [("created_at", NOW.isoformat()), ("projection_id", "other")]
)
async def test_projection_metadata_tamper_detected(tmp_path, field, value):
    store, publication, _ = await ready(tmp_path)
    await project(store, publication)
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute("DROP TRIGGER workflow_result_projections_immutable_update")
        connection.execute(f"UPDATE workflow_result_projections SET {field}=?", (value,))
    with pytest.raises(WorkflowStateError, match="integrity"):
        await project(store, publication)


def test_projection_digest_deterministic_across_hash_seeds():
    import os
    import subprocess
    import sys

    code = """from datetime import datetime, UTC
from neuro_code.domain.workflows.projection import WorkflowResultProjection
from neuro_code.domain.workflows.state import StepIdentity
from neuro_code.domain.workflows.publication import canonical
p = WorkflowResultProjection("projection", "run", "session", "expansion", StepIdentity("step",1,"中文"), "dag", canonical({k:k for k in {"甲", "a", "z"}}), canonical({"tasks":{"task":{"status":"completed","response":"中文\\nEnglish"}}}), datetime(2026,10,6,tzinfo=UTC))
print(p.source_fingerprint,p.fingerprint,p.output_json)
"""
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"},
        )
        for seed in ("1", "7", "101")
    ]
    assert outputs[0] == outputs[1] == outputs[2]


async def test_completed_standalone_lease_cannot_be_used_as_dag_result(tmp_path):
    store, publication, _ = await ready(tmp_path)
    with closing(store._connect()) as connection, connection:
        connection.execute("UPDATE writable_subagent_leases SET execution_scope='standalone'")
    with pytest.raises(WorkflowStateError, match="scope mismatch"):
        await project(store, publication)


async def test_claimed_worker_cannot_be_replaced_at_finish(tmp_path):
    store, run = await setup(tmp_path)
    publication = await publish(store, run, intent(run))
    original = store.finish_task_dag_node

    async def swapped(dag_id, node, **kwargs):
        proposed = replace(node, parent_task_id="another-worker")
        return await original(
            dag_id,
            proposed,
            **{
                **kwargs,
                "result_evidence": replace(
                    kwargs["result_evidence"], parent_task_id="another-worker"
                ),
            },
        )

    with (
        patch.object(store, "finish_task_dag_node", swapped),
        pytest.raises(TaskDagError, match="claimed worker"),
    ):
        await finish(store, publication.dag)
    assert (await store.get_task_dag(publication.dag.dag_id)).nodes[0].state is NodeState.RUNNING


async def test_unbound_dag_finish_preserves_existing_session_cleanup(tmp_path):
    store = SqliteSessionStore(tmp_path / "ordinary.db")
    await store.initialize()
    session = await store.create_session("/workspace", "provider", "model")
    ready = TaskDagNode("a", 0, "prompt", state=NodeState.READY, generation=1)
    dag = TaskDag("ordinary", session, (ready,), created_at=NOW, updated_at=NOW)
    await store.insert_task_dag(dag)
    running = replace(ready, state=NodeState.RUNNING, generation=2, parent_task_id="task-a")
    await store.claim_task_dag_node(
        dag.dag_id, running, expected_generation=1, expected_state=NodeState.READY, updated_at=END
    )
    await store.finish_task_dag_node(
        dag.dag_id,
        replace(running, state=NodeState.FAILED, generation=3, error_reason="allocation failed"),
        expected_generation=2,
        expected_state=NodeState.RUNNING,
        updated_at=END,
    )
    # Ordinary Task DAGs do not acquire new Workflow-retention constraints.
    await store.delete_session(session)
    assert await store.get_task_dag(dag.dag_id) is None
