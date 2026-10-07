"""One atomic publication seam, with no interpreter or scheduler API."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from neuro_code.domain.workflows.publication import (
    WorkflowExpansion,
    WorkflowExpansionIntent,
    WorkflowNodeExecutionIntent,
    WorkflowPublicationResult,
)


class WorkflowPublicationStore(Protocol):
    async def publish_workflow_expansion(
        self,
        intent: WorkflowExpansionIntent,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowPublicationResult: ...

    async def get_workflow_expansion(self, expansion_id: str) -> WorkflowExpansion | None: ...


class WorkflowExecutionIntentStore(Protocol):
    async def get_workflow_node_execution_intent(
        self, dag_id: str, node_id: str
    ) -> WorkflowNodeExecutionIntent | None:
        """None only for non-Workflow DAGs; missing/corrupt Workflow intent must raise."""
        ...
