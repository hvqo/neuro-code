"""Read-only exact ADOPT source and terminal proof; no Activity write dependency."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime

from neuro_code.application.ports.result_adoption import ResultAdoptionRecord
from neuro_code.application.ports.workflow_adoption import (
    adoption_activity_state,
    adoption_recovery_usage,
    adoption_terminal_digest,
    workflow_adoption_id,
)
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.completed_dag_adoption import (
    CompletedDagAdoptionSource,
    CompletedDagSourceKind,
    WorkflowAdoptionSourceRef,
)
from neuro_code.domain.result_adoption import ResultAdoptionRequest
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityState
from neuro_code.domain.workflows.definition import Activity, ActivityKind, Map, ResultRef, TaskBatch
from neuro_code.domain.workflows.interpreter import (
    OutputKind,
    WorkflowStepOutput,
    expansion_id,
    validate_step_output,
)
from neuro_code.domain.workflows.projection import WorkflowResultProjection, projection_identity
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import StepIdentity, WorkflowEventKind, WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session_result_adoption import (
    _load_result_adoption,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_publication import (
    _load_publication,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _load_definition,
    _load_run,
    _validate_position,
)


def _request(
    connection: sqlite3.Connection,
    attempt: WorkflowActivityAttempt | None,
) -> ResultAdoptionRequest:
    if attempt is None or attempt.invocation.activity is not ActivityKind.ADOPT:
        raise WorkflowStateError("exact ADOPT invocation required", kind="protocol")
    invocation = attempt.invocation
    run = _load_run(connection, invocation.run_id)
    if run is None:
        raise WorkflowStateError("ADOPT run missing", kind="integrity")
    definition = _load_definition(connection, run.definition_fingerprint)
    if definition is None:
        raise WorkflowStateError("ADOPT definition missing", kind="integrity")
    declared = _validate_position(definition, invocation.step)
    if not isinstance(declared, Activity):
        raise WorkflowStateError("ADOPT declaration missing", kind="integrity")
    ref = dict(declared.inputs).get("source")
    if not isinstance(ref, ResultRef) or ref.iteration is not None or ref.item_key is not None:
        raise WorkflowStateError(
            "ADOPT source must be an unambiguous direct ResultRef", kind="protocol"
        )
    identity = StepIdentity(ref.step_id, invocation.step.iteration)
    linked = next((s for s in run.steps if s.identity == identity), None)
    if linked is None:
        identity = StepIdentity(ref.step_id)
        linked = next((s for s in run.steps if s.identity == identity), None)
    source_step = _validate_position(definition, identity)
    if (
        not isinstance(source_step, TaskBatch | Map)
        or linked is None
        or linked.status is not WorkflowStatus.COMPLETED
    ):
        raise WorkflowStateError("ADOPT requires an exact completed TaskBatch/Map", kind="protocol")
    if ref.field_path != (("tasks",) if isinstance(source_step, TaskBatch) else ("items",)):
        raise WorkflowStateError(
            "ADOPT source selects whole batch tasks or Map items", kind="protocol"
        )
    # Resolve provenance from the immutable, consumed output index and journal.
    # Business JSON is only compared with that exact typed output, never read as
    # a caller-provided DAG/source identity. Live execution still uses DW4b's
    # strict Projection/DAG/resource validation; historical terminal replay does
    # not depend on those execution-time resources remaining available.
    output_row = connection.execute(
        "SELECT kind, source_id, source_fingerprint, input_fingerprint, output_json, output_fingerprint, generation FROM workflow_step_outputs WHERE run_id = ? AND step_key = ?",
        (run.run_id, identity.key),
    ).fetchone()
    if (
        output_row is None
        or output_row[0] != OutputKind.PROJECTION.value
        or output_row[4] is not None
    ):
        raise WorkflowStateError("ADOPT requires an exact consumed Projection", kind="integrity")
    # DW5a stores Projection output once, not a second large copy. The immutable
    # Activity request contains the resolved whole tasks/items value. Reconstruct
    # its DW1 shape solely to verify the already-consumed output fingerprint.
    selected = json.loads(invocation.request_json).get("source")
    if isinstance(source_step, Map) and type(selected) is not list:
        raise WorkflowStateError("ADOPT source value differs", kind="integrity")
    value = (
        {"tasks": selected}
        if isinstance(source_step, TaskBatch)
        else {"count": len(selected), "items": selected}
    )
    output = WorkflowStepOutput(
        run.run_id,
        identity,
        output_row[3],
        OutputKind.PROJECTION,
        output_row[1],
        output_row[2],
        canonical(value),
    )
    validate_step_output(source_step, output)
    if output.fingerprint != output_row[5] or output.input_fingerprint != linked.input_fingerprint:
        raise WorkflowStateError("ADOPT consumed output differs", kind="integrity")
    event = connection.execute(
        "SELECT kind, payload_json FROM workflow_transition_journal WHERE run_id = ? AND generation = ?",
        (run.run_id, output_row[6]),
    ).fetchone()
    if output_row[6] > run.generation or event != (
        WorkflowEventKind.STEP.value,
        canonical(
            {
                "operation": "consume_typed_output",
                "step_key": identity.key,
                "output_fingerprint": output.fingerprint,
            }
        ),
    ):
        raise WorkflowStateError("ADOPT output journal differs", kind="integrity")
    if (
        json.loads(invocation.request_json).get("source")
        != json.loads(output.output_json)[ref.field_path[0]]
    ):
        raise WorkflowStateError("ADOPT source input differs", kind="integrity")
    source = WorkflowAdoptionSourceRef(
        run.run_id, expansion_id(run.run_id, identity), output.source_id, output.source_fingerprint
    )
    return ResultAdoptionRequest(workflow_adoption_id(invocation, source), workflow_source=source)


def _verify_durable_source(
    connection: sqlite3.Connection,
    attempt: WorkflowActivityAttempt,
    record: ResultAdoptionRecord,
    request: ResultAdoptionRequest,
) -> None:
    """Cross-check frozen SQLite provenance, never live execution resources.

    The consumed output pins the immutable Projection fingerprint. Its source
    snapshot binds terminal generations and worker facts; DW3 independently
    pins members, definition and publication journal. A rehashed plan cannot
    substitute its own DAG or worker identities for those historical facts.
    """
    ref = request.workflow_source
    assert ref is not None
    publication = _load_publication(connection, ref.expansion_id)
    row = connection.execute(
        "SELECT projection_id, run_id, parent_session_id, step_key, dag_id, source_json, "
        "output_json, source_fingerprint, projection_fingerprint, created_at "
        "FROM workflow_result_projections WHERE expansion_id = ?",
        (ref.expansion_id,),
    ).fetchone()
    if publication is None or row is None:
        raise WorkflowStateError("ADOPT immutable provenance is missing", kind="integrity")
    expansion, run, dag = publication.expansion, publication.run, publication.dag
    projection = WorkflowResultProjection(
        row[0],
        row[1],
        row[2],
        ref.expansion_id,
        expansion.step,
        row[4],
        row[5],
        row[6],
        datetime.fromisoformat(row[9]),
    )
    if (
        run.run_id != attempt.invocation.run_id
        or run.parent_session_id != attempt.invocation.parent_session_id
        or projection.projection_id != projection_identity(expansion.identity_fingerprint)
        or projection.projection_id != ref.projection_id
        or projection.fingerprint != ref.projection_fingerprint
        or projection.fingerprint != row[8]
        or projection.source_fingerprint != row[7]
        or projection.run_id != run.run_id
        or projection.parent_session_id != run.parent_session_id
        or row[3] != expansion.step.key
        or projection.dag_id != expansion.dag_id
        or record.adoption_id != request.adoption_id
    ):
        raise WorkflowStateError("ADOPT immutable projection linkage differs", kind="integrity")
    facts = json.loads(projection.source_json)
    if (
        facts["version"] != 1
        or facts["run_id"] != run.run_id
        or facts["parent_session_id"] != run.parent_session_id
        or facts["definition_fingerprint"] != run.definition_fingerprint
        or facts["expansion_id"] != expansion.expansion_id
        or facts["expansion_identity"] != expansion.identity_fingerprint
        or facts["step"] != asdict(expansion.step)
        or facts["input_fingerprint"] != expansion.input_fingerprint
        or facts["member_fingerprint"] != expansion.member_fingerprint
        or facts["dag_id"] != expansion.dag_id
        or facts["dag_definition_fingerprint"] != expansion.dag_definition_fingerprint
        or facts["max_parallel"] != dag.max_parallel
        or facts["dag_state"] != "completed"
    ):
        raise WorkflowStateError("ADOPT frozen expansion source differs", kind="integrity")
    expected = CompletedDagAdoptionSource(
        CompletedDagSourceKind.WORKFLOW,
        projection.projection_id,
        run.parent_session_id,
        expansion.dag_id,
        facts["dag_generation"],
        expansion.dag_definition_fingerprint,
        ref,
        projection.source_fingerprint,
    )
    if record.plan.source != expected:
        raise WorkflowStateError("ADOPT frozen DAG source differs", kind="integrity")
    nodes = facts["nodes"]
    if len(nodes) != len(expansion.members) or len(record.plan.sources) != len(nodes):
        raise WorkflowStateError("ADOPT frozen member count differs", kind="integrity")
    # Plan sources follow DAG order, while the projection follows frozen members.
    sources = {source.node_id: source for source in record.plan.sources}
    if set(sources) != {member.node_id for member in expansion.members}:
        raise WorkflowStateError("ADOPT frozen node identities differ", kind="integrity")
    for member, fact in zip(expansion.members, nodes, strict=True):
        node, worker = fact["node"], fact["worker_linkage"]
        source = sources[member.node_id]
        if (
            fact["member"] != member.payload
            or node["node_id"] != member.node_id
            or node["state"] != "completed"
            or node["prompt"] != dag.node(member.node_id).prompt
            or node["dependencies"] != list(dag.node(member.node_id).dependencies)
            or node["kind"] != dag.node(member.node_id).kind.value
            or worker is None
            or worker["task"][0] != run.parent_session_id
            or worker["task"][1:] != ["completed", "subagent"]
            or source.parent_repository != record.plan.parent_repository
            or source.base_commit_sha != record.plan.parent_head_sha
            or {
                "parent_task_id": source.parent_task_id,
                "child_session_id": source.child_session_id,
                "lease_id": source.lease_id,
                "worktree_id": source.worktree_id.value,
                "baseline_checkpoint_id": source.baseline_checkpoint_id.value,
                "final_workspace_fingerprint": source.final_workspace_fingerprint,
            }
            != {
                key: node[key]
                for key in (
                    "parent_task_id",
                    "child_session_id",
                    "lease_id",
                    "worktree_id",
                    "baseline_checkpoint_id",
                    "final_workspace_fingerprint",
                )
            }
            or {
                "base_commit_sha": source.base_commit_sha,
                "capability_fingerprint": source.capability_fingerprint,
                "grant_fingerprint": source.grant_fingerprint,
            }
            != {
                key: worker[key]
                for key in (
                    "base_commit_sha",
                    "capability_fingerprint",
                    "grant_fingerprint",
                )
            }
        ):
            raise WorkflowStateError("ADOPT frozen worker source differs", kind="integrity")


def _verify_terminal_adopt(
    connection: sqlite3.Connection, attempt: WorkflowActivityAttempt
) -> None:
    """Semantic terminal proof check, including on normal recovery reads."""
    assert attempt.result is not None
    request = _request(connection, attempt)
    record = _load_result_adoption(connection, request.adoption_id)
    if (
        record is None
        or not record.state.terminal
        or record.plan.source.workflow != request.workflow_source
        or record.plan.parent_session_id != attempt.invocation.parent_session_id
    ):
        raise WorkflowStateError("ADOPT underlying terminal evidence missing", kind="integrity")
    _verify_durable_source(connection, attempt, record, request)
    amounts = attempt.result.usage
    if amounts != adoption_recovery_usage(record):
        raise WorkflowStateError("ADOPT has no trusted known execution usage", kind="integrity")
    proof = digest([adoption_terminal_digest(record), asdict(amounts)])
    state = adoption_activity_state(record.state)
    output = (
        canonical(
            {"status": "completed", "parent_workspace_changed": record.parent_workspace_changed}
        )
        if state is WorkflowActivityState.COMPLETED
        else None
    )
    if (
        attempt.result.source_id,
        attempt.result.source_fingerprint,
        attempt.result.state,
        attempt.result.output_json,
    ) != (record.adoption_id, proof, state, output):
        raise WorkflowStateError("ADOPT terminal evidence differs", kind="integrity")
