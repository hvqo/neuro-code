"""Exact ADOPT source resolution and terminal-only reconciliation in schema 40."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from neuro_code.application.ports.workflow_adoption import (
    adoption_activity_state,
    adoption_recovery_usage,
    adoption_terminal_digest,
)
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.result_adoption import ResultAdoptionRequest
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
    identifier,
    integer,
    timestamp,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
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
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _append_event,
    _guard,
    _save_run,
)
from neuro_code.shared.async_utils import run_blocking


class WorkflowAdoptionMixin(_SqliteSessionPersistenceContext):
    async def get_workflow_adoption_request(self, invocation_id: str) -> ResultAdoptionRequest:
        identifier(invocation_id)

        def read() -> ResultAdoptionRequest:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return _request(connection, _load_attempt(connection, invocation_id))

        return await run_blocking(lambda: _guard(read))

    async def reconcile_workflow_adoption(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: str,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt:
        identifier(invocation_id)
        identifier(parent_session_id)
        timestamp(updated_at)

        def reconcile() -> WorkflowActivityAttempt:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
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
                if (
                    not current.state.terminal
                    and current.state is not WorkflowActivityState.RUNNING
                ):
                    raise WorkflowStateError(
                        "ADOPT terminal fact requires RUNNING", kind="protocol"
                    )
                if current.result is not None:
                    amounts = current.result.usage
                else:
                    # Target revisions prove neither exact port invocation count
                    # nor wall usage. Do not manufacture precision from them.
                    amounts = adoption_recovery_usage(record)
                if (
                    amounts.generated_tasks,
                    amounts.model_calls,
                    amounts.input_tokens,
                    amounts.output_tokens,
                ) != (0, 0, 0, 0):
                    raise WorkflowStateError(
                        "ADOPT cannot consume models or generate tasks", kind="protocol"
                    )
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
                return _settle_terminal(connection, current, result)

        async with self._write_lock:
            return await run_blocking(lambda: _guard(reconcile))

    async def mark_workflow_adoption_attention(
        self,
        invocation_id: str,
        *,
        expected_revision: int,
        updated_at: datetime,
    ) -> None:
        identifier(invocation_id)
        integer(expected_revision)
        timestamp(updated_at)

        def mark() -> None:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_attempt(connection, invocation_id)
                _request(connection, current)
                assert current is not None
                if (
                    current.state is not WorkflowActivityState.RUNNING
                    or current.revision != expected_revision
                ):
                    raise WorkflowStateError(
                        "ADOPT recovery revision changed", kind="concurrent_modification"
                    )
                run = _run(connection, current.invocation.run_id)
                if run.status.terminal or run.status is WorkflowStatus.NEEDS_ATTENTION:
                    return
                changed = replace(
                    run,
                    generation=run.generation + 1,
                    status=WorkflowStatus.NEEDS_ATTENTION,
                    waiting_reason=None,
                    failure=WorkflowFailure(
                        "adopt_recovery_uncertain", "ADOPT needs exact underlying recovery evidence"
                    ),
                    updated_at=max(updated_at, run.updated_at),
                )
                _save_run(connection, changed, expected=run)
                _append_event(
                    connection,
                    changed,
                    "adopt-attention:" + invocation_id,
                    WorkflowEventKind.ACTIVITY,
                    canonical(
                        {"operation": "adopt_recovery_uncertain", "invocation_id": invocation_id}
                    ),
                )

        async with self._write_lock:
            await run_blocking(lambda: _guard(mark))
