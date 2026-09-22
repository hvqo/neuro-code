"""Canonical application boundary for bounded project memory storage."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from neuro_code.domain.memory import (
    ProjectMemory,
    ProjectMemoryCursor,
    ProjectMemorySnapshot,
)


class ProjectMemoryStore(Protocol):
    """Persist memory files under a validated, project-id-owned state path."""

    def load_snapshot(self, project_id: str) -> ProjectMemorySnapshot: ...

    def load_index(self, project_id: str) -> str: ...

    def read_memory(self, project_id: str, memory_id: str) -> ProjectMemory: ...

    def upsert_memory(self, project_id: str, memory: ProjectMemory) -> None: ...

    def delete_memory(self, project_id: str, memory_id: str) -> bool: ...

    def save_cursor(self, project_id: str, cursor: ProjectMemoryCursor) -> None: ...

    def delete_project(self, project_id: str) -> None: ...


class ProjectMemoryExtractionScheduler(Protocol):
    """Schedule one bounded extraction while retaining ownership of its task."""

    def schedule(
        self,
        session_id: str,
        project_id: str,
    ) -> bool: ...


class ProjectMemoryLifecycleCoordinator(Protocol):
    """Serialize extraction writes with project memory deletion."""

    def project_lock(self, project_id: str) -> AbstractAsyncContextManager[None]: ...


class ProjectMemoryRecallController(Protocol):
    """Application-owned, identity-scoped read-only memory recall."""

    def index_text(self, project_id: str) -> str | None: ...

    def read_text(self, project_id: str, memory_id: str) -> str: ...


class ProjectMemoryScopeProvider(Protocol):
    """Expose only the current conversation's project identity to read adapters."""

    @property
    def project_id(self) -> str | None: ...


__all__ = [
    "ProjectMemoryExtractionScheduler",
    "ProjectMemoryLifecycleCoordinator",
    "ProjectMemoryRecallController",
    "ProjectMemoryScopeProvider",
    "ProjectMemoryStore",
]
