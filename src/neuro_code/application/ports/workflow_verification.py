"""Trusted composition and durable VERIFY boundaries, never model tool arguments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from neuro_code.domain.permissions import validate_verification_command
from neuro_code.domain.tools import ToolExecutionResult
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt
from neuro_code.domain.workflows.interpreter import WorkflowStepOutput
from neuro_code.domain.workflows.state import WorkflowWriteResult, fingerprint, integer


@dataclass(frozen=True, slots=True)
class ApprovedWorkflowVerification:
    """Explicit user configuration assembled by bootstrap, not Workflow JSON.

    This is intent, not a permission grant. The bound tool executor must still
    apply its current capability, Permission, Workspace and Sandbox policies.
    """

    command: str
    timeout_milliseconds: int = 30_000
    output_byte_limit: int = 4096
    origin: str = "explicit_user_configuration"

    def __post_init__(self) -> None:
        validate_verification_command(self.command)
        integer(self.timeout_milliseconds)
        integer(self.output_byte_limit)
        if not 1 <= self.timeout_milliseconds <= 120_000:
            raise ValueError("VERIFY timeout must be bounded")
        if not 1 <= self.output_byte_limit <= 16_384:
            raise ValueError("VERIFY output must be bounded")
        if self.origin != "explicit_user_configuration":
            raise ValueError("VERIFY command needs explicit trusted configuration")


class WorkflowVerificationCommand(Protocol):
    """A binding-owned foreground ToolExecutor path, not a shell implementation."""

    @property
    def workspace_root(self) -> Path: ...

    async def verify_command(
        self, configuration: ApprovedWorkflowVerification, *, session_id: str
    ) -> ToolExecutionResult: ...


class WorkflowVerificationStore(Protocol):
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
    ) -> WorkflowActivityAttempt: ...

    async def reconcile_workflow_verification(
        self,
        invocation_id: str,
        *,
        parent_session_id: str,
        parent_workspace_root: Path,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt: ...

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
    ) -> WorkflowWriteResult: ...


@dataclass(frozen=True, slots=True)
class VerificationWorkspaceEvidence:
    repository_id: str
    root: str
    head: str
    content_fingerprint: str

    def __post_init__(self) -> None:
        if not self.repository_id or not self.head or not Path(self.root).is_absolute():
            raise ValueError("VERIFY needs exact repository/root/HEAD")
        fingerprint(self.content_fingerprint)


class VerificationWorkspaceWatch(Protocol):
    async def unchanged(self) -> bool: ...


class WorkflowVerificationWorkspace(Protocol):
    async def evidence(self, root: Path) -> VerificationWorkspaceEvidence: ...

    async def watch(self, root: Path) -> VerificationWorkspaceWatch: ...
