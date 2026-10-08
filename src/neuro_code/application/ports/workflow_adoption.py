"""Exact ADOPT binding and terminal-only reconciliation; no owner takeover."""

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from typing import Protocol

from neuro_code.application.ports.result_adoption import ResultAdoptionRecord, WorkspaceMutationPort
from neuro_code.domain.completed_dag_adoption import WorkflowAdoptionSourceRef
from neuro_code.domain.result_adoption import (
    ResultAdoptionRequest,
    ResultAdoptionState,
    workspace_entry_fingerprint,
)
from neuro_code.domain.workflows.activity import (
    WorkflowActivityAttempt,
    WorkflowActivityInvocation,
    WorkflowActivityState,
)
from neuro_code.domain.workflows.publication import digest
from neuro_code.domain.workflows.state import BudgetAmounts


def workflow_adoption_id(
    invocation: WorkflowActivityInvocation, source: WorkflowAdoptionSourceRef
) -> str:
    return "adopt-" + digest(
        [
            invocation.run_id,
            invocation.invocation_id,
            invocation.step.key,
            invocation.request_fingerprint,
            asdict(source),
            invocation.parent_session_id,
        ]
    )


def adoption_terminal_digest(record: ResultAdoptionRecord) -> str:
    if not record.state.terminal:
        raise ValueError("adoption proof requires a terminal record")
    if any(
        target.target != planned
        for target, planned in zip(record.targets, record.plan.targets, strict=True)
    ):
        raise ValueError("adoption terminal targets differ from frozen plan")
    if record.state is ResultAdoptionState.COMPLETED and any(
        t.state.value != "applied"
        or t.observed_fingerprint != workspace_entry_fingerprint(t.target.desired)
        for t in record.targets
    ):
        raise ValueError("completed adoption has unresolved targets")
    return digest(
        {
            "adoption_id": record.adoption_id,
            "plan_fingerprint": record.plan_fingerprint,
            "state": record.state.value,
            "version": record.version,
            "targets": [
                {
                    "target": t.target.to_dict(),
                    "state": t.state.value,
                    "observed_fingerprint": t.observed_fingerprint,
                    "error_kind": t.error_kind,
                    "version": t.version,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                }
                for t in record.targets
            ],
            "error_kind": record.error_kind,
            "parent_workspace_changed": record.parent_workspace_changed,
            "terminal_at": record.updated_at.isoformat(),
        }
    )


def adoption_recovery_usage(record: ResultAdoptionRecord) -> BudgetAmounts:
    """Absent complete controlled measurement, target revisions are not usage."""
    return BudgetAmounts(tool_calls=None if record.plan.targets else 0, wall_milliseconds=None)


def adoption_activity_state(state: ResultAdoptionState) -> WorkflowActivityState:
    return {
        ResultAdoptionState.COMPLETED: WorkflowActivityState.COMPLETED,
        ResultAdoptionState.CONFLICT: WorkflowActivityState.BLOCKED,
        ResultAdoptionState.FAILED: WorkflowActivityState.FAILED,
        ResultAdoptionState.INDETERMINATE: WorkflowActivityState.INDETERMINATE,
    }[state]


class WorkflowAdoptionStore(Protocol):
    async def get_workflow_adoption_request(self, invocation_id: str) -> ResultAdoptionRequest: ...

    async def reconcile_workflow_adoption(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: str,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt:
        """Read the exact underlying terminal fact and atomically settle once.

        No caller-supplied usage is accepted. Only repository-owned completed execution measurement can provide
        known operation/wall usage; incomplete historical execution stays unknown.
        Neither path claims
        execution ownership or invokes mutation.
        """
        ...

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
        """First-start CAS plus controlled port invocation/time measurement.

        This execution boundary accepts neither amounts nor a caller receipt.
        The real mutation dependency comes from trusted composition, as for the
        existing adoption engine. Historical RUNNING replay cannot enter it.
        """
        ...

    async def mark_workflow_adoption_attention(
        self, invocation_id: str, *, expected_revision: int, updated_at: datetime
    ) -> None: ...
