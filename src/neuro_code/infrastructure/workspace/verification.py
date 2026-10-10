"""Bounded source-workspace evidence using the existing checkpoint projection."""

from __future__ import annotations

from pathlib import Path

from neuro_code.application.ports.result_adoption import ParentWorkspaceProjectionReader
from neuro_code.application.ports.workflow_verification import VerificationWorkspaceEvidence
from neuro_code.domain.checkpoints import workspace_projection_fingerprint
from neuro_code.domain.worktree import WorktreeHandle, WorktreeId
from neuro_code.shared.async_utils import run_blocking


def _versions(paths: tuple[Path, ...]) -> tuple[tuple[int, ...] | None, ...]:
    values: list[tuple[int, ...] | None] = []
    for path in paths:
        try:
            stat = path.lstat()
            values.append(
                (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mode,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            )
        except FileNotFoundError:
            values.append(None)
    return tuple(values)


class _ProjectionWatch:
    def __init__(
        self, paths: tuple[Path, ...], versions: tuple[tuple[int, ...] | None, ...]
    ) -> None:
        self.paths, self.versions = paths, versions

    async def unchanged(self) -> bool:
        return await run_blocking(lambda: _versions(self.paths)) == self.versions


class LocalWorkflowVerificationWorkspace:
    def __init__(self, reader: ParentWorkspaceProjectionReader) -> None:
        self.reader = reader

    async def evidence(self, root: Path) -> VerificationWorkspaceEvidence:
        snapshot = await self.reader.inspect(root)
        repository = snapshot.repository
        handle = WorktreeHandle(
            WorktreeId("parent-" + repository.repository_id),
            repository,
            repository.source_worktree,
            repository.head_sha,
            None,
        )
        return VerificationWorkspaceEvidence(
            repository.repository_id,
            str(repository.source_worktree),
            repository.head_sha,
            workspace_projection_fingerprint(handle, snapshot.projection).value,
        )

    async def watch(self, root: Path) -> _ProjectionWatch:
        # Bounded checkpoint paths; metadata polling avoids repeated content hashes.
        snapshot = await self.reader.inspect(root)
        paths = tuple(root / entry.path for entry in snapshot.projection.entries)
        return _ProjectionWatch(paths, await run_blocking(lambda: _versions(paths)))
