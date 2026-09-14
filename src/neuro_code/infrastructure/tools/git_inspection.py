"""Model-facing bounded read-only Git inspection tool.

The tool deliberately accepts only a view selector.  The trusted workspace,
Git executable, argv, revisions, and process policies are all owned by the
injected application capability and binding context.

面向模型的有界只读 Git 检查工具.

本工具刻意只接受检查视图选择器.受信工作区、Git 可执行文件、argv、revision 与
进程策略均由注入的应用能力和 binding context 拥有.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.git_inspection import (
    GitInspectionApplication,
    GitInspectionError,
    GitInspectionView,
)
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import ToolError


class GitInspectTool(Tool):
    """Expose the application-owned inspection projection without Git writes."""

    definition = ToolDefinition(
        name="git_inspect",
        description=(
            "Inspect the current trusted workspace Git repository. Reports repository identity, "
            "branch or detached HEAD, staged/unstaged/untracked/conflicted paths, and bounded "
            "staged and unstaged textual diffs. Untracked file contents are never read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": [view.value for view in GitInspectionView],
                    "default": GitInspectionView.ALL.value,
                }
            },
            "additionalProperties": False,
        },
    )
    side_effecting = False

    def __init__(self, service: GitInspectionApplication) -> None:
        self._service = service

    @staticmethod
    def _view(arguments: Mapping[str, Any]) -> GitInspectionView:
        unsupported = set(arguments).difference({"view"})
        if unsupported:
            raise ToolError("git_inspect contains unsupported fields")
        raw = arguments.get("view", GitInspectionView.ALL.value)
        if not isinstance(raw, str):
            raise ToolError("git_inspect.view must be a string")
        try:
            return GitInspectionView(raw)
        except ValueError as error:
            raise ToolError("git_inspect.view is invalid") from error

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        view = self._view(arguments)
        if context.client_file_system is not None:
            return ToolResult(
                "git inspection is unavailable for a delegated client filesystem",
                is_error=True,
                metadata={
                    "failure_kind": "not_available",
                    "complete": False,
                    "read_only": True,
                },
            )
        try:
            result = await self._service.inspect(context.cwd, view)
        except GitInspectionError as error:
            return ToolResult(
                f"git inspection failed: {error}",
                is_error=True,
                metadata={
                    "failure_kind": error.kind.value,
                    "complete": False,
                    "read_only": True,
                },
            )
        content = json.dumps(result.to_dict(), ensure_ascii=True, sort_keys=True)
        if len(content.encode("utf-8", "surrogateescape")) > context.output_byte_limit:
            return ToolResult(
                "git inspection result exceeded the tool output bound",
                is_error=True,
                metadata={
                    "failure_kind": "output_limit",
                    "complete": False,
                    "read_only": True,
                },
            )
        return ToolResult(
            content,
            metadata={
                "read_only": True,
                "complete": result.completeness.value == "complete",
                "completeness": result.completeness.value,
                "view": view.value,
            },
        )


__all__ = ["GitInspectTool"]
