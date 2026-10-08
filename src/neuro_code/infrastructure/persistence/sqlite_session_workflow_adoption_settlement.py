"""Atomic ADOPT settlement; no public caller-supplied known usage."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from neuro_code.application.ports.workflow_adoption import (
    adoption_activity_state,
    adoption_recovery_usage,
    adoption_terminal_digest,
)
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import (
    WorkflowActivityAttempt,
    WorkflowActivityResult,
    WorkflowActivityState,
)
from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    WorkflowEventKind,
    WorkflowFailure,
    WorkflowStatus,
)
from neuro_code.infrastructure.persistence.sqlite_session_result_adoption import (
    _load_result_adoption,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity import (
    _load_attempt,
    _run,
    _settle_terminal,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_facts import (
    _request,
    _verify_durable_source,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_measurements import (
    measured_adoption_usage,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _append_event,
    _save_run,
)

if TYPE_CHECKING:
    from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_meter import (
        _ExecutionMeter,
    )


def _reconcile_adoption(
    connection: sqlite3.Connection,
    invocation_id: str,
    *,
    parent_session_id: str,
    parent_workspace_root: str,
    updated_at: datetime,
    live_execution: _ExecutionMeter | None = None,
) -> WorkflowActivityAttempt:
    current = _load_attempt(connection, invocation_id)
    request = _request(connection, current)
    assert current is not None
    record = _load_result_adoption(connection, request.adoption_id)
    if record is None or not record.state.terminal:
        raise WorkflowStateError(
            "ADOPT reconciliation requires a terminal fact", kind="needs_attention"
        )
    if (
        record.plan.source.workflow != request.workflow_source
        or record.plan.parent_session_id != parent_session_id
        or current.invocation.parent_session_id != parent_session_id
        or record.plan.parent_workspace_root != Path(parent_workspace_root)
    ):
        raise WorkflowStateError("ADOPT terminal binding differs", kind="integrity")
    _verify_durable_source(connection, current, record, request)
    if not current.state.terminal and current.state is not WorkflowActivityState.RUNNING:
        raise WorkflowStateError("ADOPT terminal fact requires RUNNING", kind="protocol")
    if current.result is not None:
        amounts = current.result.usage
    else:
        # Only a completed controlled execution supplies known usage.
        # An intent, revision or desired image cannot manufacture it.
        receipt = connection.execute(
            "SELECT execution_id FROM workflow_adoption_measurements WHERE execution_id IN (SELECT execution_id FROM workflow_adoption_executions WHERE invocation_id=?)",
            (invocation_id,),
        ).fetchone()
        if live_execution is None:
            if receipt is not None:
                raise WorkflowStateError(
                    "orphan ADOPT measurement is not settlement authority", kind="integrity"
                )
            amounts = adoption_recovery_usage(record)
        else:
            live_execution._assert_live()
            if live_execution.attempt.invocation.invocation_id != invocation_id or receipt != (
                live_execution.execution_id,
            ):
                raise WorkflowStateError("ADOPT measurement identity differs", kind="integrity")
            amounts = measured_adoption_usage(connection, current, record)
    if (
        amounts.generated_tasks,
        amounts.model_calls,
        amounts.input_tokens,
        amounts.output_tokens,
    ) != (0, 0, 0, 0):
        raise WorkflowStateError("ADOPT cannot consume models or generate tasks", kind="protocol")
    proof = digest([adoption_terminal_digest(record), asdict(amounts)])
    state = adoption_activity_state(record.state)
    output = (
        canonical(
            {
                "status": "completed",
                "parent_workspace_changed": record.parent_workspace_changed,
            }
        )
        if state is WorkflowActivityState.COMPLETED
        else None
    )
    if current.result is not None:
        if (
            current.result.source_id,
            current.result.source_fingerprint,
            current.result.state,
            current.result.output_json,
        ) != (record.adoption_id, proof, state, output):
            raise WorkflowStateError("ADOPT terminal proof differs", kind="integrity")
        return current
    run = _run(connection, current.invocation.run_id)
    result = WorkflowActivityResult(
        invocation_id,
        current.invocation.request_fingerprint,
        ActivityKind.ADOPT,
        state,
        record.adoption_id,
        proof,
        amounts,
        max(
            updated_at,
            run.updated_at,
            current.updated_at or current.invocation.created_at,
            record.updated_at,
        ),
        output,
    )
    terminal = _settle_terminal(connection, current, result)
    assert current.reserved is not None
    overrun = any(
        used is not None and ceiling is not None and used > ceiling
        for used, ceiling in (
            (amounts.tool_calls, current.reserved.tool_calls),
            (amounts.wall_milliseconds, current.reserved.wall_milliseconds),
        )
    )
    settled_run = _run(connection, current.invocation.run_id)
    if (
        overrun
        and not settled_run.status.terminal
        and settled_run.status is not WorkflowStatus.NEEDS_ATTENTION
    ):
        changed = replace(
            settled_run,
            generation=settled_run.generation + 1,
            status=WorkflowStatus.NEEDS_ATTENTION,
            waiting_reason=None,
            failure=WorkflowFailure(
                "adopt_budget_exceeded",
                "ADOPT measured usage exceeded its dispatch reservation",
            ),
        )
        _save_run(connection, changed, expected=settled_run)
        _append_event(
            connection,
            changed,
            "adopt-accounting-overrun:" + invocation_id,
            WorkflowEventKind.ACTIVITY,
            canonical({"operation": "adopt_budget_exceeded", "invocation_id": invocation_id}),
        )
    return terminal
