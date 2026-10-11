"""One explicit REPAIR adapter using the parent's canonical conversation turn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.ports.execution_observation import observe_execution
from neuro_code.application.ports.workflow_activity import WorkflowActivityStore
from neuro_code.application.ports.workflow_repair import (
    RepairExecutionObserver,
    RepairTurnResult,
    WorkflowRepairLimits,
    WorkflowRepairStore,
    WorkflowRepairTurn,
    WorkflowRepairWorkspace,
)
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.domain.conversation.prompt_continuity import ModelRequestSource
from neuro_code.domain.execution import ExecutionBudget, TurnSource
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityState
from neuro_code.domain.workflows.definition import ActivityKind


class BoundWorkflowRepairTurn:
    """Retain the exact parent binding, including its turn lock and authority.

    The internal source is persisted in the parent session; no fresh binding,
    hidden session, profile substitution, or separate Agent loop is created.
    """

    def __init__(self, binding: ConversationBinding) -> None:
        if binding.runner.session_id is None or binding.workspace_root is None:
            raise WorkflowStateError("REPAIR needs a persisted parent binding", kind="protocol")
        self.binding = binding
        self._session = binding.runner.session_id
        self._root = binding.workspace_root

    @property
    def session_id(self) -> str:
        return self._session

    @property
    def workspace_root(self) -> Path:
        return self._root

    @property
    def execution_budget(self) -> ExecutionBudget:
        return self.binding.runner.execution_budget

    async def run_repair(
        self,
        prompt: str,
        *,
        turn_id: str,
        budget: ExecutionBudget,
        observer: RepairExecutionObserver,
    ) -> RepairTurnResult:
        if self.binding.runner.session_id != self._session:
            raise WorkflowStateError("REPAIR parent session changed", kind="integrity")
        with observe_execution(observer):
            result = await self.binding.runner.run(
                prompt,
                turn_id=turn_id,
                turn_source=TurnSource.WORKFLOW_REPAIR,
                model_request_source=ModelRequestSource.WORKFLOW_REPAIR,
                execution_budget_override=budget,
            )
        return RepairTurnResult(
            result.session_id, result.turn_id, result.response, result.outcome, tuple(result.events)
        )


@dataclass(frozen=True, slots=True)
class WorkflowRepairOutcome:
    attempt: WorkflowActivityAttempt
    disposition: str


class WorkflowRepairActivityAdapter:
    def __init__(
        self,
        *,
        store: WorkflowRepairStore,
        activities: WorkflowActivityStore,
        turn: WorkflowRepairTurn,
        workspace: WorkflowRepairWorkspace,
        limits: WorkflowRepairLimits | None = None,
    ) -> None:
        self.store, self.activities, self.turn, self.workspace, self.limits = (
            store,
            activities,
            turn,
            workspace,
            limits or WorkflowRepairLimits(),
        )

    async def run_once(self, invocation_id: str) -> WorkflowRepairOutcome:
        current = await self.activities.get_workflow_activity(invocation_id)
        if current is None or current.invocation.activity is not ActivityKind.REPAIR:
            raise WorkflowStateError("exact REPAIR required", kind="protocol")
        try:
            if current.state is WorkflowActivityState.READY:
                result = await self.store.execute_workflow_repair(
                    invocation_id,
                    parent_session_id=self.turn.session_id,
                    parent_workspace_root=self.turn.workspace_root,
                    turn=self.turn,
                    workspace=self.workspace,
                    limits=self.limits,
                    updated_at=datetime.now(UTC),
                )
            else:
                result = await self.store.reconcile_workflow_repair(
                    invocation_id,
                    parent_session_id=self.turn.session_id,
                    parent_workspace_root=self.turn.workspace_root,
                    updated_at=datetime.now(UTC),
                )
            return WorkflowRepairOutcome(result, result.state.value)
        except WorkflowStateError as error:
            if error.kind != "concurrent_execution":
                raise
            current = await self.activities.get_workflow_activity(invocation_id)
            assert current is not None
            return WorkflowRepairOutcome(current, "busy")
