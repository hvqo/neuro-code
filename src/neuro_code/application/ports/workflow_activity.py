"""Narrow durable Activity port; execution adapters are intentionally absent."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from neuro_code.domain.workflows.activity import (
    WorkflowActivityAttempt,
    WorkflowActivityInvocation,
    WorkflowActivityResult,
)
from neuro_code.domain.workflows.state import BudgetAmounts, WorkflowWriteResult


class WorkflowActivityStore(Protocol):
    async def consume_workflow_activity_failure(
        self,
        invocation_id: str,
        result_fingerprint: str,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult: ...

    async def publish_workflow_activity(
        self,
        invocation: WorkflowActivityInvocation,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult: ...

    async def get_workflow_activity(self, invocation_id: str) -> WorkflowActivityAttempt | None: ...

    async def claim_workflow_activity(
        self,
        invocation_id: str,
        *,
        expected_revision: int,
        owner_id: str,
        reserved: BudgetAmounts,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt: ...

    async def start_workflow_activity(
        self,
        invocation_id: str,
        *,
        expected_revision: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt: ...

    async def finish_workflow_activity(
        self,
        result: WorkflowActivityResult,
        *,
        expected_revision: int,
        owner_id: str,
        owner_fence: int,
    ) -> WorkflowActivityAttempt: ...
