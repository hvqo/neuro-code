"""Concrete tool registry adapter.

This module is the canonical owner of :class:`ToolRegistry` and the
``default_tool_registry`` factory.  It is pure wiring: it does not execute
tools, hold side effects, or own permissions, sandbox, or cancellation
semantics.  Tool implementations are imported lazily inside the factory so
that importing this module does not load bash, background-task, client
terminal, filesystem, plan, or skill implementations.

The former ``neuro_code.tools.registry`` facade has been removed; this module
is the only registry owner.

定义具体的工具注册表适配器. 该模块只负责连接,不执行工具、不持有副作用、权限、沙箱或取消语义.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any, Final

from neuro_code.application.ports.client_filesystem import ClientFileSystem
from neuro_code.application.ports.client_terminal import ClientTerminal
from neuro_code.application.ports.context_rollover import ContextRolloverController
from neuro_code.application.ports.git_inspection import GitInspectionApplication
from neuro_code.application.ports.lsp import LanguageServerService
from neuro_code.application.ports.session_history import SessionHistoryQueryController
from neuro_code.application.ports.terminal import InteractiveTerminalManager
from neuro_code.application.ports.tools import Tool
from neuro_code.application.ports.user_interaction import UserInteractionPort
from neuro_code.application.ports.working_set import WorkingSetController
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.tools import ToolDefinition
from neuro_code.shared.errors import ToolError

_TOOL_INTENT_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "description": (
        "One short sentence, in the user's language, explaining what this call does and why. "
        "It is shown to the user in the approval prompt and is never executed or forwarded "
        "to the program."
    ),
}


def _with_tool_intent(tool: Tool) -> ToolDefinition:
    """Add the optional intent field to one built-in side-effecting tool.

    为内置副作用工具添加可选的意图字段."""

    definition = tool.definition
    if not tool.side_effecting:
        return definition
    properties = dict(definition.input_schema.get("properties") or {})
    if "intent" in properties:
        return definition
    properties["intent"] = dict(_TOOL_INTENT_PROPERTY)
    schema = dict(definition.input_schema)
    schema["properties"] = properties
    return ToolDefinition(
        definition.name,
        definition.description,
        schema,
        definition.execution_mode,
    )


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._external_names: set[str] = set()
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if not name or name in self._tools:
            raise ToolError(f"duplicate or empty tool name: {name!r}")
        self._tools[name] = tool

    def register_external(self, tool: Tool) -> None:
        """Register a caller-owned extension and remember its ownership."""

        self.register(tool)
        self._external_names.add(tool.definition.name)

    def replace_external(self, tools: Iterable[Tool], previous_names: Collection[str]) -> None:
        """Replace a caller-owned extension set without touching built-ins.

        替换调用方拥有的扩展工具集合,不触碰内置工具.
        """

        previous = set(previous_names)
        if not previous.issubset(self._external_names):
            raise ToolError("external tool ownership is invalid")
        normalized = tuple(tools)
        names = tuple(tool.definition.name for tool in normalized)
        if len(set(names)) != len(names) or any(
            name in self._tools and name not in previous for name in names
        ):
            raise ToolError("duplicate or reserved external tool name")
        updated = dict(self._tools)
        for name in previous:
            updated.pop(name, None)
        for tool in normalized:
            updated[tool.definition.name] = tool
        self._tools = updated
        self._external_names.difference_update(previous)
        self._external_names.update(names)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Advertise the provider-facing catalog, including the intent field.

        返回面向 Provider 的工具目录,其中包含意图字段.
        """

        return tuple(
            tool.definition if name in self._external_names else _with_tool_intent(tool)
            for name, tool in self._tools.items()
        )

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)


def default_tool_registry(
    sandbox_profile: SandboxProfile = SandboxProfile.OFF,
    *,
    enable_background_tasks: bool = False,
    allowed_tool_names: Collection[str] | None = None,
    client_file_system: ClientFileSystem | None = None,
    client_terminal: ClientTerminal | None = None,
    interactive_terminals: InteractiveTerminalManager | None = None,
    user_interaction: UserInteractionPort | None = None,
    lsp_service: LanguageServerService | None = None,
    git_inspection: GitInspectionApplication | None = None,
    session_item_query: SessionHistoryQueryController | None = None,
    session_working_set: WorkingSetController | None = None,
    context_rollover: ContextRolloverController | None = None,
) -> ToolRegistry:
    from neuro_code.infrastructure.tools.background_tasks import (
        KillTaskTool,
        TaskOutputTool,
        WaitTasksTool,
    )
    from neuro_code.infrastructure.tools.bash import BashTool
    from neuro_code.infrastructure.tools.client_terminal import (
        ClientTerminalKillTool,
        ClientTerminalOutputTool,
        ClientTerminalStartTool,
        ClientTerminalTool,
        ClientTerminalWaitTool,
    )
    from neuro_code.infrastructure.tools.filesystem_discovery import (
        GlobTool,
        ListDirTool,
        ListTreeTool,
    )
    from neuro_code.infrastructure.tools.filesystem_mutation import (
        ApplyPatchTool,
        SearchReplaceTool,
    )
    from neuro_code.infrastructure.tools.filesystem_read import ReadFilesTool, ReadFileTool
    from neuro_code.infrastructure.tools.filesystem_search import (
        GrepManyTool,
        GrepTool,
    )
    from neuro_code.infrastructure.tools.git_inspection import GitInspectTool
    from neuro_code.infrastructure.tools.interaction import AskUserTool
    from neuro_code.infrastructure.tools.lsp import LspTool
    from neuro_code.infrastructure.tools.new_context import NewContextTool
    from neuro_code.infrastructure.tools.plans import UpdatePlanTool
    from neuro_code.infrastructure.tools.session_history import SessionHistoryTool
    from neuro_code.infrastructure.tools.session_working_set import SessionWorkingSetTool
    from neuro_code.infrastructure.tools.skills import SkillTool
    from neuro_code.infrastructure.tools.workspace_diff import WorkspaceDiffTool

    tools: list[Tool] = [
        ReadFileTool(),
        ReadFilesTool(),
        SkillTool(),
        UpdatePlanTool(),
        LspTool(lsp_service),
    ]
    if user_interaction is not None:
        tools.append(AskUserTool())
    if client_file_system is None:
        tools[2:2] = [
            ListDirTool(),
            ListTreeTool(),
            GlobTool(),
            GrepTool(),
            GrepManyTool(),
            WorkspaceDiffTool(),
        ]
        if git_inspection is not None:
            tools.append(GitInspectTool(git_inspection))
    if sandbox_profile.workspace_writable and client_file_system is None:
        tools.append(SearchReplaceTool())
        tools.append(ApplyPatchTool())
    elif (
        sandbox_profile.workspace_writable
        and client_file_system is not None
        and client_file_system.supports_read
        and client_file_system.supports_write
    ):
        # Delegated ACP filesystems can only express one-file text updates.
        # Do not expose the local transactional patch contract to a model that
        # cannot actually perform add/delete/move or multi-file operations.
        tools.append(SearchReplaceTool())
    tools.append(BashTool(background_enabled=enable_background_tasks))
    if client_terminal is not None and not sandbox_profile.enabled:
        tools.extend(
            (
                ClientTerminalTool(),
                ClientTerminalStartTool(),
                ClientTerminalOutputTool(),
                ClientTerminalWaitTool(),
                ClientTerminalKillTool(),
            )
        )
    if interactive_terminals is not None and client_terminal is None:
        from neuro_code.infrastructure.tools.interactive_terminal import (
            CreateTerminalTool,
            TerminalKillTool,
            TerminalOutputTool,
            TerminalResizeTool,
            TerminalWaitTool,
            TerminalWriteTool,
        )

        tools.extend(
            (
                CreateTerminalTool(),
                TerminalOutputTool(),
                TerminalWriteTool(),
                TerminalResizeTool(),
                TerminalWaitTool(),
                TerminalKillTool(),
            )
        )
    if enable_background_tasks:
        tools.extend((TaskOutputTool(), WaitTasksTool(), KillTaskTool()))
    if session_item_query is not None:
        tools.append(SessionHistoryTool(session_item_query))
    if session_working_set is not None:
        tools.append(SessionWorkingSetTool(session_working_set))
    if context_rollover is not None:
        tools.append(NewContextTool(context_rollover))
    if allowed_tool_names is not None:
        allowed = frozenset(allowed_tool_names)
        tools = [tool for tool in tools if tool.definition.name in allowed]
    return ToolRegistry(tools)


__all__ = ["ToolRegistry", "default_tool_registry"]
