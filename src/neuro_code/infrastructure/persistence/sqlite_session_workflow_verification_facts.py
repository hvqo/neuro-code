"""VERIFY evidence integrity, independent frozen inputs, no filesystem execution."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from typing import Any

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.ports.workflow_verification import (
    ApprovedWorkflowVerification,
    VerificationWorkspaceEvidence,
)
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityResult
from neuro_code.domain.workflows.definition import Activity, ActivityKind, Map, ResultRef, TaskBatch
from neuro_code.domain.workflows.interpreter import (
    OutputKind,
    WorkflowStepOutput,
    expansion_id,
    validate_step_output,
)
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import StepIdentity, WorkflowEventKind, WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity_codec import (
    decode_attempt,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_facts import (
    _verify_terminal_adopt,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_projection import (
    _project_facts,
    _read_projection,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_publication import (
    _load_publication,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _load_definition,
    _load_run,
    _validate_position,
)


def fact(row: tuple[str, str] | None) -> dict[str, Any]:
    if row is None:
        raise WorkflowStateError("VERIFY evidence missing", kind="integrity")
    value = json.loads(row[0])
    if not isinstance(value, dict) or canonical(value) != row[0] or digest(value) != row[1]:
        raise WorkflowStateError("VERIFY evidence fingerprint differs", kind="integrity")
    return value


def source(connection: sqlite3.Connection, attempt: WorkflowActivityAttempt) -> dict[str, str]:
    invocation = attempt.invocation
    if invocation.activity is not ActivityKind.VERIFY:
        raise WorkflowStateError("exact VERIFY invocation required", kind="protocol")
    run = _load_run(connection, invocation.run_id)
    if run is None:
        raise WorkflowStateError("VERIFY run missing", kind="integrity")
    definition = _load_definition(connection, run.definition_fingerprint)
    if definition is None:
        raise WorkflowStateError("VERIFY definition missing", kind="integrity")
    declared = _validate_position(definition, invocation.step)
    if not isinstance(declared, Activity):
        raise WorkflowStateError("VERIFY declaration differs", kind="integrity")
    bindings: dict[str, str] = {}
    for name, ref in declared.inputs:
        if not isinstance(ref, ResultRef):
            # Non-result inputs are data, never command or authority.
            continue
        if ref.iteration is not None or ref.item_key is not None:
            raise WorkflowStateError("VERIFY source selector is ambiguous", kind="protocol")
        identity = StepIdentity(ref.step_id, invocation.step.iteration)
        if not any(s.identity == identity for s in run.steps):
            identity = StepIdentity(ref.step_id)
        row = connection.execute(
            "SELECT kind,source_id,source_fingerprint,input_fingerprint,output_json,output_fingerprint,generation FROM workflow_step_outputs WHERE run_id=? AND step_key=?",
            (run.run_id, identity.key),
        ).fetchone()
        linked = next((step for step in run.steps if step.identity == identity), None)
        if row is None or linked is None or linked.status is not WorkflowStatus.COMPLETED:
            raise WorkflowStateError("VERIFY needs consumed exact source", kind="integrity")
        value = row[4]
        if row[0] == OutputKind.PROJECTION.value:
            publication = _load_publication(connection, expansion_id(run.run_id, identity))
            if publication is None:
                raise WorkflowStateError("VERIFY projection publication missing", kind="integrity")
            source_json, output_json = _project_facts(connection, publication)
            projection = _read_projection(connection, publication, source_json, output_json)
            if (
                projection is None
                or projection.projection_id != row[1]
                or projection.fingerprint != row[2]
            ):
                raise WorkflowStateError("VERIFY projection source differs", kind="integrity")
            value = projection.output_json
        output = WorkflowStepOutput(
            run.run_id, identity, row[3], OutputKind(row[0]), row[1], row[2], value
        )
        producer = _validate_position(definition, identity)
        if not isinstance(producer, Activity | TaskBatch | Map):
            raise WorkflowStateError("VERIFY source cannot be a control node", kind="integrity")
        validate_step_output(producer, output)
        event = connection.execute(
            "SELECT kind,payload_json FROM workflow_transition_journal WHERE run_id=? AND generation=?",
            (run.run_id, row[6]),
        ).fetchone()
        if (
            output.fingerprint != row[5]
            or output.input_fingerprint != linked.input_fingerprint
            or row[6] > run.generation
            or event
            != (
                (
                    WorkflowEventKind.ACTIVITY
                    if output.kind is OutputKind.ACTIVITY
                    else WorkflowEventKind.STEP
                ).value,
                canonical(
                    {
                        "operation": "consume_typed_output",
                        "step_key": identity.key,
                        "output_fingerprint": output.fingerprint,
                    }
                ),
            )
        ):
            raise WorkflowStateError("VERIFY consumed source/journal differs", kind="integrity")
        if output.kind is OutputKind.ACTIVITY:
            result_row = connection.execute(
                "SELECT payload_json,payload_fingerprint FROM workflow_activity_results WHERE invocation_id=?",
                (output.source_id,),
            ).fetchone()
            result = fact(result_row)
            if (
                result_row is None
                or result_row[1] != output.source_fingerprint
                or result["output_json"] != output.output_json
                or result["state"] != "completed"
            ):
                raise WorkflowStateError("VERIFY Activity source differs", kind="integrity")
            snapshot = fact(
                connection.execute(
                    "SELECT snapshot_json,snapshot_fingerprint FROM workflow_activity_attempts WHERE invocation_id=?",
                    (output.source_id,),
                ).fetchone()
            )
            previous = decode_attempt(snapshot)
            if (
                previous.result is None
                or previous.result.fingerprint != output.source_fingerprint
                or previous.invocation.run_id != run.run_id
                or previous.invocation.parent_session_id != run.parent_session_id
                or previous.invocation.definition_fingerprint != run.definition_fingerprint
                or previous.invocation.step != identity
            ):
                raise WorkflowStateError(
                    "VERIFY previous Activity binding differs", kind="integrity"
                )
            if previous.invocation.activity is ActivityKind.ADOPT:
                _verify_terminal_adopt(connection, previous)
            elif previous.invocation.activity is ActivityKind.VERIFY:
                verify_terminal(connection, previous)
            else:
                raise WorkflowStateError(
                    "VERIFY needs a real supported source Activity", kind="protocol"
                )
        selected: Any = json.loads(output.output_json)
        for field in ref.field_path:
            if not isinstance(selected, dict) or field not in selected:
                raise WorkflowStateError("VERIFY source field differs", kind="integrity")
            selected = selected[field]
        if selected != json.loads(invocation.request_json)[name]:
            raise WorkflowStateError("VERIFY resolved source input differs", kind="integrity")
        bindings[name] = output.fingerprint
    return bindings


def parent_root(connection: sqlite3.Connection, attempt: WorkflowActivityAttempt) -> str:
    row = connection.execute(
        "SELECT cwd FROM sessions WHERE id=?", (attempt.invocation.parent_session_id,)
    ).fetchone()
    if row is None:
        raise WorkflowStateError("VERIFY parent missing", kind="integrity")
    return str(row[0])


def execution(connection: sqlite3.Connection, attempt: WorkflowActivityAttempt) -> dict[str, Any]:
    value = fact(
        connection.execute(
            "SELECT payload_json,payload_fingerprint FROM workflow_verification_executions WHERE invocation_id=?",
            (attempt.invocation.invocation_id,),
        ).fetchone()
    )
    assert attempt.reserved is not None
    config = value["configuration"]
    if config is not None:
        ApprovedWorkflowVerification(**config)
    before = value["workspace_before"]
    if before is not None:
        VerificationWorkspaceEvidence(**before)
    started_at = datetime.fromisoformat(value["started_at"])
    if started_at.tzinfo is None:
        raise WorkflowStateError("VERIFY execution time is invalid", kind="integrity")
    if (
        value["invocation_fingerprint"] != attempt.invocation.fingerprint
        or value["owner_id"] != attempt.owner_id
        or value["owner_fence"] != attempt.owner_fence
        or value["reservation_id"] != attempt.reservation_id
        or value["reserved"] != asdict(attempt.reserved)
        or value["source_bindings"] != source(connection, attempt)
        or value["parent_session_id"] != attempt.invocation.parent_session_id
        or value["parent_workspace_root"] != parent_root(connection, attempt)
        or (before is not None and before["root"] != value["parent_workspace_root"])
        or type(value["workspace_generation"]) is not int
        or value["workspace_generation"] < 0
    ):
        raise WorkflowStateError("VERIFY frozen execution binding differs", kind="integrity")
    row = connection.execute(
        "SELECT invocation_id FROM workflow_verification_executions WHERE execution_id=?",
        (value["execution_id"],),
    ).fetchone()
    if row != (attempt.invocation.invocation_id,):
        raise WorkflowStateError("VERIFY indexed execution differs", kind="integrity")
    return value


def terminal_anchor(
    attempt: WorkflowActivityAttempt,
    started: dict[str, Any],
    evidence: dict[str, Any],
    result: WorkflowActivityResult,
) -> dict[str, Any]:
    """Pin the trusted observation outside the local result/snapshot group."""
    return {
        "execution_id": started["execution_id"],
        "invocation_id": attempt.invocation.invocation_id,
        "request_fingerprint": attempt.invocation.request_fingerprint,
        "configuration_fingerprint": digest(started["configuration"]),
        "owner_id": attempt.owner_id,
        "owner_fence": attempt.owner_fence,
        "execution_fingerprint": digest(started),
        "observation": evidence["observation"],
        "exit_code": evidence["exit_code"],
        "outcome": evidence["outcome"],
        "workspace_before": started["workspace_before"],
        "workspace_after": evidence["workspace_after"],
        "evidence_fingerprint": digest(evidence),
        "result_fingerprint": result.fingerprint,
    }


def verify_terminal(connection: sqlite3.Connection, attempt: WorkflowActivityAttempt) -> None:
    result = attempt.result
    assert result is not None
    started = execution(connection, attempt)
    value = fact(
        connection.execute(
            "SELECT payload_json,payload_fingerprint FROM workflow_verification_evidence WHERE execution_id=?",
            (started["execution_id"],),
        ).fetchone()
    )
    if (
        value["execution_fingerprint"] != digest(started)
        or value["finished_at"] != result.terminal_at.isoformat()
        or result.terminal_at < datetime.fromisoformat(started["started_at"])
        or value["state"] != result.state.value
        or value["usage"] != asdict(result.usage)
        or value["output_json"] != result.output_json
        or result.source_id != started["execution_id"]
        or result.source_fingerprint != digest(value)
    ):
        raise WorkflowStateError("VERIFY terminal proof differs", kind="integrity")
    if result.usage.tool_calls not in {0, 1, None}:
        raise WorkflowStateError(
            "VERIFY has at most one foreground tool invocation", kind="integrity"
        )
    code = value["exit_code"]
    if code is not None and type(code) is not int:
        raise WorkflowStateError("VERIFY exit code is invalid", kind="integrity")
    summary = value["summary"]
    if (
        summary is not None
        and (not isinstance(summary, str) or len(summary.encode("utf-8")) > 4096)
    ) or value["output_fingerprint"] != digest(summary):
        raise WorkflowStateError("VERIFY output summary differs", kind="integrity")
    if value["outcome"] not in {
        "PASS",
        "FAIL",
        "blocked",
        "uncertain",
        "executor_error",
        "unknown",
    }:
        raise WorkflowStateError("VERIFY outcome differs", kind="integrity")
    if result.output_json is not None:
        output = json.loads(result.output_json)
        if (
            started["configuration"] is None
            or started["workspace_before"] is None
            or value["outcome"] != output["status"]
            or output["status"] not in {"PASS", "FAIL"}
            or output["workspace_generation"] != started["workspace_generation"]
            or value["workspace_after"] != started["workspace_before"]
            or code != (0 if output["status"] == "PASS" else 1)
            or value["usage"]["tool_calls"] != 1
            or not isinstance(value["observation"], dict)
            or value["observation"].get("tool_name") != "bash"
            or value["observation"].get("not_started") is not False
            or value["observation"].get("cancelled") is not False
            or value["observation"].get("is_error") is not (code != 0)
        ):
            raise WorkflowStateError(
                "VERIFY output is not exact fresh execution evidence", kind="integrity"
            )
    anchor = terminal_anchor(attempt, started, value, result)
    journal = connection.execute(
        "SELECT kind,payload_json,payload_fingerprint,created_at FROM workflow_transition_journal WHERE run_id=? AND request_id=?",
        (attempt.invocation.run_id, "activity-settle:" + attempt.invocation.invocation_id),
    ).fetchone()
    expected = canonical(
        {
            "operation": "activity_budget",
            "reservation_id": attempt.reservation_id,
            "amounts": asdict(result.usage),
            "verification": anchor,
        }
    )
    if journal != (
        WorkflowEventKind.CONSUMED.value,
        expected,
        digest(json.loads(expected)),
        result.terminal_at.isoformat(),
    ):
        raise WorkflowStateError(
            "VERIFY independent terminal journal proof differs", kind="integrity"
        )
    if value["outcome"] == "unknown" and (
        result.usage.tool_calls != (None if attempt.revision == 3 else 0)
        or result.usage.wall_milliseconds is not None
    ):
        raise WorkflowStateError("VERIFY recovery cannot invent known usage", kind="integrity")
    if any(
        value["usage"][key] != 0
        for key in ("generated_tasks", "model_calls", "input_tokens", "output_tokens")
    ):
        raise WorkflowStateError("VERIFY cannot generate tasks or call models", kind="integrity")
