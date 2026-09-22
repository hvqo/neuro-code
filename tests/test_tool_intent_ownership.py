"""Synthetic tool-intent ownership regression tests.

合成工具 intent 所有权回归测试.

Neuro Code injects a synthetic ``intent`` field only into the provider-facing
schema of its own built-in side-effecting tools.  External and caller-owned
tools are authoritative over their own schema, so a real ``intent`` parameter of
theirs must reach the tool implementation unchanged.
"""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.tools.registry import ToolRegistry
from tests.fakes import EmptyWorkspaceChangeObserver


class _RecordingTool:
    """Record the arguments that actually reach a tool implementation."""

    def __init__(
        self,
        name: str,
        *,
        side_effecting: bool,
        properties: Mapping[str, Any] | None = None,
        required: tuple[str, ...] = (),
    ) -> None:
        schema: dict[str, Any] = {"type": "object", "properties": dict(properties or {})}
        if required:
            schema["required"] = list(required)
        self.definition = ToolDefinition(
            name=name,
            description=f"{name} fixture tool.",
            input_schema=schema,
        )
        self.side_effecting = side_effecting
        self.received: list[Mapping[str, Any]] = []

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del context
        self.received.append(dict(arguments))
        return ToolResult("ok")


class ToolIntentOwnershipTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _executor(registry: ToolRegistry) -> ToolExecutor:
        return ToolExecutor(
            tools=registry,
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            approver=None,
            tool_context=ToolContext(Path("/workspace")),
            session_store=None,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            context_builder=ContextBuilder(
                reasoning_effort=ReasoningEffort.HIGH,
                interaction_mode=InteractionMode.AUTO,
                plan=None,
                instruction_provider=None,
                skill_provider=None,
            ),
        )

    @staticmethod
    async def _execute(executor: ToolExecutor, call: ToolCall) -> None:
        events: list[AgentEvent] = []

        async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
            event = AgentEvent.create(len(events) + 1, kind, data)
            events.append(event)
            return event

        await executor.execute(call, [], [], emit, "fixture-session")

    @staticmethod
    def _definition(registry: ToolRegistry, name: str) -> ToolDefinition:
        return next(item for item in registry.definitions() if item.name == name)

    async def test_builtin_side_effecting_strips_synthetic_intent(self) -> None:
        # Case A: built-in side-effecting tool.
        tool = _RecordingTool(
            "fixture_write", side_effecting=True, properties={"path": {"type": "string"}}
        )
        registry = ToolRegistry((tool,))

        self.assertTrue(registry.has_synthetic_intent("fixture_write"))
        properties = self._definition(registry, "fixture_write").input_schema["properties"]
        self.assertIn("intent", properties)
        self.assertNotIn(
            "intent", self._definition(registry, "fixture_write").input_schema.get("required", ())
        )

        await self._execute(
            self._executor(registry),
            ToolCall("call-1", "fixture_write", {"path": "note.txt", "intent": "update the note"}),
        )

        self.assertEqual(len(tool.received), 1)
        self.assertNotIn("intent", tool.received[0])
        self.assertEqual(tool.received[0]["path"], "note.txt")

    async def test_external_tool_required_real_intent_reaches_implementation(self) -> None:
        # Case B: external tool whose real schema requires `intent`.
        tool = _RecordingTool(
            "mcp_write",
            side_effecting=True,
            properties={"intent": {"type": "string", "description": "caller-owned intent"}},
            required=("intent",),
        )
        registry = ToolRegistry()
        registry.register_external(tool)

        self.assertFalse(registry.has_synthetic_intent("mcp_write"))
        definition = self._definition(registry, "mcp_write")
        self.assertEqual(definition.input_schema.get("required"), ["intent"])
        self.assertEqual(
            definition.input_schema["properties"]["intent"]["description"], "caller-owned intent"
        )

        await self._execute(
            self._executor(registry),
            ToolCall("call-2", "mcp_write", {"intent": "real external intent"}),
        )

        self.assertEqual(tool.received, [{"intent": "real external intent"}])

    async def test_external_read_only_tool_real_intent_reaches_implementation(self) -> None:
        # Case C: external read-only tool with a real `intent` parameter.
        tool = _RecordingTool(
            "mcp_read",
            side_effecting=False,
            properties={"intent": {"type": "string"}},
        )
        registry = ToolRegistry()
        registry.register_external(tool)

        self.assertFalse(registry.has_synthetic_intent("mcp_read"))
        await self._execute(
            self._executor(registry),
            ToolCall("call-3", "mcp_read", {"intent": "read-only intent"}),
        )

        self.assertEqual(tool.received, [{"intent": "read-only intent"}])

    async def test_builtin_tool_owning_intent_keeps_real_argument(self) -> None:
        # Case D: built-in tool whose canonical schema already owns `intent`.
        tool = _RecordingTool(
            "builtin_with_intent",
            side_effecting=True,
            properties={"intent": {"type": "string", "description": "real canonical intent"}},
        )
        registry = ToolRegistry((tool,))

        self.assertFalse(registry.has_synthetic_intent("builtin_with_intent"))
        self.assertEqual(
            self._definition(registry, "builtin_with_intent").input_schema["properties"]["intent"][
                "description"
            ],
            "real canonical intent",
        )

        await self._execute(
            self._executor(registry),
            ToolCall("call-4", "builtin_with_intent", {"intent": "real value"}),
        )

        self.assertEqual(tool.received, [{"intent": "real value"}])

    async def test_call_without_intent_is_unchanged(self) -> None:
        # Case E: a call without intent.
        tool = _RecordingTool(
            "fixture_write", side_effecting=True, properties={"path": {"type": "string"}}
        )
        registry = ToolRegistry((tool,))

        await self._execute(
            self._executor(registry),
            ToolCall("call-5", "fixture_write", {"path": "note.txt"}),
        )

        self.assertEqual(tool.received, [{"path": "note.txt"}])

    def test_ownership_truth_table(self) -> None:
        builtin_write = _RecordingTool("builtin_write", side_effecting=True)
        builtin_read = _RecordingTool("builtin_read", side_effecting=False)
        external_write = _RecordingTool("external_write", side_effecting=True)
        registry = ToolRegistry((builtin_write, builtin_read))
        registry.register_external(external_write)

        self.assertTrue(registry.has_synthetic_intent("builtin_write"))
        self.assertFalse(registry.has_synthetic_intent("builtin_read"))
        self.assertFalse(registry.has_synthetic_intent("external_write"))
        self.assertFalse(registry.has_synthetic_intent("missing"))


if __name__ == "__main__":
    unittest.main()
