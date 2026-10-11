"""Frozen FAIL provenance and independent terminal journal integrity."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt
from neuro_code.domain.workflows.definition import Activity, ActivityKind, ResultRef
from neuro_code.domain.workflows.interpreter import (
    OutputKind,
    WorkflowStepOutput,
    validate_step_output,
)
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import StepIdentity, WorkflowEventKind, WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity_codec import (
    decode_attempt,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_verification_facts import (
    execution as verify_execution,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_verification_facts import (
    fact,
    parent_root,
    verify_terminal,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _load_definition,
    _load_run,
    _validate_position,
)


def fail_source(connection: sqlite3.Connection, attempt: WorkflowActivityAttempt) -> dict[str, Any]:
    invocation = attempt.invocation
    run = _load_run(connection, invocation.run_id)
    if run is None or run.parent_session_id != invocation.parent_session_id:
        raise WorkflowStateError("REPAIR run/session differs", kind="integrity")
    definition = _load_definition(connection, run.definition_fingerprint)
    if definition is None:
        raise WorkflowStateError("REPAIR definition missing", kind="integrity")
    declared = _validate_position(definition, invocation.step)
    if not isinstance(declared, Activity) or declared.activity is not ActivityKind.REPAIR:
        raise WorkflowStateError("exact REPAIR declaration required", kind="protocol")
    sources: list[dict[str, Any]] = []
    for name, ref in declared.inputs:
        if not isinstance(ref, ResultRef):
            continue
        if ref.iteration is not None or ref.item_key is not None:
            raise WorkflowStateError("REPAIR FAIL selector is ambiguous", kind="protocol")
        identity = StepIdentity(ref.step_id, invocation.step.iteration)
        if not any(s.identity == identity for s in run.steps):
            identity = StepIdentity(ref.step_id)
        # Only consumed VERIFY Activities are accepted; keep the evidence reader
        # below Activity/Interpreter to avoid circular persistence ownership.
        row = connection.execute(
            "SELECT kind,source_id,source_fingerprint,input_fingerprint,output_json,output_fingerprint,generation FROM workflow_step_outputs WHERE run_id=? AND step_key=?",
            (run.run_id, identity.key),
        ).fetchone()
        linked = next((step for step in run.steps if step.identity == identity), None)
        if row is None or linked is None or linked.status is not WorkflowStatus.COMPLETED:
            raise WorkflowStateError("REPAIR needs legally consumed VERIFY", kind="integrity")
        output = WorkflowStepOutput(
            run.run_id, identity, row[3], OutputKind(row[0]), row[1], row[2], row[4]
        )
        producer = _validate_position(definition, identity)
        if (
            output is None
            or not isinstance(producer, Activity)
            or producer.activity is not ActivityKind.VERIFY
            or output.kind is not OutputKind.ACTIVITY
        ):
            raise WorkflowStateError("REPAIR needs consumed real VERIFY", kind="protocol")
        validate_step_output(producer, output)
        event = connection.execute(
            "SELECT kind,payload_json,payload_fingerprint FROM workflow_transition_journal WHERE run_id=? AND generation=?",
            (run.run_id, row[6]),
        ).fetchone()
        consumed = {
            "operation": "consume_typed_output",
            "step_key": identity.key,
            "output_fingerprint": output.fingerprint,
        }
        if (
            output.fingerprint != row[5]
            or output.input_fingerprint != linked.input_fingerprint
            or row[6] > run.generation
            or event != (WorkflowEventKind.ACTIVITY.value, canonical(consumed), digest(consumed))
        ):
            raise WorkflowStateError("REPAIR consumed FAIL journal differs", kind="integrity")
        previous = decode_attempt(
            fact(
                connection.execute(
                    "SELECT snapshot_json,snapshot_fingerprint FROM workflow_activity_attempts WHERE invocation_id=?",
                    (output.source_id,),
                ).fetchone()
            )
        )
        terminal = fact(
            connection.execute(
                "SELECT payload_json,payload_fingerprint FROM workflow_activity_results WHERE invocation_id=?",
                (output.source_id,),
            ).fetchone()
        )
        if (
            previous.result is None
            or previous.result.fingerprint != output.source_fingerprint
            or digest(terminal) != output.source_fingerprint
            or previous.result.output_json != output.output_json
            or previous.invocation.invocation_id != output.source_id
            or previous.invocation.run_id != run.run_id
            or previous.invocation.parent_session_id != run.parent_session_id
            or previous.invocation.definition_fingerprint != run.definition_fingerprint
            or previous.invocation.step != identity
            or previous.invocation.activity is not ActivityKind.VERIFY
        ):
            raise WorkflowStateError("REPAIR exact VERIFY binding differs", kind="integrity")
        verify_terminal(connection, previous)
        value = json.loads(output.output_json)
        if value["status"] != "FAIL":
            raise WorkflowStateError("REPAIR requires exact FAIL, never PASS", kind="protocol")
        selected: Any = value
        for field in ref.field_path:
            if not isinstance(selected, dict) or field not in selected:
                raise WorkflowStateError("REPAIR FAIL field differs", kind="integrity")
            selected = selected[field]
        if selected != json.loads(invocation.request_json)[name]:
            raise WorkflowStateError("REPAIR resolved FAIL input differs", kind="integrity")
        started = verify_execution(connection, previous)
        evidence = fact(
            connection.execute(
                "SELECT payload_json,payload_fingerprint FROM workflow_verification_evidence WHERE execution_id=?",
                (started["execution_id"],),
            ).fetchone()
        )
        sources.append(
            {
                "run_id": run.run_id,
                "parent_session_id": run.parent_session_id,
                "step_key": identity.key,
                "verify_invocation_id": output.source_id,
                "verify_result_fingerprint": output.source_fingerprint,
                "consumed_output_fingerprint": output.fingerprint,
                "workspace": started["workspace_before"],
                "command": started["configuration"]["command"],
                "summary": evidence["summary"],
            }
        )
    if not sources or any(s != sources[0] for s in sources):
        raise WorkflowStateError("REPAIR needs one exact consumed FAIL", kind="protocol")
    return sources[0]


def execution(connection: sqlite3.Connection, attempt: WorkflowActivityAttempt) -> dict[str, Any]:
    value = fact(
        connection.execute(
            "SELECT payload_json,payload_fingerprint FROM workflow_repair_executions WHERE invocation_id=?",
            (attempt.invocation.invocation_id,),
        ).fetchone()
    )
    assert attempt.reserved is not None
    if (
        value["invocation_fingerprint"] != attempt.invocation.fingerprint
        or value["session_id"] != attempt.invocation.parent_session_id
        or value["root"] != parent_root(connection, attempt)
        or value["owner_id"] != attempt.owner_id
        or value["owner_fence"] != attempt.owner_fence
        or value["reservation_id"] != attempt.reservation_id
        or value["reserved"] != asdict(attempt.reserved)
        or value["fail_source"] != fail_source(connection, attempt)
    ):
        raise WorkflowStateError("REPAIR frozen execution differs", kind="integrity")
    return value


def events(connection: sqlite3.Connection, key: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT ordinal,phase,payload_json,payload_fingerprint FROM workflow_repair_events WHERE execution_id=? ORDER BY ordinal,phase",
        (key,),
    ).fetchall()
    values = []
    for ordinal, phase, raw, fingerprint in rows:
        value = fact((raw, fingerprint))
        if value["ordinal"] != ordinal or value["phase"] != phase:
            raise WorkflowStateError("REPAIR indexed observation differs", kind="integrity")
        values.append(value)
    intents = [v for v in values if v["phase"] == "intent"]
    if [v["ordinal"] for v in intents] != list(range(1, len(intents) + 1)):
        raise WorkflowStateError("REPAIR observation continuity differs", kind="integrity")
    for value in values:
        if value["phase"] == "returned" and (
            not 1 <= value["ordinal"] <= len(intents)
            or value["intent_fingerprint"] != digest(intents[value["ordinal"] - 1])
            or value["kind"] != intents[value["ordinal"] - 1]["kind"]
        ):
            raise WorkflowStateError("REPAIR ACK binding differs", kind="integrity")
    return values


def terminal_anchor(
    started: dict[str, Any], evidence: dict[str, Any], result_fingerprint: str
) -> dict[str, Any]:
    return {
        "execution_id": started["execution_id"],
        "turn_id": started["turn_id"],
        "execution_fingerprint": digest(started),
        "evidence_fingerprint": digest(evidence),
        "result_fingerprint": result_fingerprint,
        "observations_fingerprint": evidence["observations_fingerprint"],
        "fail_source_fingerprint": digest(started["fail_source"]),
    }


def repair_terminal(connection: sqlite3.Connection, attempt: WorkflowActivityAttempt) -> None:
    result = attempt.result
    assert result is not None
    started = execution(connection, attempt)
    evidence = fact(
        connection.execute(
            "SELECT payload_json,payload_fingerprint FROM workflow_repair_evidence WHERE execution_id=?",
            (started["execution_id"],),
        ).fetchone()
    )
    if (
        evidence["execution_fingerprint"] != digest(started)
        or evidence["state"] != result.state.value
        or evidence["usage"] != asdict(result.usage)
        or evidence["output_json"] != result.output_json
        or evidence["finished_at"] != result.terminal_at.isoformat()
        or evidence["observations_fingerprint"]
        != digest(events(connection, started["execution_id"]))
        or result.source_id != started["execution_id"]
        or result.source_fingerprint != digest(evidence)
        or result.usage.generated_tasks != 0
    ):
        raise WorkflowStateError("REPAIR terminal evidence differs", kind="integrity")
    expected = canonical(
        {
            "operation": "activity_budget",
            "reservation_id": attempt.reservation_id,
            "amounts": asdict(result.usage),
            "repair": terminal_anchor(started, evidence, result.fingerprint),
        }
    )
    journal = connection.execute(
        "SELECT kind,payload_json,payload_fingerprint FROM workflow_transition_journal WHERE run_id=? AND request_id=?",
        (attempt.invocation.run_id, "activity-settle:" + result.invocation_id),
    ).fetchone()
    if journal != (WorkflowEventKind.CONSUMED.value, expected, digest(json.loads(expected))):
        raise WorkflowStateError("REPAIR independent journal differs", kind="integrity")
    if result.output_json is not None:
        value = json.loads(result.output_json)
        if (
            value["status"] != "COMPLETED"
            or evidence["runtime_status"] != "completed"
            or evidence["workspace_after"] is None
        ):
            raise WorkflowStateError("REPAIR output needs real completed turn", kind="integrity")
        row = connection.execute(
            "SELECT source,resolution,input_fingerprint FROM session_turn_attempts WHERE session_id=? AND turn_id=?",
            (started["session_id"], started["turn_id"]),
        ).fetchone()
        if row != ("workflow_repair", "committed", started["turn_input_fingerprint"]):
            raise WorkflowStateError("REPAIR durable turn terminal differs", kind="integrity")
