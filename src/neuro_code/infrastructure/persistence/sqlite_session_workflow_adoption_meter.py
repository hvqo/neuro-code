"""Repository-owned ADOPT execution measurement, never caller-supplied receipts.

SQLite intent and external mutation are not atomic. Only this live scope can
acknowledge calls it actually entered. An interrupted scope is not restartable.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from typing import Any

from neuro_code.application.ports.result_adoption import (
    ResultAdoptionError,
    WorkspaceMutationPort,
    WorkspaceMutationRequest,
    WorkspaceMutationResult,
)
from neuro_code.application.ports.workflow_adoption import (
    adoption_terminal_digest,
)
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.result_adoption import ResultAdoptionTargetState
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityState
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import BudgetAmounts, WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_result_adoption import (
    _load_result_adoption,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity import (
    _load_attempt,
    _run,
    _start_activity,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_facts import _request
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_lock import (
    adoption_execution_lock,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_measurements import (
    _fact,
    _mutation_fingerprint,
    measured_adoption_usage,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_settlement import (
    _reconcile_adoption,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import _guard
from neuro_code.shared.async_utils import run_blocking


class _ExecutionMeter:
    """A non-replayable scope wrapping the injected authority-bearing mutation port."""

    def __init__(
        self,
        context: _SqliteSessionPersistenceContext,
        attempt: WorkflowActivityAttempt,
        execution_id: str,
        inner: WorkspaceMutationPort,
        authorization: object,
    ) -> None:
        self.context, self.attempt, self.execution_id, self.inner = (
            context,
            attempt,
            execution_id,
            inner,
        )
        if context._workflow_adoption_meter_scopes.get(execution_id) is not authorization:
            raise WorkflowStateError(
                "ADOPT measurement scope was not issued by first dispatch", kind="protocol"
            )
        context._workflow_adoption_meter_scopes[execution_id] = self
        self._assert_live()
        self.started_ns = time.perf_counter_ns()
        self.calls = 0
        self.closed = False
        self.lock = asyncio.Lock()

    def _assert_live(self) -> None:
        if self.context._workflow_adoption_meter_scopes.get(self.execution_id) is not self:
            raise WorkflowStateError(
                "ADOPT measurement scope was not issued by first dispatch", kind="protocol"
            )

    async def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        self._assert_live()

        def write() -> Any:
            with closing(self.context._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                return operation(connection)

        async with self.context._write_lock:
            return await run_blocking(lambda: _guard(write))

    async def apply(
        self, request: WorkspaceMutationRequest, *, session_id: str
    ) -> WorkspaceMutationResult:
        async with self.lock:
            if self.closed:
                raise ResultAdoptionError(
                    "ADOPT measurement scope is closed", kind="budget_unknown"
                )
            assert self.attempt.reserved is not None
            if (
                self.calls >= (self.attempt.reserved.tool_calls or 0)
                or time.perf_counter_ns() - self.started_ns
                >= (self.attempt.reserved.wall_milliseconds or 0) * 1_000_000
            ):
                raise ResultAdoptionError(
                    "ADOPT measured ceiling exhausted", kind="budget_exceeded"
                )
            ordinal = self.calls + 1

            def intent(connection: sqlite3.Connection) -> dict[str, Any]:
                current = _load_attempt(connection, self.attempt.invocation.invocation_id)
                if current is None or (
                    current.state,
                    current.revision,
                    current.owner_id,
                    current.owner_fence,
                ) != (
                    WorkflowActivityState.RUNNING,
                    self.attempt.revision,
                    self.attempt.owner_id,
                    self.attempt.owner_fence,
                ):
                    raise WorkflowStateError(
                        "ADOPT measurement owner/fence is stale", kind="concurrent_modification"
                    )
                run = _run(connection, current.invocation.run_id)
                if (
                    run.status is not WorkflowStatus.WAITING
                    or run.waiting_reason != "activity:" + current.invocation.invocation_id
                ):
                    raise ResultAdoptionError("ADOPT no longer authorized", kind="cancelled")
                adoption_request = _request(connection, current)
                record = _load_result_adoption(connection, adoption_request.adoption_id)
                target = (
                    next((t for t in record.targets if t.target.path == request.path), None)
                    if record
                    else None
                )
                if (
                    record is None
                    or session_id != current.invocation.parent_session_id
                    or target is None
                    or target.state is not ResultAdoptionTargetState.APPLYING
                    or request
                    != WorkspaceMutationRequest(
                        target.target.path,
                        target.target.operation,
                        target.target.baseline,
                        target.target.desired,
                    )
                ):
                    raise WorkflowStateError("ADOPT measured dispatch differs", kind="integrity")
                value = {
                    "path": request.path,
                    "target_revision": target.version,
                    "request_fingerprint": _mutation_fingerprint(request),
                }
                connection.execute(
                    "INSERT INTO workflow_adoption_dispatch_events VALUES (?,?,?,?,?)",
                    (self.execution_id, ordinal, "intent", canonical(value), digest(value)),
                )
                return value

            value = await self._write(intent)
            # Intent alone does NOT prove port entry. The live scope owns this count.
            self.calls += 1
            try:
                result = await self.inner.apply(request, session_id=session_id)
            except Exception:
                await self._ack(ordinal, "raised", value)
                raise
            else:
                await self._ack(ordinal, "returned", value)
                return result
            # BaseException/cancellation/ACK loss leaves no complete measurement.

    async def _ack(self, ordinal: int, phase: str, value: dict[str, Any]) -> None:
        await self._write(
            lambda c: c.execute(
                "INSERT INTO workflow_adoption_dispatch_events VALUES (?,?,?,?,?)",
                (self.execution_id, ordinal, phase, canonical(value), digest(value)),
            )
        )

    async def complete(self) -> None:
        self._assert_live()
        if self.closed:
            raise WorkflowStateError("ADOPT scope is already sealed", kind="protocol")
        self.closed = True
        elapsed_ns = time.perf_counter_ns() - self.started_ns

        def finish(connection: sqlite3.Connection) -> None:
            current = _load_attempt(connection, self.attempt.invocation.invocation_id)
            if current is None or (
                current.state,
                current.revision,
                current.owner_id,
                current.owner_fence,
            ) != (
                WorkflowActivityState.RUNNING,
                self.attempt.revision,
                self.attempt.owner_id,
                self.attempt.owner_fence,
            ):
                raise WorkflowStateError(
                    "ADOPT measurement cannot reuse owner", kind="concurrent_modification"
                )
            request = _request(connection, current)
            record = _load_result_adoption(connection, request.adoption_id)
            if record is None or not record.state.terminal:
                return  # Interrupted/non-terminal controller: no precise wall receipt.
            start = connection.execute(
                "SELECT payload_json,payload_fingerprint FROM workflow_adoption_executions WHERE execution_id=?",
                (self.execution_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT ordinal,phase,payload_json,payload_fingerprint FROM workflow_adoption_dispatch_events WHERE execution_id=? ORDER BY ordinal,phase",
                (self.execution_id,),
            ).fetchall()
            events = [(r[0], r[1], _fact(r[2:])) for r in rows]
            if len(events) != self.calls * 2:
                raise WorkflowStateError(
                    "ADOPT incomplete call acknowledgements", kind="needs_attention"
                )
            usage = BudgetAmounts(
                tool_calls=self.calls, wall_milliseconds=(elapsed_ns + 999_999) // 1_000_000
            )
            value = {
                "execution_fingerprint": digest(_fact(start)),
                "events_fingerprint": digest(events),
                "terminal_fingerprint": adoption_terminal_digest(record),
                "elapsed_ns": elapsed_ns,
                "usage": asdict(usage),
            }
            connection.execute(
                "INSERT INTO workflow_adoption_measurements VALUES (?,?,?)",
                (self.execution_id, canonical(value), digest(value)),
            )
            measured_adoption_usage(connection, current, record)
            _reconcile_adoption(
                connection,
                current.invocation.invocation_id,
                parent_session_id=current.invocation.parent_session_id,
                parent_workspace_root=str(record.plan.parent_workspace_root),
                updated_at=max(
                    current.updated_at or current.invocation.created_at, record.updated_at
                ),
                live_execution=self,
            )

        await self._write(finish)


async def execute_adoption(
    context: _SqliteSessionPersistenceContext,
    invocation_id: str,
    *,
    expected_revision: int,
    owner_id: str,
    owner_fence: int,
    updated_at: datetime,
    mutation: WorkspaceMutationPort,
    dispatch: Callable[[WorkflowActivityAttempt, WorkspaceMutationPort], Awaitable[None]],
) -> WorkflowActivityAttempt:
    """Commit a FIRST start, execute through a measured port, seal observed facts.

    No usage/receipt parameter exists. A historical RUNNING start is rejected.
    Composition supplies the real mutation dependency; this is not a new engine.
    """
    with adoption_execution_lock(context._database_path, invocation_id, execution=True):
        return await _execute_locked(
            context,
            invocation_id,
            expected_revision=expected_revision,
            owner_id=owner_id,
            owner_fence=owner_fence,
            updated_at=updated_at,
            mutation=mutation,
            dispatch=dispatch,
        )


async def _execute_locked(
    context: _SqliteSessionPersistenceContext,
    invocation_id: str,
    *,
    expected_revision: int,
    owner_id: str,
    owner_fence: int,
    updated_at: datetime,
    mutation: WorkspaceMutationPort,
    dispatch: Callable[[WorkflowActivityAttempt, WorkspaceMutationPort], Awaitable[None]],
) -> WorkflowActivityAttempt:
    execution_id = "adopt-exec-" + uuid.uuid4().hex

    def start(connection: sqlite3.Connection) -> WorkflowActivityAttempt:
        current = _load_attempt(connection, invocation_id)
        if (
            current is None
            or current.state is not WorkflowActivityState.CLAIMED
            or current.revision != expected_revision
            or (current.owner_id, current.owner_fence) != (owner_id, owner_fence)
        ):
            raise WorkflowStateError(
                "ADOPT measured start requires first owner CAS", kind="concurrent_modification"
            )
        request = _request(connection, current)
        record = _load_result_adoption(connection, request.adoption_id)
        if record is not None:
            raise WorkflowStateError(
                "ADOPT measured start cannot inherit historical execution", kind="protocol"
            )
        started = _start_activity(connection, current, updated_at)
        value = {
            "execution_id": execution_id,
            "invocation_id": invocation_id,
            "adoption_id": request.adoption_id,
            "request_fingerprint": started.invocation.request_fingerprint,
            "owner_id": owner_id,
            "owner_fence": owner_fence,
            "revision": started.revision,
            "reservation_id": started.reservation_id,
        }
        connection.execute(
            "INSERT INTO workflow_adoption_executions VALUES (?,?,?,?)",
            (execution_id, invocation_id, canonical(value), digest(value)),
        )
        return started

    def begin() -> WorkflowActivityAttempt:
        with closing(context._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return start(connection)

    async with context._write_lock:
        attempt = await run_blocking(lambda: _guard(begin))
    authorization = object()  # Neither serialized nor accepted from a caller.
    context._workflow_adoption_meter_scopes[execution_id] = authorization
    meter: _ExecutionMeter | None = None
    try:
        meter = _ExecutionMeter(context, attempt, execution_id, mutation, authorization)
        await dispatch(attempt, meter)
        await meter.complete()
        return attempt
    finally:
        if meter is not None:
            meter.closed = True
        context._workflow_adoption_meter_scopes.pop(execution_id, None)
