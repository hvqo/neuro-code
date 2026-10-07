"""DW5a persistence and deterministic fake Activity seams, without mutation authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.interpreter import WorkflowStepOutput
from neuro_code.domain.workflows.state import StepIdentity, WorkflowWriteResult


class WorkflowInterpreterStore(Protocol):
    async def put_workflow_input(self, run_id: str, input_json: str) -> None: ...
    async def get_workflow_input(self, run_id: str) -> str: ...
    async def get_workflow_step_output(
        self, run_id: str, step: StepIdentity
    ) -> WorkflowStepOutput | None: ...
    async def commit_workflow_step_output(
        self,
        output: WorkflowStepOutput,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult: ...


@dataclass(frozen=True, slots=True)
class FakeActivityInvocation:
    invocation_id: str
    run_id: str
    step: StepIdentity
    activity: ActivityKind
    input_json: str


class FakeWorkflowActivity(Protocol):
    """Pure deterministic evaluation only; DW5b must define real side-effect recovery."""

    def evaluate(self, invocation: FakeActivityInvocation) -> str: ...
