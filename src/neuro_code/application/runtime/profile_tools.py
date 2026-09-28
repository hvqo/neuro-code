"""Model and execution tool view restricted by one immutable Agent binding."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from neuro_code.application.agents.binding import tool_required_capabilities
from neuro_code.application.ports.tools import Tool, ToolCollection, ToolContext
from neuro_code.domain.agents.profile import AgentCapability
from neuro_code.domain.tools import ToolDefinition, ToolResult

_STABLE_CONTEXT_TOOLS = frozenset({"read_project_memory"})


class _CapabilityBlockedTool:
    """Keep a stable read-only schema while failing closed without its scope."""

    __slots__ = ("_delegate", "definition", "side_effecting")

    def __init__(self, delegate: Tool) -> None:
        self._delegate = delegate
        self.definition = delegate.definition
        self.side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        del arguments, context
        return ToolResult(
            "Requested capability is unavailable in this Agent binding.",
            is_error=True,
        )


class ProfileBoundToolCollection:
    """Keep schema exposure and tool lookup on the same capability decision."""

    __slots__ = ("_allowed_names", "_delegate", "_effective_capabilities")

    def __init__(
        self,
        delegate: ToolCollection,
        allowed_names: Collection[str],
        *,
        effective_capabilities: Collection[AgentCapability],
    ) -> None:
        self._delegate = delegate
        self._allowed_names = frozenset(allowed_names)
        self._effective_capabilities = frozenset(effective_capabilities)

    def get(self, name: str) -> Tool | None:
        tool = self._delegate.get(name)
        if name in self._allowed_names:
            return tool
        if name in _STABLE_CONTEXT_TOOLS and tool is not None:
            return _CapabilityBlockedTool(tool)
        return None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition in self._delegate.definitions()
            if definition.name in self._allowed_names or definition.name in _STABLE_CONTEXT_TOOLS
        )

    def names(self) -> tuple[str, ...]:
        """Return the deterministic names in the filtered model-visible catalog."""

        return tuple(definition.name for definition in self.definitions())

    def has_synthetic_intent(self, name: str) -> bool:
        return name in self._allowed_names and self._delegate.has_synthetic_intent(name)

    def replace_external(self, tools: Collection[Tool], previous_names: Collection[str]) -> None:
        replace_external = getattr(self._delegate, "replace_external", None)
        if not callable(replace_external):
            raise TypeError("bound tool collection cannot replace external tools")
        normalized = tuple(tools)
        new_names = {tool.definition.name for tool in normalized}
        extension_requirements = (
            {
                AgentCapability.EXTENSION_INVOKE,
                *(
                    capability
                    for name in new_names
                    for capability in tool_required_capabilities(name)
                ),
            }
            if new_names
            else set()
        )
        if not extension_requirements.issubset(self._effective_capabilities):
            raise TypeError("Agent profile does not allow external tools for these capabilities")
        previous = frozenset(previous_names)
        replace_external(normalized, previous_names)
        self._allowed_names = (self._allowed_names - previous) | frozenset(new_names)


__all__ = ["ProfileBoundToolCollection"]
