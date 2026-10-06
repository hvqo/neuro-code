"""DW4a snapshot-read and immutable fact insert, without Workflow advancement."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.task_dag import TaskDagNode, TaskDagNodeState, TaskDagState
from neuro_code.domain.workflows.definition import Map, TaskBatch
from neuro_code.domain.workflows.projection import (
    WorkflowResultProjection,
    projection_identity,
    validated_output_json,
)
from neuro_code.domain.workflows.publication import WorkflowPublicationResult, canonical, digest
from neuro_code.domain.workflows.state import fingerprint, identifier, timestamp
from neuro_code.domain.writable_subagent import WritableSubagentLeaseScope
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_dag_results import load_result_evidence
from neuro_code.infrastructure.persistence.sqlite_session_subagents import (
    _PARENT_CONTEXT_RELAY_SELECT,
    _WRITABLE_LEASE_SELECT,
    _parent_context_relay_from_row,
    _writable_lease_from_row,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_publication import (
    _load_publication,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _guard,
    _load_definition,
    _validate_position,
)
from neuro_code.shared.async_utils import run_blocking


class WorkflowProjectionMixin(_SqliteSessionPersistenceContext):
    async def project_workflow_result(
        self,
        expansion_id: str,
        *,
        run_id: str,
        parent_session_id: str,
        created_at: datetime,
        expected_source_fingerprint: str | None = None,
    ) -> WorkflowResultProjection:
        _scope_values(expansion_id, run_id, parent_session_id)
        timestamp(created_at)
        if expected_source_fingerprint is not None:
            fingerprint(expected_source_fingerprint)

        def project() -> WorkflowResultProjection:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                publication = _scoped_publication(
                    connection, expansion_id, run_id, parent_session_id
                )
                source, output = _project_facts(connection, publication)
                if (
                    expected_source_fingerprint is not None
                    and digest(json.loads(source)) != expected_source_fingerprint
                ):
                    raise WorkflowStateError("projection source conflict", kind="conflict")
                previous = _read_projection(connection, publication, source, output)
                if previous is not None:
                    return previous
                if publication.dag.updated_at is None or created_at < publication.dag.updated_at:
                    raise WorkflowStateError("projection predates terminal DAG", kind="protocol")
                result = WorkflowResultProjection(
                    projection_identity(publication.expansion.identity_fingerprint),
                    run_id,
                    parent_session_id,
                    expansion_id,
                    publication.expansion.step,
                    publication.dag.dag_id,
                    source,
                    output,
                    created_at.astimezone(UTC),
                )
                connection.execute(
                    "INSERT INTO workflow_result_projections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        expansion_id,
                        result.projection_id,
                        run_id,
                        parent_session_id,
                        result.step.key,
                        result.dag_id,
                        source,
                        output,
                        result.source_fingerprint,
                        result.fingerprint,
                        result.created_at.isoformat(),
                    ),
                )
                return result

        async with self._write_lock:
            return await run_blocking(lambda: _guard(project))

    async def get_workflow_result_projection(
        self, expansion_id: str, *, run_id: str, parent_session_id: str
    ) -> WorkflowResultProjection | None:
        _scope_values(expansion_id, run_id, parent_session_id)

        def read() -> WorkflowResultProjection | None:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                publication = _scoped_publication(
                    connection, expansion_id, run_id, parent_session_id
                )
                if (
                    connection.execute(
                        "SELECT 1 FROM workflow_result_projections WHERE expansion_id = ?",
                        (expansion_id,),
                    ).fetchone()
                    is None
                ):
                    return None
                source, output = _project_facts(connection, publication)
                return _read_projection(connection, publication, source, output)

        return await run_blocking(lambda: _guard(read))


def _scope_values(expansion_id: str, run_id: str, parent_session_id: str) -> None:
    for value in (expansion_id, run_id, parent_session_id):
        identifier(value)


def _scoped_publication(
    connection: sqlite3.Connection, expansion_id: str, run_id: str, parent_session_id: str
) -> WorkflowPublicationResult:
    result = _load_publication(connection, expansion_id)
    if result is None:
        raise WorkflowStateError("expansion is missing", kind="missing")
    if (result.run.run_id, result.run.parent_session_id) != (run_id, parent_session_id):
        raise WorkflowStateError("projection scope mismatch", kind="integrity")
    return result


def _project_facts(
    connection: sqlite3.Connection, publication: WorkflowPublicationResult
) -> tuple[str, str]:
    dag, expansion, run = publication.dag, publication.expansion, publication.run
    if not dag.state.terminal or not all(node.state.terminal for node in dag.nodes):
        raise WorkflowStateError("projection requires terminal DAG and nodes", kind="protocol")
    states = {node.state for node in dag.nodes}
    expected_state = (
        TaskDagState.INDETERMINATE
        if TaskDagNodeState.INDETERMINATE in states
        else TaskDagState.COMPLETED
        if states == {TaskDagNodeState.COMPLETED}
        else TaskDagState.CANCELLED
        if TaskDagNodeState.CANCELLED in states
        else TaskDagState.FAILED
    )
    if dag.state is not expected_state:
        raise WorkflowStateError("DAG/node terminal states disagree", kind="integrity")
    definition = _load_definition(connection, run.definition_fingerprint)
    if definition is None:
        raise WorkflowStateError("definition is missing", kind="integrity")
    declared = _validate_position(definition, expansion.step)
    if not isinstance(declared, TaskBatch | Map):
        raise WorkflowStateError("result source is not TaskBatch/Map", kind="protocol")
    facts: list[dict[str, object]] = []
    outputs: dict[str, dict[str, object]] = {}
    for member in expansion.members:
        node = dag.node(member.node_id)
        evidence, evidence_fingerprint = load_result_evidence(connection, dag.dag_id, node)
        if evidence is not None and evidence.truncated:
            raise WorkflowStateError(
                "truncated worker response is not an exact result", kind="incomplete_result"
            )
        if node.state is TaskDagNodeState.COMPLETED and evidence is None:
            raise WorkflowStateError(
                "completed worker exact result is missing", kind="incomplete_result"
            )
        if evidence is None and node.response_preview is not None:
            raise WorkflowStateError(
                "preview cannot substitute for exact response", kind="incomplete_result"
            )
        worker_fact = _worker_linkage(
            connection, dag.parent_session_id, node, has_result=evidence is not None
        )
        tasks = outputs.setdefault(member.member_key, {})
        tasks[member.task_id] = {
            "status": node.state.value,
            # Absence is explicit for a worker that produced no result, never success.
            "response": "" if evidence is None else evidence.response,
        }
        facts.append(
            {
                "member": asdict(member),
                "node": asdict(node),
                "result_fingerprint": evidence_fingerprint,
                "response_kind": "not_produced"
                if evidence is None
                else "exact_redacted_worker_response",
                "worker_linkage": worker_fact,
            }
        )
    value: object = (
        {"tasks": outputs["batch"]}
        if isinstance(declared, TaskBatch)
        else {"count": len(outputs), "items": [{"tasks": outputs[key]} for key in sorted(outputs)]}
    )
    output_json = validated_output_json(declared, value)
    source_json = canonical(
        {
            "version": 1,
            "run_id": run.run_id,
            "parent_session_id": run.parent_session_id,
            "definition_fingerprint": run.definition_fingerprint,
            "expansion_id": expansion.expansion_id,
            "expansion_identity": expansion.identity_fingerprint,
            "step": asdict(expansion.step),
            "input_fingerprint": expansion.input_fingerprint,
            "member_fingerprint": expansion.member_fingerprint,
            "dag_id": dag.dag_id,
            "dag_definition_fingerprint": dag.definition_fingerprint,
            "dag_generation": dag.generation,
            "dag_created_at": None
            if dag.created_at is None
            else dag.created_at.astimezone(UTC).isoformat(),
            "dag_updated_at": None
            if dag.updated_at is None
            else dag.updated_at.astimezone(UTC).isoformat(),
            "dag_state": dag.state.value,
            "max_parallel": dag.max_parallel,
            "nodes": facts,
        }
    )
    return source_json, output_json


def _worker_linkage(
    connection: sqlite3.Connection, parent_session_id: str, node: TaskDagNode, *, has_result: bool
) -> object:
    # A never-started skipped/cancelled node has no worker result. Completed
    # workers must retain exact durable task/lease/relay/workspace correlation.
    if node.state is not TaskDagNodeState.COMPLETED and not has_result:
        # Partial failure facts may have no worker allocation. Any durable
        # references that do exist must still belong to the exact parent scope.
        for table, key, value in (
            ("session_tasks", "task_id", node.parent_task_id),
            ("writable_subagent_leases", "lease_id", node.lease_id),
            ("parent_context_relays", "relay_id", node.relay_id),
        ):
            column = "session_id" if table == "session_tasks" else "parent_session_id"
            row = connection.execute(
                f"SELECT {column} FROM {table} WHERE {key} = ?", (value,)
            ).fetchone()
            if row is not None and row[0] != parent_session_id:
                raise WorkflowStateError("failure worker scope mismatch", kind="integrity")
        return None
    if any(
        value is None
        for value in (
            node.parent_task_id,
            node.child_session_id,
            node.lease_id,
            node.worktree_id,
            node.baseline_checkpoint_id,
            node.relay_id,
            node.final_workspace_fingerprint,
            node.changed_file_count,
        )
    ):
        raise WorkflowStateError("completed worker linkage is incomplete", kind="integrity")
    task = connection.execute(
        "SELECT session_id, status, kind FROM session_tasks WHERE task_id = ?",
        (node.parent_task_id,),
    ).fetchone()
    lease_row = connection.execute(
        _WRITABLE_LEASE_SELECT + " WHERE lease_id = ?", (node.lease_id,)
    ).fetchone()
    relay_row = connection.execute(
        _PARENT_CONTEXT_RELAY_SELECT + " WHERE relay_id = ?", (node.relay_id,)
    ).fetchone()
    if lease_row is None or relay_row is None:
        raise WorkflowStateError("worker lease/relay missing", kind="integrity")
    lease = _writable_lease_from_row(lease_row)
    relay = _parent_context_relay_from_row(relay_row)
    if (
        task is None
        or task[0] != parent_session_id
        or task[2] != "subagent"
        or lease.execution_scope is not WritableSubagentLeaseScope.TASK_DAG
        or (node.state is TaskDagNodeState.COMPLETED and task[1] != "completed")
        or (
            lease.parent_session_id,
            lease.parent_task_id,
            lease.child_session_id,
            lease.worktree_id.value,
            None if lease.baseline_checkpoint_id is None else lease.baseline_checkpoint_id.value,
            lease.final_workspace_fingerprint,
            lease.changed_file_count,
        )
        != (
            parent_session_id,
            node.parent_task_id,
            node.child_session_id,
            node.worktree_id,
            node.baseline_checkpoint_id,
            node.final_workspace_fingerprint,
            node.changed_file_count,
        )
        or (
            relay.parent_session_id,
            relay.parent_task_id,
            relay.child_session_id,
            relay.lease_id,
            relay.worktree_id.value,
            relay.baseline_checkpoint_id.value,
        )
        != (
            parent_session_id,
            node.parent_task_id,
            node.child_session_id,
            node.lease_id,
            node.worktree_id,
            node.baseline_checkpoint_id,
        )
        or relay.capability_fingerprint != lease.capability_fingerprint
        or relay.grant_fingerprint != lease.grant_fingerprint
        or relay.base_commit_sha != lease.base_commit_sha
        or relay.task_prompt_fingerprint != node.prompt_fingerprint
    ):
        raise WorkflowStateError("worker task/lease/relay scope mismatch", kind="integrity")
    return {
        "task": task,
        "relay_fingerprint": relay.integrity_fingerprint,
        "base_commit_sha": lease.base_commit_sha,
        "capability_fingerprint": lease.capability_fingerprint,
        "grant_fingerprint": lease.grant_fingerprint,
    }


def _read_projection(
    connection: sqlite3.Connection, publication: WorkflowPublicationResult, source: str, output: str
) -> WorkflowResultProjection | None:
    row = connection.execute(
        "SELECT projection_id, run_id, parent_session_id, step_key, dag_id, source_json, output_json, source_fingerprint, projection_fingerprint, created_at FROM workflow_result_projections WHERE expansion_id = ?",
        (publication.expansion.expansion_id,),
    ).fetchone()
    if row is None:
        return None
    result = WorkflowResultProjection(
        row[0],
        row[1],
        row[2],
        publication.expansion.expansion_id,
        publication.expansion.step,
        row[4],
        row[5],
        row[6],
        datetime.fromisoformat(row[9]),
    )
    if (
        (row[0], row[1], row[2], row[3], row[4])
        != (
            projection_identity(publication.expansion.identity_fingerprint),
            publication.run.run_id,
            publication.run.parent_session_id,
            publication.expansion.step.key,
            publication.dag.dag_id,
        )
        or source != result.source_json
        or output != result.output_json
        or row[7] != result.source_fingerprint
        or row[8] != result.fingerprint
        or publication.dag.updated_at is None
        or result.created_at < publication.dag.updated_at
    ):
        raise WorkflowStateError("projection source/payload integrity mismatch", kind="integrity")
    return result
