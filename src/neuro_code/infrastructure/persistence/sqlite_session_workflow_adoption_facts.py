"""Read-only exact ADOPT source and terminal proof; no Activity write dependency."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from neuro_code.application.ports.workflow_adoption import (
    adoption_activity_state,
    adoption_terminal_digest,
    workflow_adoption_id,
)
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.completed_dag_adoption import WorkflowAdoptionSourceRef
from neuro_code.domain.result_adoption import ResultAdoptionRequest
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityState
from neuro_code.domain.workflows.definition import Activity, ActivityKind, Map, ResultRef, TaskBatch
from neuro_code.domain.workflows.interpreter import (
    OutputKind,
    WorkflowStepOutput,
    expansion_id,
    validate_step_output,
)
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import StepIdentity, WorkflowEventKind, WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session_result_adoption import (
    _load_result_adoption,
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
    amounts = attempt.result.usage
    if (
        amounts.generated_tasks,
        amounts.model_calls,
        amounts.input_tokens,
        amounts.output_tokens,
    ) != (0, 0, 0, 0):
        raise WorkflowStateError("ADOPT execution usage differs", kind="integrity")
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
