"""Publish/read immutable result facts only from a DW3-bound terminal DAG."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from neuro_code.domain.workflows.projection import WorkflowResultProjection


class WorkflowProjectionStore(Protocol):
    async def project_workflow_result(
        self,
        expansion_id: str,
        *,
        run_id: str,
        parent_session_id: str,
        created_at: datetime,
        expected_source_fingerprint: str | None = None,
    ) -> WorkflowResultProjection: ...

    async def get_workflow_result_projection(
        self, expansion_id: str, *, run_id: str, parent_session_id: str
    ) -> WorkflowResultProjection | None: ...
