"""DW2 persistence port. No Workflow execution or Task DAG publication API."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from neuro_code.domain.workflows.definition import WorkflowDefinition
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    WorkflowChange,
    WorkflowJournalEvent,
    WorkflowRun,
    WorkflowWriteResult,
)


class WorkflowStateError(Exception):
    def __init__(self, message: str, *, kind: str = "storage") -> None:
        self.kind = kind
        super().__init__(message[:1000])


class WorkflowStateStore(Protocol):
    async def insert_workflow_definition(
        self, definition: WorkflowDefinition, *, compiler_version: int = 1
    ) -> WorkflowDefinition: ...

    async def get_workflow_definition(self, fingerprint: str) -> WorkflowDefinition | None: ...

    async def create_workflow_run(
        self,
        run_id: str,
        *,
        definition_fingerprint: str,
        parent_session_id: str,
        input_fingerprint: str,
        request_id: str,
        created_at: datetime,
        ceiling: BudgetAmounts | None = None,
    ) -> WorkflowWriteResult: ...

    async def get_workflow_run(self, run_id: str) -> WorkflowRun | None: ...

    async def claim_workflow_run(
        self,
        run_id: str,
        *,
        expected_generation: int,
        expected_owner_fence: int,
        owner_id: str,
        request_id: str,
        updated_at: datetime,
    ) -> WorkflowWriteResult: ...

    async def transition_workflow_run(
        self,
        run_id: str,
        change: WorkflowChange,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        request_id: str,
        updated_at: datetime,
    ) -> WorkflowWriteResult: ...

    async def get_workflow_journal(
        self, run_id: str, *, after_generation: int = -1, limit: int = 100
    ) -> tuple[WorkflowJournalEvent, ...]: ...
