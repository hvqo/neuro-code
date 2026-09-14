"""Application service for the bounded read-only Git inspection capability.

The service accepts only a trusted workspace selected by bootstrap or a
binding.  It does not accept Git revisions, paths, or argv from the model and
does not own any durable state.

有界只读 Git 检查能力的应用服务.

本服务只接受由 bootstrap 或 binding 选择的受信工作区,不接受模型提供的 Git
revision、路径或 argv,也不拥有任何持久化状态.
"""

from __future__ import annotations

from pathlib import Path

from neuro_code.application.ports.git_inspection import (
    GitInspectionError,
    GitInspectionFailureKind,
    GitInspectionPort,
    GitInspectionResult,
    GitInspectionView,
)
from neuro_code.shared.async_utils import run_blocking


class GitInspectionService:
    """Validate the trusted workspace boundary and delegate one inspection."""

    def __init__(self, inspection: GitInspectionPort) -> None:
        self._inspection = inspection

    async def inspect(
        self,
        workspace: Path,
        view: GitInspectionView = GitInspectionView.ALL,
        /,
    ) -> GitInspectionResult:
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            raise GitInspectionError(
                "Git inspection requires an absolute trusted workspace",
                kind=GitInspectionFailureKind.PATH_UNSAFE,
            )
        if not isinstance(view, GitInspectionView):
            raise GitInspectionError(
                "Git inspection view is invalid",
                kind=GitInspectionFailureKind.PROTOCOL,
            )
        try:
            canonical = await run_blocking(lambda: workspace.expanduser().resolve(strict=True))
            is_directory = await run_blocking(canonical.is_dir)
        except (OSError, RuntimeError) as error:
            raise GitInspectionError(
                "trusted Git workspace is unavailable",
                kind=GitInspectionFailureKind.PATH_UNSAFE,
            ) from error
        if not is_directory:
            raise GitInspectionError(
                "trusted Git workspace is not a directory",
                kind=GitInspectionFailureKind.PATH_UNSAFE,
            )
        return await self._inspection.inspect(canonical, view)


__all__ = ["GitInspectionService"]
