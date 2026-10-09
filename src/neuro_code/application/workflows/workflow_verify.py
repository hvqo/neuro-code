"""Real foreground VERIFY adapter; the Interpreter never executes a command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.ports.workflow_activity import WorkflowActivityStore
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.ports.workflow_verification import (
    ApprovedWorkflowVerification,
    WorkflowVerificationCommand,
    WorkflowVerificationStore,
    WorkflowVerificationWorkspace,
)
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityState
from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.interpreter import WorkflowStepOutput
from neuro_code.domain.workflows.state import WorkflowWriteResult


@dataclass(frozen=True, slots=True)
class WorkflowVerifyOutcome:
    attempt: WorkflowActivityAttempt
    disposition: str


class WorkflowVerifyActivityAdapter:
    def __init__(
        self,
        *,
        store: WorkflowVerificationStore,
        activities: WorkflowActivityStore,
        command: WorkflowVerificationCommand,
        workspace: WorkflowVerificationWorkspace,
        parent_session_id: str,
        parent_workspace_root: Path,
        configuration: ApprovedWorkflowVerification | None,
    ) -> None:
        if command.workspace_root != parent_workspace_root:
            raise WorkflowStateError(
                "VERIFY executor belongs to another workspace", kind="integrity"
            )
        self.store, self.activities = store, activities
        self.command, self.workspace = command, workspace
        self.session, self.root, self.configuration = (
            parent_session_id,
            parent_workspace_root,
            configuration,
        )

    async def run_once(self, invocation_id: str) -> WorkflowVerifyOutcome:
        attempt = await self.activities.get_workflow_activity(invocation_id)
        if attempt is None or attempt.invocation.activity is not ActivityKind.VERIFY:
            raise WorkflowStateError("exact VERIFY invocation required", kind="protocol")
        try:
            if attempt.state is WorkflowActivityState.READY:
                result = await self.store.execute_workflow_verification(
                    invocation_id,
                    parent_session_id=self.session,
                    parent_workspace_root=self.root,
                    configuration=self.configuration,
                    command=self.command,
                    workspace=self.workspace,
                    updated_at=datetime.now(UTC),
                )
            else:
                result = await self.store.reconcile_workflow_verification(
                    invocation_id,
                    parent_session_id=self.session,
                    parent_workspace_root=self.root,
                    updated_at=datetime.now(UTC),
                )
            return WorkflowVerifyOutcome(result, result.state.value)
        except WorkflowStateError as error:
            if error.kind != "concurrent_execution":
                raise
            current = await self.activities.get_workflow_activity(invocation_id)
            assert current is not None
            return WorkflowVerifyOutcome(current, "busy")

    async def consume(
        self,
        output: WorkflowStepOutput,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult:
        return await self.store.consume_workflow_verification(
            output,
            workspace=self.workspace,
            parent_session_id=self.session,
            parent_workspace_root=self.root,
            expected_generation=expected_generation,
            owner_id=owner_id,
            owner_fence=owner_fence,
            updated_at=updated_at,
        )
