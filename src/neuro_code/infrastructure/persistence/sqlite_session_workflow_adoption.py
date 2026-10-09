"""Exact ADOPT source resolution and terminal-only reconciliation with schema-41 measurement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import closing, nullcontext
from dataclasses import replace
from datetime import datetime

from neuro_code.application.ports.result_adoption import WorkspaceMutationPort
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.result_adoption import ResultAdoptionRequest
from neuro_code.domain.workflows.activity import (
    WorkflowActivityAttempt,
    WorkflowActivityState,
)
from neuro_code.domain.workflows.publication import canonical
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
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity import (
    _load_attempt,
    _run,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_facts import (
    _request,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_lock import (
    adoption_execution_lock,
    owns_execution_lock,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_meter import (
    execute_adoption,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_settlement import (
    _reconcile_adoption,
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
            # Immutable terminal replay needs no execution-resource liveness.
            # A read transaction validates the exact proof without settling again.
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                current = _load_attempt(connection, invocation_id)
                if current is not None and current.state.terminal:
                    return _reconcile_adoption(
                        connection,
                        invocation_id,
                        parent_session_id=parent_session_id,
                        parent_workspace_root=parent_workspace_root,
                        updated_at=updated_at,
                    )
            with (
                adoption_execution_lock(self._database_path, invocation_id),
                closing(self._connect()) as connection,
                connection,
            ):
                connection.execute("BEGIN IMMEDIATE")
                # Re-read all facts under both OS arbitration and SQLite CAS.
                return _reconcile_adoption(
                    connection,
                    invocation_id,
                    parent_session_id=parent_session_id,
                    parent_workspace_root=parent_workspace_root,
                    updated_at=updated_at,
                )

        async with self._write_lock:
            return await run_blocking(lambda: _guard(reconcile))

    async def execute_workflow_adoption(
        self,
        invocation_id: str,
        *,
        expected_revision: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
        mutation: WorkspaceMutationPort,
        dispatch: Callable[[WorkflowActivityAttempt, WorkspaceMutationPort], Awaitable[None]],
    ) -> WorkflowActivityAttempt:
        identifier(invocation_id)
        integer(expected_revision)
        identifier(owner_id)
        integer(owner_fence)
        timestamp(updated_at)
        return await execute_adoption(
            self,
            invocation_id,
            expected_revision=expected_revision,
            owner_id=owner_id,
            owner_fence=owner_fence,
            updated_at=updated_at,
            mutation=mutation,
            dispatch=dispatch,
        )

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
                        "adopt_recovery_uncertain",
                        "ADOPT needs exact underlying recovery evidence",
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
                        {
                            "operation": "adopt_recovery_uncertain",
                            "invocation_id": invocation_id,
                        }
                    ),
                )

        guard = (
            nullcontext()
            if owns_execution_lock(self._database_path, invocation_id)
            else adoption_execution_lock(self._database_path, invocation_id)
        )
        with guard:
            async with self._write_lock:
                await run_blocking(lambda: _guard(mark))
