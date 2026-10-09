"""Read-only integrity checks for immutable ADOPT measurement facts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any

from neuro_code.application.ports.result_adoption import (
    ResultAdoptionRecord,
    WorkspaceMutationRequest,
)
from neuro_code.application.ports.workflow_adoption import (
    adoption_recovery_usage,
    adoption_terminal_digest,
)
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.result_adoption import ResultAdoptionTargetState, workspace_entry_fingerprint
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import BudgetAmounts


def _mutation_fingerprint(request: WorkspaceMutationRequest) -> str:
    return digest(
        [
            request.path,
            request.operation.value,
            workspace_entry_fingerprint(request.expected),
            workspace_entry_fingerprint(request.desired),
        ]
    )


def _fact(row: tuple[Any, ...]) -> dict[str, Any]:
    payload, fingerprint = row
    value = json.loads(payload)
    if not isinstance(value, dict) or canonical(value) != payload or digest(value) != fingerprint:
        raise WorkflowStateError("ADOPT measurement integrity differs", kind="integrity")
    return value


def measured_adoption_usage(
    connection: sqlite3.Connection, attempt: WorkflowActivityAttempt, record: ResultAdoptionRecord
) -> BudgetAmounts:
    """Validate durable measurement against exact immutable execution/source facts."""
    row = connection.execute(
        "SELECT execution_id,payload_json,payload_fingerprint FROM workflow_adoption_executions WHERE invocation_id=?",
        (attempt.invocation.invocation_id,),
    ).fetchone()
    if row is None:
        return adoption_recovery_usage(record)
    execution_id = row[0]
    start = _fact(row[1:])
    if start != {
        "execution_id": execution_id,
        "invocation_id": attempt.invocation.invocation_id,
        "adoption_id": record.adoption_id,
        "request_fingerprint": attempt.invocation.request_fingerprint,
        "owner_id": attempt.owner_id,
        "owner_fence": attempt.owner_fence,
        "revision": 2,
        "reservation_id": attempt.reservation_id,
    }:
        raise WorkflowStateError("ADOPT execution binding differs", kind="integrity")
    completion = connection.execute(
        "SELECT payload_json,payload_fingerprint FROM workflow_adoption_measurements WHERE execution_id=?",
        (execution_id,),
    ).fetchone()
    if completion is None:
        return adoption_recovery_usage(record)
    end = _fact(completion)
    rows = connection.execute(
        "SELECT ordinal,phase,payload_json,payload_fingerprint FROM workflow_adoption_dispatch_events WHERE execution_id=? ORDER BY ordinal,phase",
        (execution_id,),
    ).fetchall()
    events = [(r[0], r[1], _fact(r[2:])) for r in rows]
    intents = {i: v for i, phase, v in events if phase == "intent"}
    acks = {i: v for i, phase, v in events if phase in {"returned", "raised"}}
    count = len(intents)
    if (
        len(events) != 2 * count
        or set(intents) != set(range(1, count + 1))
        or set(acks) != set(intents)
        or any(acks[i] != intents[i] for i in intents)
        or end.keys()
        != {
            "execution_fingerprint",
            "events_fingerprint",
            "terminal_fingerprint",
            "elapsed_ns",
            "usage",
        }
        or end["execution_fingerprint"] != digest(start)
        or end["events_fingerprint"] != digest(events)
        or end["terminal_fingerprint"] != adoption_terminal_digest(record)
        or type(end["elapsed_ns"]) is not int
        or end["elapsed_ns"] < 0
    ):
        raise WorkflowStateError("ADOPT measurement continuity differs", kind="integrity")
    for value in intents.values():
        target = next((t for t in record.plan.targets if t.path == value.get("path")), None)
        if (
            target is None
            or value.keys() != {"path", "target_revision", "request_fingerprint"}
            or type(value["target_revision"]) is not int
            or value["target_revision"] < 1
            or value["request_fingerprint"]
            != _mutation_fingerprint(
                WorkspaceMutationRequest(
                    target.path, target.operation, target.baseline, target.desired
                )
            )
        ):
            raise WorkflowStateError("ADOPT measured mutation differs from plan", kind="integrity")
    # Revisions are only an omission guard, NEVER the source of usage. Every
    # crossing into APPLYING must have a matching measured call acknowledgement.
    observed = {(v["path"], v["target_revision"]) for v in intents.values()}
    expected_boundaries = {
        (t.target.path, revision)
        for t in record.targets
        for revision in range(1, t.version + (t.state is ResultAdoptionTargetState.APPLYING), 2)
    }
    if len(observed) != count or observed != expected_boundaries:
        raise WorkflowStateError("ADOPT unmeasured execution window", kind="integrity")
    amounts = BudgetAmounts(
        tool_calls=count, wall_milliseconds=(end["elapsed_ns"] + 999_999) // 1_000_000
    )
    if end["usage"] != asdict(amounts):
        raise WorkflowStateError("ADOPT trusted known usage differs", kind="integrity")
    return amounts
