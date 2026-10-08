"""Real ADOPT adapter; Interpreter does not execute or own this controller."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from neuro_code.application.ports.completed_dag_adoption import CompletedDagAdoptionSourceAdapter
from neuro_code.application.ports.result_adoption import (
    ResultAdoptionError,
    ResultAdoptionStore,
    WorkspaceMutationPort,
    WorkspaceMutationRequest,
    WorkspaceMutationResult,
)
from neuro_code.application.ports.workflow_activity import WorkflowActivityStore
from neuro_code.application.ports.workflow_adoption import WorkflowAdoptionStore
from neuro_code.application.ports.workflow_state import WorkflowStateError, WorkflowStateStore
from neuro_code.application.workflows.result_adoption import ResultAdoptionApplicationService
from neuro_code.domain.result_adoption import (
    MAX_RESULT_ADOPTION_TARGETS,
    ResultAdoptionPlan,
    ResultAdoptionTargetState,
)
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityState
from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.state import BudgetAmounts, WorkflowStatus


@dataclass(frozen=True, slots=True)
class WorkflowAdoptOutcome:
    attempt: WorkflowActivityAttempt
    disposition: str


class _BoundedAdoptionMutation:
    """An operation ceiling at the existing mutation port, not a mutation engine."""

    def __init__(
        self,
        inner: WorkspaceMutationPort,
        *,
        state: WorkflowStateStore,
        attempt: WorkflowActivityAttempt,
        clock: Callable[[], datetime],
        max_operations: int,
        deadline: datetime,
    ) -> None:
        self.inner, self.state, self.attempt, self.clock = inner, state, attempt, clock
        self.limit, self.deadline = max_operations, deadline
        self.calls = 0
        self.plan: ResultAdoptionPlan | None = None
        self.adoptions: ResultAdoptionStore | None = None
        self.initial_versions: dict[str, int] = {}
        self.uncertain_ceiling = False

    async def apply(
        self, request: WorkspaceMutationRequest, *, session_id: str
    ) -> WorkspaceMutationResult:
        run = await self.state.get_workflow_run(self.attempt.invocation.run_id)
        if (
            run is None
            or run.status is not WorkflowStatus.WAITING
            or run.waiting_reason != "activity:" + self.attempt.invocation.invocation_id
        ):
            raise ResultAdoptionError(
                "Workflow no longer authorizes ADOPT dispatch", kind="cancelled"
            )
        if self.clock() >= self.deadline or self.calls >= self.limit:
            self.uncertain_ceiling = True
            raise ResultAdoptionError(
                "ADOPT pre-dispatch ceiling exhausted", kind="budget_exceeded"
            )
        if (
            self.plan is None
            or session_id != self.plan.parent_session_id
            or not any(
                (t.path, t.operation, t.baseline, t.desired)
                == (request.path, request.operation, request.expected, request.desired)
                for t in self.plan.targets
            )
        ):
            raise ResultAdoptionError("ADOPT mutation differs from frozen plan", kind="integrity")
        if self.adoptions is None:
            raise ResultAdoptionError("ADOPT dispatch evidence unavailable", kind="integrity")
        record = await self.adoptions.get_result_adoption(self.plan.adoption_id)
        if record is None:
            raise ResultAdoptionError("ADOPT dispatch record missing", kind="integrity")
        target = next((t for t in record.targets if t.target.path == request.path), None)
        if (
            target is None
            or target.state is not ResultAdoptionTargetState.APPLYING
            or target.version <= self.initial_versions.get(request.path, 0)
        ):
            self.uncertain_ceiling = True
            raise ResultAdoptionError(
                "ADOPT dispatch boundary is historical", kind="budget_unknown"
            )
        # Each target revision bounds prior dispatch attempts, including uncertain
        # crashes. A repeated APPLYING frame is not another dispatch receipt.
        upper_bound = sum((t.version + 1) // 2 for t in record.targets)
        if upper_bound > self.limit:
            self.uncertain_ceiling = True
            raise ResultAdoptionError(
                "ADOPT durable operation ceiling exhausted", kind="budget_exceeded"
            )
        self.calls += 1  # Counts port invocations, not causal writes or verification.
        try:
            return await self.inner.apply(request, session_id=session_id)
        except ResultAdoptionError as error:
            if error.kind in {"budget_exceeded", "budget_unknown"}:
                self.uncertain_ceiling = True
            raise


class WorkflowAdoptActivityAdapter:
    def __init__(
        self,
        *,
        activities: WorkflowActivityStore,
        adoption_facts: WorkflowAdoptionStore,
        state: WorkflowStateStore,
        adoptions: ResultAdoptionStore,
        source_adapter: CompletedDagAdoptionSourceAdapter,
        service_factory: Callable[[WorkspaceMutationPort], ResultAdoptionApplicationService],
        mutation: WorkspaceMutationPort,
        parent_session_id: str,
        parent_workspace_root: Path,
        max_operations: int = MAX_RESULT_ADOPTION_TARGETS,
        max_wall_milliseconds: int = 30_000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(max_operations) is not int
            or not 1 <= max_operations <= MAX_RESULT_ADOPTION_TARGETS
        ):
            raise ValueError("ADOPT operation ceiling must be bounded")
        if type(max_wall_milliseconds) is not int or not 1 <= max_wall_milliseconds <= 300_000:
            raise ValueError("ADOPT wall ceiling must be bounded")
        if not parent_workspace_root.is_absolute():
            raise ValueError("ADOPT parent root must be absolute")
        self.activities, self.facts, self.state, self.adoptions = (
            activities,
            adoption_facts,
            state,
            adoptions,
        )
        self.source_adapter, self.factory, self.mutation = source_adapter, service_factory, mutation
        self.parent_session_id, self.parent_root = parent_session_id, parent_workspace_root
        self.max_operations, self.max_wall, self.clock = (
            max_operations,
            max_wall_milliseconds,
            clock,
        )
        self.owner = "workflow-adopt-" + uuid.uuid4().hex
        self.lock = asyncio.Lock()
        self._active: (
            tuple[str, _BoundedAdoptionMutation, ResultAdoptionApplicationService] | None
        ) = None

    async def _settle(self, attempt: WorkflowActivityAttempt) -> WorkflowAdoptOutcome:
        terminal = await self.facts.reconcile_workflow_adoption(
            attempt.invocation.invocation_id,
            parent_session_id=self.parent_session_id,
            parent_workspace_root=str(self.parent_root),
            updated_at=self.clock(),
        )
        return WorkflowAdoptOutcome(terminal, "terminal")

    async def run_once(self, invocation_id: str) -> WorkflowAdoptOutcome:
        async with self.lock:
            attempt = await self.activities.get_workflow_activity(invocation_id)
            if (
                attempt is None
                or attempt.invocation.activity is not ActivityKind.ADOPT
                or attempt.invocation.parent_session_id != self.parent_session_id
            ):
                raise WorkflowStateError("exact parent ADOPT invocation required", kind="integrity")
            request = await self.facts.get_workflow_adoption_request(invocation_id)
            record = await self.adoptions.get_result_adoption(request.adoption_id)
            if attempt.state.terminal or (record is not None and record.state.terminal):
                return await self._settle(attempt)
            if attempt.state is WorkflowActivityState.CLAIMED:
                return WorkflowAdoptOutcome(attempt, "busy")  # Never borrow its owner token.
            run = await self.state.get_workflow_run(attempt.invocation.run_id)
            if (
                run is None
                or run.status is not WorkflowStatus.WAITING
                or run.waiting_reason != "activity:" + invocation_id
            ):
                return WorkflowAdoptOutcome(attempt, "not_authorized")
            fresh = attempt.state is WorkflowActivityState.READY
            if not fresh and record is None:
                await self.facts.mark_workflow_adoption_attention(
                    invocation_id, expected_revision=attempt.revision, updated_at=self.clock()
                )
                return WorkflowAdoptOutcome(attempt, "needs_attention")
            await self.source_adapter.resolve(request, parent_session_id=self.parent_session_id)
            # Factory verifies existing parent capability/workspace authority before claim.
            bounded = _BoundedAdoptionMutation(
                self.mutation,
                state=self.state,
                attempt=attempt,
                clock=self.clock,
                max_operations=self.max_operations,
                deadline=self.clock() + timedelta(milliseconds=self.max_wall),
            )
            if self._active is not None and self._active[0] == invocation_id:
                _, bounded, service = self._active
            else:
                service = self.factory(bounded)
                self._active = (invocation_id, bounded, service)
            if service.parent_session_id != self.parent_session_id:
                raise WorkflowStateError("ADOPT service parent differs", kind="integrity")
            if fresh:
                attempt = await self.activities.claim_workflow_activity(
                    invocation_id,
                    expected_revision=attempt.revision,
                    owner_id=self.owner,
                    reserved=BudgetAmounts(
                        tool_calls=self.max_operations, wall_milliseconds=self.max_wall
                    ),
                    updated_at=self.clock(),
                )

            async def invoke(started: WorkflowActivityAttempt) -> WorkflowAdoptOutcome | None:
                attempt = started
                assert attempt.reserved is not None
                assert attempt.updated_at is not None
                bounded.attempt = attempt
                bounded.limit = attempt.reserved.tool_calls or 0
                bounded.deadline = attempt.updated_at + timedelta(
                    milliseconds=attempt.reserved.wall_milliseconds or 0
                )
                if fresh and self.clock() >= bounded.deadline:
                    await self.facts.mark_workflow_adoption_attention(
                        invocation_id, expected_revision=attempt.revision, updated_at=self.clock()
                    )
                    return WorkflowAdoptOutcome(attempt, "needs_attention")
                prepared = await service.prepare(request)
                bounded.plan = prepared.plan
                bounded.adoptions = self.adoptions
                bounded.initial_versions = {t.target.path: t.version for t in prepared.targets}
                if len(prepared.plan.targets) > bounded.limit:
                    await self.facts.mark_workflow_adoption_attention(
                        invocation_id, expected_revision=attempt.revision, updated_at=self.clock()
                    )
                    return WorkflowAdoptOutcome(attempt, "needs_attention")
                try:
                    record = await service.adopt(request)
                except ResultAdoptionError as error:
                    if error.kind != "busy":
                        raise
                    return WorkflowAdoptOutcome(attempt, "busy")
                if not record.state.terminal:
                    if bounded.uncertain_ceiling:
                        await self.facts.mark_workflow_adoption_attention(
                            invocation_id,
                            expected_revision=attempt.revision,
                            updated_at=self.clock(),
                        )
                        return WorkflowAdoptOutcome(attempt, "needs_attention")
                    return WorkflowAdoptOutcome(attempt, "waiting_underlying")
                return None

            if fresh:
                pending: WorkflowAdoptOutcome | None = None

                async def dispatch(
                    started: WorkflowActivityAttempt, port: WorkspaceMutationPort
                ) -> None:
                    nonlocal pending
                    bounded.inner = port
                    pending = await invoke(started)

                attempt = await self.facts.execute_workflow_adoption(
                    invocation_id,
                    expected_revision=attempt.revision,
                    owner_id=self.owner,
                    owner_fence=attempt.owner_fence,
                    updated_at=self.clock(),
                    mutation=self.mutation,
                    dispatch=dispatch,
                )
            else:
                # Existing forward recovery stays bounded, but cannot reopen a
                # completed live measurement scope or manufacture known usage.
                bounded.inner = self.mutation
                pending = await invoke(attempt)
            return pending if pending is not None else await self._settle(attempt)
