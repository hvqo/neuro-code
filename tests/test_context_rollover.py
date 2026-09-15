from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.working_set import (
    WORKING_SET_SECTION_ORDER,
    UpdateWorkingSetRequest,
    WorkingSetEntry,
    WorkingSetSection,
    WorkingSetSectionState,
    WorkingSetUpdate,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.sessions.context_rollover import (
    SessionContextRolloverApplicationService,
)
from neuro_code.application.sessions.working_set import SessionWorkingSetApplicationService
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason, ToolCall
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.tools.new_context import NewContextTool
from neuro_code.infrastructure.tools.registry import default_tool_registry
from neuro_code.shared.errors import ProviderError, ToolError
from tests.fakes import EmptyWorkspaceChangeObserver


class _ScriptedProvider:
    provider_name = "fixture-provider"
    model_name = "fixture-model"
    context_affinity = "fixture-affinity"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent | BaseException]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[ModelContext] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tools, tool_policy
        self.calls.append(context)
        for event in self._scripts.pop(0):
            if isinstance(event, BaseException):
                raise event
            yield event


class ContextRolloverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database = Path(self._temporary.name) / "sessions.db"
        self.store = SqliteSessionStore(self.database)
        await self.store.initialize()
        self.session_id = await self.store.create_session(
            self._temporary.name,
            "fixture-provider",
            "fixture-model",
            "fixture-affinity",
        )
        self.rollover = SessionContextRolloverApplicationService(self.store)
        self.working_set = SessionWorkingSetApplicationService(self.store)

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    def _runtime(
        self,
        provider: _ScriptedProvider,
        *,
        store: SqliteSessionStore | None = None,
    ) -> AgentRuntime:
        selected_store = store or self.store
        rollover = SessionContextRolloverApplicationService(selected_store)
        working_set = SessionWorkingSetApplicationService(selected_store)
        tools = default_tool_registry(
            allowed_tool_names=("new_context",),
            context_rollover=rollover,
        )
        return AgentRuntime(
            provider=provider,
            tools=tools,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(
                Path(self._temporary.name),
                session_id=self.session_id,
            ),
            session_store=selected_store,
            working_set=working_set,
            context_rollover=rollover,
            max_steps=4,
            execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
        )

    async def _seed_working_set(self) -> object:
        sections = tuple(
            WorkingSetSectionState(
                section,
                (WorkingSetEntry("preserve this durable goal"),)
                if section is WorkingSetSection.GOAL
                else (),
            )
            for section in WORKING_SET_SECTION_ORDER
        )
        return await self.working_set.update_working_set(
            UpdateWorkingSetRequest(
                self.session_id,
                WorkingSetUpdate(0, sections),
            )
        )

    async def test_success_starts_fresh_projection_and_keeps_durable_state(self) -> None:
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            Message(Role.USER, "old durable evidence that must be rehydratable"),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        before_working_set = await self._seed_working_set()
        provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-1", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("continued in a fresh context"), ModelCompleted("stop")),
            )
        )

        result = await self._runtime(provider).run(
            "finish the current durable task",
            initial_items=initial_items,
            session_id=self.session_id,
        )

        self.assertEqual(result.session_id, self.session_id)
        self.assertEqual(result.response, "continued in a fresh context")
        self.assertEqual(len(provider.calls), 2)
        first_items = provider.calls[0].items
        second_items = provider.calls[1].items
        self.assertTrue(
            any(
                isinstance(item, Message) and "old durable evidence" in item.content
                for item in first_items
            )
        )
        self.assertFalse(
            any(
                isinstance(item, Message) and "old durable evidence" in item.content
                for item in second_items
            )
        )
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.role is Role.USER
                and "finish the current durable task" in item.content
                for item in second_items
            )
        )
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.RUNTIME_CONTEXT_ROLLOVER
                and "generation 1" in item.content
                for item in second_items
            )
        )
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.WORKING_SET
                and "preserve this durable goal" in item.content
                for item in second_items
            )
        )

        self.assertEqual(await self.store.load_context_generation(self.session_id), 1)
        self.assertEqual(
            await self.store.load_working_set(self.session_id),
            before_working_set,
        )
        persisted_items = await self.store.load_session_items(self.session_id)
        self.assertTrue(
            any(
                isinstance(item, Message) and "old durable evidence" in item.content
                for item in persisted_items
            )
        )
        self.assertTrue(
            any(
                isinstance(item, Message) and item.role is Role.TOOL and item.name == "new_context"
                for item in persisted_items
            )
        )
        self.assertFalse(
            any(
                isinstance(item, Message) and item.synthetic_reason is not None
                for item in persisted_items
            )
        )

    async def test_output_limit_rejects_before_generation_write(self) -> None:
        tool = NewContextTool(self.rollover)
        with self.assertRaises(ToolError):
            await tool.execute(
                {},
                ToolContext(
                    Path(self._temporary.name),
                    session_id=self.session_id,
                    output_byte_limit=1,
                ),
            )

        self.assertEqual(await self.store.load_context_generation(self.session_id), 0)

    async def test_generation_survives_failed_turn_and_reopen(self) -> None:
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            Message(Role.USER, "pre-rollover durable fact"),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        failed_provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-1", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ProviderError("simulated interruption"),),
            )
        )

        with self.assertRaises(ProviderError):
            await self._runtime(failed_provider).run(
                "task interrupted after rollover",
                initial_items=initial_items,
                session_id=self.session_id,
            )
        self.assertEqual(await self.store.load_context_generation(self.session_id), 1)

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        recovered_items = await reopened.load_session_items(self.session_id)
        recovery_provider = _ScriptedProvider(
            ((ModelTextDelta("recovered"), ModelCompleted("stop")),)
        )
        result = await self._runtime(recovery_provider, store=reopened).run(
            "continue after recovery",
            initial_items=recovered_items,
            session_id=self.session_id,
        )

        self.assertEqual(result.session_id, self.session_id)
        self.assertEqual(result.response, "recovered")
        self.assertEqual(len(recovery_provider.calls), 1)
        self.assertFalse(
            any(
                isinstance(item, Message) and "pre-rollover durable fact" in item.content
                for item in recovery_provider.calls[0].items
            )
        )
        self.assertTrue(
            any(
                isinstance(item, Message) and "continue after recovery" in item.content
                for item in recovery_provider.calls[0].items
            )
        )
        self.assertEqual(await reopened.load_context_generation(self.session_id), 1)


if __name__ == "__main__":
    unittest.main()
