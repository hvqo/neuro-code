"""Internal, composition-owned REPAIR ports; never model tool arguments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from neuro_code.application.ports.workflow_verification import VerificationWorkspaceEvidence
from neuro_code.domain.conversation.events import AgentEvent
from neuro_code.domain.execution import AgentExecutionOutcome, ExecutionBudget
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt
from neuro_code.domain.workflows.state import BudgetAmounts, integer


@dataclass(frozen=True, slots=True)
class RepairWorkspaceEvidence:
    workspace: VerificationWorkspaceEvidence
    paths: tuple[tuple[str, str], ...]


class WorkflowRepairWorkspace(Protocol):
    async def snapshot(self, root: Path) -> RepairWorkspaceEvidence: ...


@dataclass(frozen=True, slots=True)
class WorkflowRepairLimits:
    """Trusted upper bounds, intersected with the existing parent turn budget."""

    model_calls: int = 4
    tool_calls: int = 8
    input_tokens: int = 32_000
    output_tokens: int = 8_000
    wall_milliseconds: int = 60_000

    def __post_init__(self) -> None:
        for value in (
            self.model_calls,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.wall_milliseconds,
        ):
            integer(value)
            if value < 1:
                raise ValueError("REPAIR limits must be positive")
        if self.model_calls > 32 or self.tool_calls > 64 or self.wall_milliseconds > 120_000:
            raise ValueError("REPAIR must remain one bounded turn")

    def reservation(self, parent: ExecutionBudget) -> BudgetAmounts:
        return BudgetAmounts(
            model_calls=min(self.model_calls, parent.max_model_calls),
            tool_calls=min(self.tool_calls, parent.max_tool_calls),
            input_tokens=min(self.input_tokens, parent.max_input_tokens or self.input_tokens),
            output_tokens=min(self.output_tokens, parent.max_output_tokens or self.output_tokens),
            wall_milliseconds=min(
                self.wall_milliseconds,
                int(parent.max_wall_seconds * 1000)
                if parent.max_wall_seconds
                else self.wall_milliseconds,
            ),
        )


class RepairExecutionObserver(Protocol):
    """Only the live first-execution scope can implement these observations."""

    async def enter(self, kind: str, identity: str, detail: str) -> int: ...

    async def returned(
        self,
        ordinal: int,
        *,
        failed: bool,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        tool_unknown: bool = False,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RepairTurnResult:
    session_id: str | None
    turn_id: str | None
    response: str
    outcome: AgentExecutionOutcome | None
    events: tuple[AgentEvent, ...]


class WorkflowRepairTurn(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def workspace_root(self) -> Path: ...

    @property
    def execution_budget(self) -> ExecutionBudget: ...

    async def run_repair(
        self,
        prompt: str,
        *,
        turn_id: str,
        budget: ExecutionBudget,
        observer: RepairExecutionObserver,
    ) -> RepairTurnResult: ...


class WorkflowRepairStore(Protocol):
    async def execute_workflow_repair(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: Path,
        turn: WorkflowRepairTurn,
        workspace: WorkflowRepairWorkspace,
        limits: WorkflowRepairLimits,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt: ...

    async def reconcile_workflow_repair(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: Path,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt: ...
