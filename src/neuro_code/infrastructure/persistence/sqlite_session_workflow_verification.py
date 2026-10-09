"""Single first-dispatch VERIFY scope, durable evidence and atomic settlement."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import closing, suppress
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from neuro_code.application.ports.workflow_activity import WorkflowActivityStore
from neuro_code.application.ports.workflow_interpreter import WorkflowInterpreterStore
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.ports.workflow_verification import (
    ApprovedWorkflowVerification,
    WorkflowVerificationCommand,
    WorkflowVerificationWorkspace,
)
from neuro_code.domain.tools import ToolExecutionResult
from neuro_code.domain.workflows.activity import (
    WorkflowActivityAttempt,
    WorkflowActivityResult,
)
from neuro_code.domain.workflows.activity import (
    WorkflowActivityState as State,
)
from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.interpreter import WorkflowStepOutput
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    WorkflowEventKind,
    WorkflowFailure,
    WorkflowStatus,
    WorkflowWriteResult,
    identifier,
    timestamp,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity import (
    _load_attempt,
    _run,
    _settle_terminal,
    _start_activity,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_verification_facts import (
    execution,
    parent_root,
    source,
    terminal_anchor,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_verification_lock import (
    verification_execution_lock,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_verification_scope import (
    _consuming,
    _revalidate,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _append_event,
    _guard,
    _save_run,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.redaction import redact_sensitive_text

_executing: ContextVar[tuple[str, str, object] | None] = ContextVar(
    "verify_first_execution", default=None
)


def _bound(attempt: WorkflowActivityAttempt | None, session: str) -> WorkflowActivityAttempt:
    if (
        attempt is None
        or attempt.invocation.activity is not ActivityKind.VERIFY
        or attempt.invocation.parent_session_id != session
    ):
        raise WorkflowStateError("exact parent VERIFY required", kind="integrity")
    return attempt


class WorkflowVerificationMixin(_SqliteSessionPersistenceContext):
    async def _verify_read(self, key: str, session: str) -> WorkflowActivityAttempt:
        def read() -> WorkflowActivityAttempt:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return _bound(_load_attempt(connection, key), session)

        return await run_blocking(lambda: _guard(read))

    async def execute_workflow_verification(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: Path,
        configuration: ApprovedWorkflowVerification | None,
        command: WorkflowVerificationCommand,
        workspace: WorkflowVerificationWorkspace,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt:
        identifier(invocation_id)
        timestamp(updated_at)
        if configuration is not None and not isinstance(
            configuration, ApprovedWorkflowVerification
        ):
            raise TypeError("VERIFY requires trusted configuration")
        if command.workspace_root != parent_workspace_root:
            raise WorkflowStateError("VERIFY executor root differs", kind="integrity")
        with verification_execution_lock(self._database_path, invocation_id):
            current = await self._verify_read(invocation_id, parent_session_id)
            if current.state is not State.READY:
                # Historical RUNNING is NEVER a first-dispatch authorization.
                return await self._recover_locked(current, parent_workspace_root, updated_at)
            token = _executing.set(
                (str(self._database_path.resolve()), invocation_id, asyncio.current_task())
            )
            try:
                return await self._execute_first(
                    invocation_id,
                    parent_session_id=parent_session_id,
                    parent_workspace_root=parent_workspace_root,
                    configuration=configuration,
                    command=command,
                    workspace=workspace,
                    updated_at=updated_at,
                )
            finally:
                _executing.reset(token)

    async def _execute_first(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: Path,
        configuration: ApprovedWorkflowVerification | None,
        command: WorkflowVerificationCommand,
        workspace: WorkflowVerificationWorkspace,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt:
        current = await self._verify_read(invocation_id, parent_session_id)

        def check_parent() -> None:
            with closing(self._connect()) as connection:
                if parent_root(connection, current) != str(parent_workspace_root):
                    raise WorkflowStateError(
                        "VERIFY session belongs to another root", kind="integrity"
                    )

        await run_blocking(check_parent)
        started_ns = time.perf_counter_ns()
        watch = None
        before: dict[str, Any] | None = None
        blocked = configuration is None
        if configuration is not None:
            try:
                async with asyncio.timeout(5):
                    watch = await workspace.watch(parent_workspace_root)
                    before = asdict(await workspace.evidence(parent_workspace_root))
                if Path(before["root"]) != parent_workspace_root:
                    raise WorkflowStateError("VERIFY workspace root differs", kind="integrity")
            except WorkflowStateError:
                raise
            except Exception:
                blocked = True  # Unsupported/bounded workspace cannot produce PASS.
        activities = cast(WorkflowActivityStore, self)
        current = await activities.claim_workflow_activity(
            invocation_id,
            expected_revision=0,
            owner_id="verify-owner-" + uuid.uuid4().hex,
            reserved=BudgetAmounts(
                tool_calls=0 if blocked else 1,
                wall_milliseconds=1000
                if blocked
                else (configuration.timeout_milliseconds if configuration else 0) + 5000,
            ),
            updated_at=updated_at,
        )
        key = "verify-exec-" + uuid.uuid4().hex

        def begin() -> WorkflowActivityAttempt:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                active = _bound(_load_attempt(connection, invocation_id), parent_session_id)
                if (active.state, active.revision, active.owner_id, active.owner_fence) != (
                    State.CLAIMED,
                    1,
                    current.owner_id,
                    current.owner_fence,
                ):
                    raise WorkflowStateError(
                        "VERIFY first owner CAS differs", kind="concurrent_modification"
                    )
                if parent_root(connection, active) != str(parent_workspace_root):
                    raise WorkflowStateError("VERIFY parent session root differs", kind="integrity")
                run = _run(connection, active.invocation.run_id)
                assert active.reserved is not None
                value = {
                    "execution_id": key,
                    "invocation_fingerprint": active.invocation.fingerprint,
                    "parent_session_id": parent_session_id,
                    "parent_workspace_root": str(parent_workspace_root),
                    "owner_id": active.owner_id,
                    "owner_fence": active.owner_fence,
                    "reservation_id": active.reservation_id,
                    "reserved": asdict(active.reserved),
                    "configuration": asdict(configuration) if configuration is not None else None,
                    "workspace_before": before,
                    "workspace_generation": run.generation,
                    "workflow_owner_id": run.owner_id,
                    "workflow_owner_fence": run.owner_fence,
                    "started_at": active.updated_at.isoformat()
                    if active.updated_at is not None
                    else updated_at.isoformat(),
                    "source_bindings": source(connection, active),
                }
                connection.execute(
                    "INSERT INTO workflow_verification_executions VALUES (?,?,?,?)",
                    (key, invocation_id, canonical(value), digest(value)),
                )
                return active if blocked else _start_activity(connection, active, updated_at)

        async with self._write_lock:
            current = await run_blocking(lambda: _guard(begin))
        if blocked:
            return await self._finish_verification(
                current,
                State.BLOCKED,
                "blocked",
                None,
                before,
                None,
                BudgetAmounts(
                    wall_milliseconds=(time.perf_counter_ns() - started_ns + 999999) // 1000000
                ),
                updated_at,
            )
        assert configuration is not None

        # Cancellation observes the durable run while the existing Bash
        # adapter owns timeout and process-tree teardown. No daemon/timer
        # survives this one bounded execution scope.
        scope = _executing.get()
        guard_used = False

        async def guard() -> bool:
            nonlocal guard_used
            if guard_used or _executing.get() != scope:
                return False
            guard_used = True

            def check() -> bool:
                with closing(self._connect()) as connection, connection:
                    connection.execute("BEGIN")
                    active = _bound(_load_attempt(connection, invocation_id), parent_session_id)
                    started = execution(connection, active)
                    run = _run(connection, active.invocation.run_id)
                    return (
                        active.state is State.RUNNING
                        and (active.revision, active.owner_id, active.owner_fence)
                        == (current.revision, current.owner_id, current.owner_fence)
                        and started["execution_id"] == key
                        and started["configuration"] == asdict(configuration)
                        and started["parent_workspace_root"] == str(parent_workspace_root)
                        and run.status is WorkflowStatus.WAITING
                        and run.waiting_reason == "activity:" + invocation_id
                        and (run.owner_id, run.owner_fence)
                        == (started["workflow_owner_id"], started["workflow_owner_fence"])
                        and run.ledger.committed.known
                    )

            return await run_blocking(lambda: _guard(check))

        async def invoke() -> ToolExecutionResult:
            return await asyncio.wait_for(
                command.verify_command(
                    configuration, session_id=parent_session_id, pre_entry_guard=guard
                ),
                configuration.timeout_milliseconds / 1000 + 1,
            )

        def cancelled() -> bool:
            with closing(self._connect()) as connection:
                return _run(connection, current.invocation.run_id).status.terminal

        if await run_blocking(cancelled):
            return await self._recover_locked(current, parent_workspace_root, updated_at)
        changed_during_execution = False

        task = asyncio.create_task(invoke(), name="workflow-verify-command")
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=0.05)
                if done:
                    break
                assert watch is not None
                if not await watch.unchanged():
                    changed_during_execution = True
                if await run_blocking(cancelled):
                    task.cancel()
                    await task  # Process teardown completes before releasing OS lock.
            result = await task
        except BaseException:
            if not task.done():
                task.cancel()
            with suppress(BaseException):
                await task
            raise  # No receipt: recovery conservatively records unknown.
        if not isinstance(result, ToolExecutionResult):
            raise WorkflowStateError("VERIFY executor returned no typed evidence", kind="protocol")
        assert watch is not None
        changed_during_execution |= not await watch.unchanged()
        after = asdict(await workspace.evidence(parent_workspace_root))
        code = (result.metadata or {}).get("exit_code")
        if type(code) is not int:
            code = None
        usage = BudgetAmounts(
            tool_calls=0 if result.not_started else 1,
            wall_milliseconds=(time.perf_counter_ns() - started_ns + 999999) // 1000000,
        )
        if result.not_started:
            state, outcome = State.BLOCKED, "blocked"
        elif (
            changed_during_execution
            or before != after
            or (code in (0, 1) and result.is_error != (code != 0))
            or result.cancelled
            or code is None
            or code < 0
        ):
            state, outcome = State.INDETERMINATE, "uncertain"
        elif code in (0, 1):
            state, outcome = State.COMPLETED, "PASS" if code == 0 else "FAIL"
        else:
            state, outcome = State.FAILED, "executor_error"
        summary = (
            redact_sensitive_text(result.content)
            .encode("utf-8")[:4096]
            .decode("utf-8", errors="ignore")
        )
        return await self._finish_verification(
            current,
            state,
            outcome,
            code,
            after,
            summary,
            usage,
            updated_at,
            observation={
                "call_id": result.call_id,
                "tool_name": result.tool_name,
                "is_error": result.is_error,
                "not_started": result.not_started,
                "cancelled": result.cancelled,
            },
        )

    async def _finish_verification(
        self,
        attempt: WorkflowActivityAttempt,
        state: State,
        outcome: str,
        code: int | None,
        after: dict[str, Any] | None,
        summary: str | None,
        usage: BudgetAmounts,
        now: datetime,
        *,
        observation: dict[str, Any] | None = None,
    ) -> WorkflowActivityAttempt:
        if usage.known and _executing.get() != (
            str(self._database_path.resolve()),
            attempt.invocation.invocation_id,
            asyncio.current_task(),
        ):
            raise WorkflowStateError(
                "VERIFY known usage needs live first execution", kind="protocol"
            )

        def finish() -> WorkflowActivityAttempt:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _bound(
                    _load_attempt(connection, attempt.invocation.invocation_id),
                    attempt.invocation.parent_session_id,
                )
                if current.state.terminal:
                    return current
                if (current.revision, current.owner_id, current.owner_fence) != (
                    attempt.revision,
                    attempt.owner_id,
                    attempt.owner_fence,
                ):
                    raise WorkflowStateError(
                        "VERIFY settlement owner differs", kind="concurrent_modification"
                    )
                started = execution(connection, current)
                run = _run(connection, current.invocation.run_id)
                assert current.updated_at is not None
                finished_at = max(datetime.now(UTC), now, run.updated_at, current.updated_at)
                output = (
                    canonical(
                        {"status": outcome, "workspace_generation": started["workspace_generation"]}
                    )
                    if state is State.COMPLETED
                    else None
                )
                value = {
                    "execution_fingerprint": digest(started),
                    "finished_at": finished_at.isoformat(),
                    "state": state.value,
                    "outcome": outcome,
                    "exit_code": code,
                    "workspace_after": after,
                    "summary": summary,
                    "output_fingerprint": digest(summary),
                    "usage": asdict(usage),
                    "output_json": output,
                    "observation": observation,
                }
                connection.execute(
                    "INSERT INTO workflow_verification_evidence VALUES (?,?,?)",
                    (started["execution_id"], canonical(value), digest(value)),
                )
                run = _run(connection, current.invocation.run_id)
                assert current.updated_at is not None
                assert current.reserved is not None
                assert current.reserved.wall_milliseconds is not None
                result = WorkflowActivityResult(
                    current.invocation.invocation_id,
                    current.invocation.request_fingerprint,
                    ActivityKind.VERIFY,
                    state,
                    started["execution_id"],
                    digest(value),
                    usage,
                    finished_at,
                    output,
                )
                terminal = _settle_terminal(
                    connection,
                    current,
                    result,
                    verification=terminal_anchor(current, started, value, result),
                )
                run = _run(connection, current.invocation.run_id)
                overrun = (
                    usage.wall_milliseconds is not None
                    and usage.wall_milliseconds > current.reserved.wall_milliseconds
                )
                if (
                    (overrun or state is State.INDETERMINATE)
                    and not run.status.terminal
                    and run.status is not WorkflowStatus.NEEDS_ATTENTION
                ):
                    changed = replace(
                        run,
                        generation=run.generation + 1,
                        status=WorkflowStatus.NEEDS_ATTENTION,
                        waiting_reason=None,
                        failure=WorkflowFailure(
                            "verify_uncertain" if not overrun else "verify_budget_exceeded",
                            "VERIFY needs explicit recovery",
                        ),
                    )
                    _save_run(connection, changed, expected=run)
                    _append_event(
                        connection,
                        changed,
                        "verify-attention:" + current.invocation.invocation_id,
                        WorkflowEventKind.ACTIVITY,
                        canonical(
                            {
                                "operation": "verify_attention",
                                "invocation_id": current.invocation.invocation_id,
                            }
                        ),
                    )
                return terminal

        async with self._write_lock:
            return await run_blocking(lambda: _guard(finish))

    async def _recover_locked(
        self, current: WorkflowActivityAttempt, root: Path, now: datetime
    ) -> WorkflowActivityAttempt:
        def read() -> dict[str, Any]:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if not connection.execute(
                    "SELECT 1 FROM workflow_verification_executions WHERE invocation_id=?",
                    (current.invocation.invocation_id,),
                ).fetchone():
                    if current.state not in {State.CLAIMED, State.RUNNING}:
                        raise WorkflowStateError("VERIFY execution fact missing", kind="integrity")
                    if parent_root(connection, current) != str(root):
                        raise WorkflowStateError("VERIFY recovery parent differs", kind="integrity")
                    assert current.reserved is not None
                    value = {
                        "execution_id": "verify-exec-" + uuid.uuid4().hex,
                        "invocation_fingerprint": current.invocation.fingerprint,
                        "parent_session_id": current.invocation.parent_session_id,
                        "parent_workspace_root": str(root),
                        "owner_id": current.owner_id,
                        "owner_fence": current.owner_fence,
                        "reservation_id": current.reservation_id,
                        "reserved": asdict(current.reserved),
                        "configuration": None,
                        "workspace_before": None,
                        "started_at": current.updated_at.isoformat()
                        if current.updated_at is not None
                        else now.isoformat(),
                        "workspace_generation": _run(
                            connection, current.invocation.run_id
                        ).generation,
                        "source_bindings": source(connection, current),
                    }
                    connection.execute(
                        "INSERT INTO workflow_verification_executions VALUES (?,?,?,?)",
                        (
                            value["execution_id"],
                            current.invocation.invocation_id,
                            canonical(value),
                            digest(value),
                        ),
                    )
                return execution(connection, current)

        started = await run_blocking(lambda: _guard(read))
        if started["parent_workspace_root"] != str(root):
            raise WorkflowStateError("VERIFY parent root differs", kind="integrity")
        if current.state.terminal:
            return current
        # An abandoned CLAIMED boundary has not dispatched; historical RUNNING
        # has no reliable port/time result and cannot be retried automatically.
        usage = BudgetAmounts(
            tool_calls=None if current.state is State.RUNNING else 0, wall_milliseconds=None
        )
        return await self._finish_verification(
            current, State.INDETERMINATE, "unknown", None, None, None, usage, now
        )

    async def reconcile_workflow_verification(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: Path,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt:
        current = await self._verify_read(invocation_id, parent_session_id)
        if current.state.terminal:
            return await self._recover_locked(current, parent_workspace_root, updated_at)
        with verification_execution_lock(self._database_path, invocation_id):
            current = await self._verify_read(invocation_id, parent_session_id)
            return await self._recover_locked(current, parent_workspace_root, updated_at)

    async def consume_workflow_verification(
        self,
        output: WorkflowStepOutput,
        *,
        workspace: WorkflowVerificationWorkspace,
        parent_session_id: str,
        parent_workspace_root: Path,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult:
        current = await self._verify_read(output.source_id, parent_session_id)

        def read() -> dict[str, Any]:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return execution(connection, current)

        started = await run_blocking(lambda: _guard(read))
        if (
            started["parent_workspace_root"] != str(parent_workspace_root)
            or current.result is None
            or current.result.fingerprint != output.source_fingerprint
        ):
            raise WorkflowStateError("VERIFY consumption binding differs", kind="integrity")

        # FAIL also belongs to an exact source revision, not arbitrary newer code.
        async def validate() -> None:
            live = asdict(await workspace.evidence(parent_workspace_root))
            if live != started["workspace_before"]:
                raise WorkflowStateError("VERIFY result is stale", kind="stale_verification")

        await validate()
        token = _consuming.set(
            (str(self._database_path.resolve()), output.fingerprint, asyncio.current_task())
        )
        validation = _revalidate.set(validate)
        try:
            return await cast(WorkflowInterpreterStore, self).commit_workflow_step_output(
                output,
                expected_generation=expected_generation,
                owner_id=owner_id,
                owner_fence=owner_fence,
                updated_at=updated_at,
            )
        finally:
            _revalidate.reset(validation)
            _consuming.reset(token)
