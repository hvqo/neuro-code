from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from neuro_code.application.memory.compaction import (
    CompactionContextUsage,
    ContextCompactionPlanner,
    ContextCompactionPolicy,
    ContextSummaryRequest,
    ProviderContextWindow,
    build_durable_compaction_item,
)
from neuro_code.application.memory.compaction_runtime import (
    ContextCompactionRuntimeBoundary,
    ContextCompactionRuntimeGate,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeResult,
)
from neuro_code.application.memory.compaction_service import ContextCompactionApplicationService
from neuro_code.application.memory.compaction_trigger import ContextCompactionTriggerService
from neuro_code.application.memory.project_scope import ProjectMemoryScope
from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.application.ports.tools import Tool, ToolCollection, ToolContext
from neuro_code.application.ports.working_set import (
    WORKING_SET_SECTION_ORDER,
    UpdateWorkingSetRequest,
    WorkingSetEntry,
    WorkingSetSection,
    WorkingSetSectionState,
    WorkingSetUpdate,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.agent_loop import AgentRunResult
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.sessions.context_rollover import (
    SessionContextRolloverApplicationService,
)
from neuro_code.application.sessions.recovery import TurnRecoveryService
from neuro_code.application.sessions.working_set import SessionWorkingSetApplicationService
from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.conversation.context import ModelContext, estimate_context_tokens
from neuro_code.domain.conversation.events import (
    AgentEventKind,
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import (
    ContentPart,
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    SyntheticReason,
    ToolCall,
)
from neuro_code.domain.execution import (
    TurnInput,
    TurnRecoveryAttempt,
    TurnRecoveryStatus,
    TurnSource,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.infrastructure.providers.failover import FailoverModelProvider, ProviderCandidate
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.infrastructure.tools.new_context import NewContextTool
from neuro_code.infrastructure.tools.registry import default_tool_registry
from neuro_code.shared.errors import ProviderError, ToolError
from tests.fakes import EmptyWorkspaceChangeObserver


class _ScriptedProvider:
    provider_name = "fixture-provider"
    model_name = "fixture-model"
    context_affinity = "fixture-affinity"

    def __init__(
        self,
        scripts: Sequence[Sequence[ModelEvent | BaseException]],
        *,
        provider_name: str = "fixture-provider",
        model_name: str = "fixture-model",
        context_affinity: str | None = "fixture-affinity",
    ) -> None:
        self._scripts = list(scripts)
        self.calls: list[ModelContext] = []
        self.provider_name = provider_name
        self.model_name = model_name
        self.context_affinity = context_affinity

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


class _LargeResultTool:
    definition = ToolDefinition(
        name="inspect",
        description="Return a large current-turn fixture result.",
        input_schema={"type": "object", "additionalProperties": False},
    )
    side_effecting = False

    def __init__(self, content: str) -> None:
        self._content = content

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult(self._content)


class _ToolCollection:
    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def has_synthetic_intent(self, name: str) -> bool:
        """Mirror the registry rule for this built-in-only test collection."""

        tool = self._tools.get(name)
        if tool is None:
            return False
        properties = tool.definition.input_schema.get("properties") or {}
        return getattr(tool, "side_effecting", False) and "intent" not in properties


class _RecordingCompactionGate(ContextCompactionRuntimeGate):
    __slots__ = ("automatic_usage_contexts", "trigger_count")

    def __init__(self, service: ContextCompactionTriggerService) -> None:
        super().__init__(service)
        self.automatic_usage_contexts: list[ModelContext] = []
        self.trigger_count = 0

    def build_automatic_request(
        self,
        *,
        source_context: ModelContext,
        usage_context: ModelContext,
        boundary: ContextCompactionRuntimeBoundary,
        provider_window: ProviderContextWindow | None,
        protected_item_count: int = 0,
        session_id: str | None = None,
        compaction_id: str | None = None,
        created_at: datetime | None = None,
        usage_override: CompactionContextUsage | None = None,
        token_estimator: Callable[[Sequence[SessionItem]], int] = estimate_context_tokens,
    ) -> ContextCompactionRuntimeRequest:
        self.automatic_usage_contexts.append(usage_context)
        return super().build_automatic_request(
            source_context=source_context,
            usage_context=usage_context,
            boundary=boundary,
            provider_window=provider_window,
            protected_item_count=protected_item_count,
            session_id=session_id,
            compaction_id=compaction_id,
            created_at=created_at,
            usage_override=usage_override,
            token_estimator=token_estimator,
        )

    async def trigger(
        self,
        request: ContextCompactionRuntimeRequest,
    ) -> ContextCompactionRuntimeResult:
        self.trigger_count += 1
        return await super().trigger(request)


class _PendingBackgroundTaskManager:
    def __init__(self, snapshot: BackgroundTaskSnapshot) -> None:
        self._snapshot = snapshot
        self.reported_task_ids: tuple[str, ...] = ()

    async def pending_completions(self) -> tuple[BackgroundTaskSnapshot, ...]:
        if self._snapshot.task_id in self.reported_task_ids:
            return ()
        return (self._snapshot,)

    async def mark_completions_reported(self, task_ids: tuple[str, ...]) -> None:
        self.reported_task_ids = task_ids


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
        provider: ModelProvider,
        *,
        tools: ToolCollection | None = None,
        store: SqliteSessionStore | None = None,
        compaction_runtime_gate: ContextCompactionRuntimeGate | None = None,
        provider_context_window: ProviderContextWindow | None = None,
        provider_max_output_tokens: int | None = None,
        background_tasks: Any | None = None,
        project_memory_scope: ProjectMemoryScope | None = None,
        project_memory_index_provider: Callable[[str], str | None] | None = None,
    ) -> AgentRuntime:
        selected_store = store or self.store
        rollover = SessionContextRolloverApplicationService(selected_store)
        working_set = SessionWorkingSetApplicationService(selected_store)
        selected_tools = tools or default_tool_registry(
            allowed_tool_names=("new_context",),
            context_rollover=rollover,
        )
        return AgentRuntime(
            provider=provider,
            tools=selected_tools,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=ToolContext(
                Path(self._temporary.name),
                session_id=self.session_id,
                background_tasks=background_tasks,
            ),
            session_store=selected_store,
            working_set=working_set,
            context_rollover=rollover,
            max_steps=4,
            execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
            compaction_runtime_gate=compaction_runtime_gate,
            provider_context_window=provider_context_window,
            provider_max_output_tokens=provider_max_output_tokens,
            project_memory_scope=project_memory_scope,
            project_memory_index_provider=project_memory_index_provider,
        )

    @staticmethod
    def _responses_sse(response: dict[str, object]) -> str:
        return (
            "data: "
            + json.dumps(
                {"type": "response.completed", "response": response},
                ensure_ascii=False,
            )
            + "\n\ndata: [DONE]\n\n"
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

    async def test_committed_rollover_refreshes_project_memory_snapshot(self) -> None:
        project_id = str(uuid.uuid4())
        index = ["Project Memory index at generation zero"]

        class _UpdatingProvider(_ScriptedProvider):
            async def stream(
                self,
                context: ModelContext,
                tools: tuple[ToolDefinition, ...],
                *,
                tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
            ) -> AsyncIterator[ModelEvent]:
                async for event in super().stream(context, tools, tool_policy=tool_policy):
                    yield event
                if len(self.calls) == 1:
                    index[0] = "Project Memory index after committed rollover"

        provider = _UpdatingProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-memory", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("continued with the refreshed snapshot"), ModelCompleted("stop")),
            )
        )
        runtime = self._runtime(
            provider,
            project_memory_scope=ProjectMemoryScope(project_id),
            project_memory_index_provider=lambda identity: index[0],
        )

        result = await runtime.run(
            "start from a fresh context",
            initial_items=(Message(Role.SYSTEM, "fixture system"),),
            session_id=self.session_id,
        )

        self.assertEqual(result.response, "continued with the refreshed snapshot")
        self.assertEqual(len(provider.calls), 2)
        memory_contents = [
            next(
                item.content
                for item in context.messages
                if item.synthetic_reason is SyntheticReason.PROJECT_MEMORY_INDEX
            )
            for context in provider.calls
        ]
        self.assertEqual(memory_contents[0], "Project Memory index at generation zero")
        self.assertEqual(memory_contents[1], "Project Memory index after committed rollover")

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

    async def test_schema_32_migration_backfills_generation_boundary(self) -> None:
        legacy_database = Path(self._temporary.name) / "legacy-sessions.db"
        legacy_items = [
            Message(Role.SYSTEM, "legacy system").to_dict(),
            Message(Role.USER, "legacy durable item").to_dict(),
        ]
        connection = sqlite3.connect(legacy_database)
        try:
            connection.executescript(
                """
                CREATE TABLE schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_meta(singleton, version) VALUES (1, 32);
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    context_affinity TEXT,
                    sandbox_profile TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    context_generation INTEGER NOT NULL DEFAULT 0
                        CHECK (context_generation >= 0)
                );
                CREATE TABLE events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence),
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                """,
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    id, cwd, provider, model, messages_json, context_generation
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-session",
                    self._temporary.name,
                    "fixture-provider",
                    "fixture-model",
                    json.dumps(legacy_items),
                    1,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = SqliteSessionStore(legacy_database)
        await migrated.initialize()

        self.assertEqual(
            await migrated.load_context_generation_state("legacy-session"),
            (1, 2),
        )
        connection = sqlite3.connect(legacy_database)
        try:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            self.assertIn("context_generation_start_index", columns)
            self.assertIn("context_generation_pending_turn_id", columns)
            self.assertIn("context_generation_pending_item_boundary", columns)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_meta WHERE singleton = 1"
                ).fetchone(),
                (SCHEMA_VERSION,),
            )
        finally:
            connection.close()

    async def test_crash_after_marker_reopens_safe_prefix_until_attempt_is_abandoned(self) -> None:
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            Message(Role.USER, "generation zero history"),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        attempt = TurnRecoveryAttempt.create(
            turn_id="rollover-crashed-turn",
            session_id=self.session_id,
            input=TurnInput(
                "continue after a crash",
                (ContentPart.from_text("continue after a crash"),),
            ),
            accepted_at=datetime.now(UTC),
        )
        await self.store.start_turn_attempt(attempt)
        await self.store.advance_context_generation(
            self.session_id,
            item_boundary=5,
            turn_id=attempt.turn_id,
        )

        self.assertEqual(
            await self.store.load_context_generation_state(self.session_id),
            (1, 2),
        )
        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        self.assertEqual(
            await reopened.load_context_generation_state(self.session_id),
            (1, 2),
        )

        resolved = await TurnRecoveryService(reopened).abandon(
            self.session_id,
            attempt.turn_id,
        )
        self.assertIs(resolved.status, TurnRecoveryStatus.ABANDONED)
        self.assertEqual(
            await reopened.load_context_generation_state(self.session_id),
            (1, 2),
        )
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT context_generation_pending_turn_id,
                           context_generation_pending_item_boundary
                    FROM sessions WHERE id = ?
                    """,
                    (self.session_id,),
                ).fetchone(),
                (None, None),
            )
        finally:
            connection.close()

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

    async def test_repeated_rollovers_keep_one_current_prompt_and_advance_generation(self) -> None:
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            Message(Role.USER, "old durable evidence"),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-1", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (
                    ModelToolCall(ToolCall("roll-2", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("twice fresh"), ModelCompleted("stop")),
            )
        )

        result = await self._runtime(provider).run(
            "the one current prompt",
            initial_items=initial_items,
            session_id=self.session_id,
        )

        self.assertEqual(result.response, "twice fresh")
        self.assertEqual(await self.store.load_context_generation(self.session_id), 2)
        self.assertEqual(len(provider.calls), 3)
        for call in provider.calls[1:]:
            self.assertEqual(
                sum(
                    isinstance(item, Message)
                    and item.role is Role.USER
                    and item.content == "the one current prompt"
                    for item in call.items
                ),
                1,
            )
            self.assertFalse(
                any(
                    isinstance(item, Message) and item.content == "old durable evidence"
                    for item in call.items
                )
            )
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.RUNTIME_CONTEXT_ROLLOVER
                and "generation 2" in item.content
                for item in provider.calls[2].items
            )
        )

    async def test_automatic_rollover_preserves_working_set_and_new_native_state(self) -> None:
        old_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "generation-zero-reasoning",
                "summary": [{"type": "summary_text", "text": "old"}],
                "encrypted_content": "generation-zero-native-state",
            },
        )
        new_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "generation-one-reasoning",
                "summary": [{"type": "summary_text", "text": "new"}],
                "encrypted_content": "generation-one-native-state",
            },
        )
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            *(Message(Role.USER, f"old history-{index}: " + "x" * 1_500) for index in range(8)),
            old_native,
        )
        await self.store.save_session_items(self.session_id, initial_items)
        before_working_set = await self._seed_working_set()
        window = ProviderContextWindow(
            "fixture-provider",
            "fixture-model",
            1_500,
            "fixture-affinity",
        )
        provider = _ScriptedProvider(
            (
                (ModelTextDelta("automatic summary"), ModelCompleted("stop")),
                (
                    ModelTextDelta("automatic fresh response"),
                    ModelCompleted("stop", context_items=(new_native,)),
                ),
            )
        )

        def compaction_gate(
            store: SqliteSessionStore,
            provider: _ScriptedProvider,
        ) -> ContextCompactionRuntimeGate:
            return ContextCompactionRuntimeGate(
                ContextCompactionTriggerService(
                    ContextCompactionApplicationService(store, provider),
                    planner=ContextCompactionPlanner(
                        ContextCompactionPolicy(minimum_recent_items=6, max_summary_tokens=64)
                    ),
                )
            )

        first = await self._runtime(
            provider,
            compaction_runtime_gate=compaction_gate(self.store, provider),
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "automatic fresh request",
            initial_items=initial_items,
            source_provider=window.provider_name,
            source_model=window.model_name,
            source_context_affinity=window.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(first.response, "automatic fresh response")
        self.assertEqual(await self.store.load_context_generation(self.session_id), 1)
        self.assertEqual(len(provider.calls), 2)
        fresh_items = provider.calls[1].items
        self.assertNotIn(old_native, fresh_items)
        self.assertFalse(
            any(
                isinstance(item, Message) and "old history-" in item.content for item in fresh_items
            )
        )
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.WORKING_SET
                and "preserve this durable goal" in item.content
                for item in fresh_items
            )
        )
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.RUNTIME_CONTEXT_ROLLOVER
                and "generation 1" in item.content
                for item in fresh_items
            )
        )
        self.assertEqual(
            sum(
                isinstance(item, Message)
                and item.role is Role.USER
                and item.content == "automatic fresh request"
                for item in fresh_items
            ),
            1,
        )

        stored_items = await self.store.load_session_items(self.session_id)
        self.assertIn(old_native, stored_items)
        self.assertIn(new_native, stored_items)
        self.assertFalse(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.COMPACTION_SUMMARY
                for item in stored_items
            )
        )
        self.assertEqual(await self.store.load_working_set(self.session_id), before_working_set)

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        reopened_items = await reopened.load_session_items(self.session_id)
        reopened_summary = await reopened.get_session(self.session_id)
        next_provider = _ScriptedProvider(
            ((ModelTextDelta("reopened response"), ModelCompleted("stop")),)
        )
        second = await self._runtime(
            next_provider,
            store=reopened,
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "reopen after automatic rollover",
            initial_items=reopened_items,
            source_provider=reopened_summary.provider,
            source_model=reopened_summary.model,
            source_context_affinity=reopened_summary.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(second.response, "reopened response")
        self.assertEqual(len(next_provider.calls), 1)
        self.assertIn(new_native, next_provider.calls[0].items)
        self.assertNotIn(old_native, next_provider.calls[0].items)
        self.assertFalse(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.COMPACTION_SUMMARY
                for item in next_provider.calls[0].items
            )
        )
        self.assertEqual(
            await reopened.load_context_generation_state(self.session_id),
            (1, len(initial_items) + 1),
        )

    async def test_automatic_rollover_cancellation_reopens_at_the_committed_boundary(self) -> None:
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            *(Message(Role.USER, f"old history-{index}: " + "x" * 1_500) for index in range(8)),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        window = ProviderContextWindow(
            "fixture-provider",
            "fixture-model",
            1_500,
            "fixture-affinity",
        )
        provider = _ScriptedProvider(
            ((ModelTextDelta("automatic summary"), ModelCompleted("stop")),)
        )
        gate = ContextCompactionRuntimeGate(
            ContextCompactionTriggerService(
                ContextCompactionApplicationService(self.store, provider),
                planner=ContextCompactionPlanner(
                    ContextCompactionPolicy(minimum_recent_items=6, max_summary_tokens=64)
                ),
            )
        )
        runtime = self._runtime(
            provider,
            compaction_runtime_gate=gate,
            provider_context_window=window,
            provider_max_output_tokens=64,
        )

        async def cancel_after_rollover(event: object) -> None:
            if getattr(event, "kind", None) is AgentEventKind.CONTEXT_PREFLIGHT and getattr(
                event, "data", {}
            ).get("automatic_rollover_succeeded"):
                task = asyncio.current_task()
                assert task is not None
                task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await runtime.run(
                "cancel after automatic rollover",
                initial_items=initial_items,
                source_provider=window.provider_name,
                source_model=window.model_name,
                source_context_affinity=window.context_affinity,
                session_id=self.session_id,
                sink=cancel_after_rollover,
            )

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(
            await self.store.load_context_generation_state(self.session_id),
            (1, len(initial_items) + 1),
        )
        cancelled_items = await self.store.load_session_items(self.session_id)
        self.assertIn(Message(Role.USER, "cancel after automatic rollover"), cancelled_items)
        self.assertFalse(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.RUNTIME_CONTEXT_ROLLOVER
                for item in cancelled_items
            )
        )

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        reopened_items = await reopened.load_session_items(self.session_id)
        reopened_summary = await reopened.get_session(self.session_id)
        next_provider = _ScriptedProvider(
            ((ModelTextDelta("recovered response"), ModelCompleted("stop")),)
        )
        recovered = await self._runtime(
            next_provider,
            store=reopened,
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "recovered after automatic rollover cancellation",
            initial_items=reopened_items,
            source_provider=reopened_summary.provider,
            source_model=reopened_summary.model,
            source_context_affinity=reopened_summary.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(recovered.response, "recovered response")
        self.assertEqual(len(next_provider.calls), 1)
        self.assertFalse(
            any(
                isinstance(item, Message) and "old history-" in item.content
                for item in next_provider.calls[0].items
            )
        )
        self.assertEqual(
            sum(
                isinstance(item, Message)
                and item.role is Role.USER
                and item.content == "recovered after automatic rollover cancellation"
                for item in next_provider.calls[0].items
            ),
            1,
        )

    async def test_automatic_rollover_failover_rebinds_native_origin_for_reopen(self) -> None:
        old_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "old-primary-reasoning",
                "summary": [{"type": "summary_text", "text": "old"}],
                "encrypted_content": "old-primary-state",
            },
        )
        new_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "new-fallback-reasoning",
                "summary": [{"type": "summary_text", "text": "new"}],
                "encrypted_content": "new-fallback-state",
            },
        )
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            *(Message(Role.USER, f"old history-{index}: " + "x" * 1_500) for index in range(8)),
            old_native,
        )
        await self.store.save_session_items(self.session_id, initial_items)
        await self.store.update_session_provider(
            self.session_id,
            "primary",
            "primary-model",
            "primary-affinity",
        )
        primary_window = ProviderContextWindow(
            "primary",
            "primary-model",
            1_500,
            "primary-affinity",
        )
        fallback_window = ProviderContextWindow(
            "fallback",
            "fallback-model",
            1_500,
            "fallback-affinity",
        )
        primary = _ScriptedProvider(
            (
                (ModelTextDelta("automatic summary"), ModelCompleted("stop")),
                (ProviderError("primary fresh request unavailable"),),
            ),
            provider_name="primary",
            model_name="primary-model",
            context_affinity="primary-affinity",
        )
        fallback = _ScriptedProvider(
            (
                (
                    ModelTextDelta("fallback fresh response"),
                    ModelCompleted("stop", context_items=(new_native,)),
                ),
                (ModelTextDelta("fallback reopened response"), ModelCompleted("stop")),
            ),
            provider_name="fallback",
            model_name="fallback-model",
            context_affinity="fallback-affinity",
        )
        provider = FailoverModelProvider(
            (
                ProviderCandidate(
                    "primary",
                    "primary-model",
                    "primary-affinity",
                    lambda: primary,
                    context_window_tokens=1_500,
                ),
                ProviderCandidate(
                    "fallback",
                    "fallback-model",
                    "fallback-affinity",
                    lambda: fallback,
                    context_window_tokens=1_500,
                ),
            )
        )
        gate = ContextCompactionRuntimeGate(
            ContextCompactionTriggerService(
                ContextCompactionApplicationService(self.store, provider),
                planner=ContextCompactionPlanner(
                    ContextCompactionPolicy(minimum_recent_items=6, max_summary_tokens=64)
                ),
            )
        )

        first = await self._runtime(
            provider,
            compaction_runtime_gate=gate,
            provider_context_window=primary_window,
            provider_max_output_tokens=64,
        ).run(
            "automatic failover request",
            initial_items=initial_items,
            source_provider=primary_window.provider_name,
            source_model=primary_window.model_name,
            source_context_affinity=primary_window.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(first.response, "fallback fresh response")
        self.assertNotIn(old_native, fallback.calls[0].items)
        self.assertEqual(
            (await self.store.get_session(self.session_id)).provider,
            "fallback",
        )
        self.assertEqual(
            (await self.store.get_session(self.session_id)).context_affinity,
            "fallback-affinity",
        )

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        reopened_items = await reopened.load_session_items(self.session_id)
        reopened_summary = await reopened.get_session(self.session_id)
        second = await self._runtime(
            provider,
            store=reopened,
            provider_context_window=fallback_window,
            provider_max_output_tokens=64,
        ).run(
            "reopen after automatic failover",
            initial_items=reopened_items,
            source_provider=reopened_summary.provider,
            source_model=reopened_summary.model,
            source_context_affinity=reopened_summary.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(second.response, "fallback reopened response")
        self.assertIn(new_native, fallback.calls[1].items)
        self.assertNotIn(old_native, fallback.calls[1].items)
        self.assertEqual(fallback.calls[1].source_provider, "fallback")
        self.assertEqual(fallback.calls[1].source_model, "fallback-model")
        self.assertEqual(fallback.calls[1].source_context_affinity, "fallback-affinity")

    async def test_automatic_rollover_keeps_uncommitted_tool_context_in_place(self) -> None:
        old_history = tuple(
            Message(Role.USER, f"old durable history-{index}: " + "x" * 200) for index in range(8)
        )
        initial_items = (Message(Role.SYSTEM, "fixture system"), *old_history)
        await self.store.save_session_items(self.session_id, initial_items)
        current_turn_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "current-turn-native-state",
                "summary": [{"type": "summary_text", "text": "current turn"}],
                "encrypted_content": "current-turn-native-state",
            },
        )
        tool = _LargeResultTool("current-turn-tool-result " + "z" * 16_000)
        window = ProviderContextWindow(
            "fixture-provider",
            "fixture-model",
            4_000,
            "fixture-affinity",
        )
        provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                    ModelCompleted("tool_calls", context_items=(current_turn_native,)),
                ),
                (ModelCompleted("stop", response_text="bounded summary"),),
            )
        )
        gate = ContextCompactionRuntimeGate(
            ContextCompactionTriggerService(
                ContextCompactionApplicationService(self.store, provider),
                planner=ContextCompactionPlanner(
                    ContextCompactionPolicy(
                        minimum_recent_items=4,
                        max_summary_tokens=64,
                    )
                ),
            )
        )

        result = await self._runtime(
            provider,
            tools=_ToolCollection((tool,)),
            compaction_runtime_gate=gate,
            provider_context_window=window,
            provider_max_output_tokens=128,
        ).run(
            "current turn with a tool result",
            initial_items=initial_items,
            source_provider=window.provider_name,
            source_model=window.model_name,
            source_context_affinity=window.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(await self.store.load_context_generation(self.session_id), 0)
        self.assertEqual(
            sum(event.kind is AgentEventKind.MODEL_REQUEST_STARTED for event in result.events),
            1,
        )
        self.assertEqual(len(provider.calls), 2)
        self.assertIn(current_turn_native, result.items)
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.role is Role.TOOL
                and "current-turn-tool-result" in item.content
                for item in result.items
            )
        )
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(preflights[-1].data["status"], "blocked")
        self.assertTrue(preflights[-1].data["compaction_attempted"])
        self.assertFalse(preflights[-1].data["automatic_rollover_eligible"])
        self.assertFalse(preflights[-1].data["automatic_rollover_attempted"])

    async def test_rollover_boundary_accumulates_across_reopened_turns(self) -> None:
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            Message(Role.USER, "generation zero history"),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        before_working_set = await self._seed_working_set()
        generation_one_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "generation-one-reasoning",
                "summary": [{"type": "summary_text", "text": "generation one"}],
                "encrypted_content": "generation-one-native-state",
            },
        )
        turn_a_provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-1", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (
                    ModelTextDelta("generation one turn A"),
                    ModelCompleted("stop", context_items=(generation_one_native,)),
                ),
            )
        )
        await self._runtime(turn_a_provider).run(
            "generation one turn A",
            initial_items=initial_items,
            session_id=self.session_id,
        )

        async def run_reopened_turn(
            store: SqliteSessionStore,
            prompt: str,
            provider: _ScriptedProvider,
        ) -> tuple[AgentRunResult, list[SessionItem]]:
            items = await store.load_session_items(self.session_id)
            summary = await store.get_session(self.session_id)
            result = await self._runtime(provider, store=store).run(
                prompt,
                initial_items=items,
                source_provider=summary.provider,
                source_model=summary.model,
                source_context_affinity=summary.context_affinity,
                session_id=self.session_id,
            )
            return result, list(provider.calls[0].items)

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        turn_b_provider = _ScriptedProvider(
            ((ModelTextDelta("generation one turn B"), ModelCompleted("stop")),)
        )
        turn_b_result, turn_b_items = await run_reopened_turn(
            reopened,
            "generation one turn B",
            turn_b_provider,
        )
        self.assertEqual(turn_b_result.response, "generation one turn B")
        self.assertIn(Message(Role.ASSISTANT, "generation one turn A"), turn_b_items)
        self.assertIn(generation_one_native, turn_b_items)
        self.assertNotIn(
            Message(Role.USER, "generation zero history"),
            turn_b_items,
        )
        self.assertEqual(
            sum(
                isinstance(item, Message)
                and item.role is Role.USER
                and item.content == "generation one turn B"
                for item in turn_b_items
            ),
            1,
        )
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.WORKING_SET
                and "preserve this durable goal" in item.content
                for item in turn_b_items
            )
        )

        turn_c_provider = _ScriptedProvider(
            ((ModelTextDelta("generation one turn C"), ModelCompleted("stop")),)
        )
        turn_c_result, turn_c_items = await run_reopened_turn(
            reopened,
            "generation one turn C",
            turn_c_provider,
        )
        self.assertEqual(turn_c_result.response, "generation one turn C")
        self.assertIn(Message(Role.ASSISTANT, "generation one turn A"), turn_c_items)
        self.assertIn(Message(Role.ASSISTANT, "generation one turn B"), turn_c_items)
        self.assertIn(generation_one_native, turn_c_items)
        self.assertNotIn(Message(Role.USER, "generation zero history"), turn_c_items)

        generation_two_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "generation-two-reasoning",
                "summary": [{"type": "summary_text", "text": "generation two"}],
                "encrypted_content": "generation-two-native-state",
            },
        )
        turn_d_provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-2", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (
                    ModelTextDelta("generation two turn D"),
                    ModelCompleted("stop", context_items=(generation_two_native,)),
                ),
            )
        )
        reopened_items = await reopened.load_session_items(self.session_id)
        reopened_summary = await reopened.get_session(self.session_id)
        turn_d_result = await self._runtime(turn_d_provider, store=reopened).run(
            "generation two turn D",
            initial_items=reopened_items,
            source_provider=reopened_summary.provider,
            source_model=reopened_summary.model,
            source_context_affinity=reopened_summary.context_affinity,
            session_id=self.session_id,
        )
        self.assertEqual(turn_d_result.response, "generation two turn D")
        self.assertNotIn(
            Message(Role.ASSISTANT, "generation one turn A"), turn_d_provider.calls[1].items
        )
        self.assertNotIn(
            Message(Role.ASSISTANT, "generation one turn B"), turn_d_provider.calls[1].items
        )
        self.assertNotIn(
            Message(Role.ASSISTANT, "generation one turn C"), turn_d_provider.calls[1].items
        )
        self.assertNotIn(generation_one_native, turn_d_provider.calls[1].items)
        self.assertEqual(
            sum(
                isinstance(item, Message)
                and item.role is Role.USER
                and item.content == "generation two turn D"
                for item in turn_d_provider.calls[1].items
            ),
            1,
        )

        reopened_again = SqliteSessionStore(self.database)
        await reopened_again.initialize()
        turn_e_provider = _ScriptedProvider(
            ((ModelTextDelta("generation two turn E"), ModelCompleted("stop")),)
        )
        turn_e_result, turn_e_items = await run_reopened_turn(
            reopened_again,
            "generation two turn E",
            turn_e_provider,
        )
        self.assertEqual(turn_e_result.response, "generation two turn E")
        self.assertIn(Message(Role.ASSISTANT, "generation two turn D"), turn_e_items)
        self.assertIn(generation_two_native, turn_e_items)
        self.assertNotIn(Message(Role.ASSISTANT, "generation one turn A"), turn_e_items)
        self.assertNotIn(Message(Role.ASSISTANT, "generation one turn B"), turn_e_items)
        self.assertNotIn(Message(Role.ASSISTANT, "generation one turn C"), turn_e_items)
        self.assertNotIn(generation_one_native, turn_e_items)
        self.assertNotIn(Message(Role.USER, "generation zero history"), turn_e_items)
        self.assertEqual(
            sum(
                isinstance(item, Message)
                and item.role is Role.USER
                and item.content == "generation two turn E"
                for item in turn_e_items
            ),
            1,
        )
        self.assertEqual(await reopened_again.load_context_generation(self.session_id), 2)
        self.assertEqual(
            await reopened_again.load_working_set(self.session_id),
            before_working_set,
        )

    async def test_reopened_compaction_allows_a_new_source_range_before_rollover(self) -> None:
        window = ProviderContextWindow(
            "fixture-provider",
            "fixture-model",
            4_000,
            "fixture-affinity",
        )
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            *(Message(Role.USER, f"old history-{index}: " + "x" * 600) for index in range(8)),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        tool = _LargeResultTool("turn A tool result " + "z" * 8_000)
        model_provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("inspect-1", "inspect", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("turn A finalized"), ModelCompleted("stop")),
                (ModelTextDelta("turn B finalized"), ModelCompleted("stop")),
            )
        )
        compaction_provider = _ScriptedProvider(
            (
                (ModelCompleted("stop", response_text="C1 summary"),),
                (ModelCompleted("stop", response_text="C2 summary"),),
            )
        )

        def compaction_gate(store: SqliteSessionStore) -> _RecordingCompactionGate:
            return _RecordingCompactionGate(
                ContextCompactionTriggerService(
                    ContextCompactionApplicationService(store, compaction_provider),
                    planner=ContextCompactionPlanner(
                        ContextCompactionPolicy(
                            soft_limit_ratio=0.60,
                            hard_limit_ratio=0.80,
                            minimum_recent_items=2,
                            max_summary_tokens=64,
                        )
                    ),
                )
            )

        first_gate = compaction_gate(self.store)
        first = await self._runtime(
            model_provider,
            tools=_ToolCollection((tool,)),
            compaction_runtime_gate=first_gate,
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "turn A",
            initial_items=initial_items,
            source_provider=window.provider_name,
            source_model=window.model_name,
            source_context_affinity=window.context_affinity,
            session_id=self.session_id,
        )
        self.assertEqual(first.response, "turn A finalized")
        self.assertEqual(await self.store.load_context_generation(self.session_id), 0)
        first_compactions = await self.store.load_compaction_items(self.session_id)
        self.assertEqual(len(first_compactions), 1)
        c1 = first_compactions[0]

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        reopened_items = await reopened.load_session_items(self.session_id)
        reopened_summary = await reopened.get_session(self.session_id)
        second_gate = compaction_gate(reopened)
        second = await self._runtime(
            model_provider,
            tools=_ToolCollection((tool,)),
            store=reopened,
            compaction_runtime_gate=second_gate,
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "turn B " + "y" * 8_000,
            initial_items=reopened_items,
            source_provider=reopened_summary.provider,
            source_model=reopened_summary.model,
            source_context_affinity=reopened_summary.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(second.response, "turn B finalized")
        self.assertEqual(await reopened.load_context_generation(self.session_id), 0)
        second_compactions = await reopened.load_compaction_items(self.session_id)
        self.assertEqual(len(second_compactions), 2)
        c2 = second_compactions[1]
        self.assertNotEqual(
            (c1.source_item_count, c1.candidate_range),
            (c2.source_item_count, c2.candidate_range),
        )
        self.assertEqual(len(compaction_provider.calls), 2)
        self.assertEqual(first_gate.trigger_count, 1)
        self.assertEqual(second_gate.trigger_count, 1)
        self.assertTrue(
            any(
                isinstance(item, Message) and "C1 summary" in item.content
                for item in second_gate.automatic_usage_contexts[0].items
            )
        )
        final_items = model_provider.calls[-1].items
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.COMPACTION_SUMMARY
                and "C2 summary" in item.content
                for item in final_items
            )
        )
        self.assertFalse(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.COMPACTION_SUMMARY
                and "C1 summary" in item.content
                for item in final_items
            )
        )
        preflights = [
            event for event in second.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(preflights[-1].data["status"], "safe")
        self.assertFalse(preflights[-1].data["automatic_rollover_attempted"])

    async def test_reopened_same_compaction_range_consumes_one_cycle_without_duplicate_summary(
        self,
    ) -> None:
        window = ProviderContextWindow(
            "fixture-provider",
            "fixture-model",
            800,
            "fixture-affinity",
        )
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            Message(Role.USER, "compacted history " + "x" * 2_000),
            Message(Role.USER, "recent durable history"),
        )
        await self.store.save_session_items(self.session_id, initial_items)
        c1 = build_durable_compaction_item(
            ModelContext(
                initial_items,
                window.provider_name,
                window.model_name,
                window.context_affinity,
            ),
            ContextSummaryRequest(
                provider_window=window,
                source_item_count=len(initial_items),
                protected_item_count=1,
                recent_item_count=1,
                candidate_range=(1, 2),
                target_tokens=600,
                max_summary_tokens=64,
            ),
            compaction_id="turn-a-compaction",
            summary="C1 summary",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await self.store.save_compaction_item(self.session_id, c1)

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        reopened_items = await reopened.load_session_items(self.session_id)
        reopened_summary = await reopened.get_session(self.session_id)
        background = _PendingBackgroundTaskManager(
            BackgroundTaskSnapshot(
                task_id="task-1",
                command="fixture background task",
                cwd=self._temporary.name,
                status=BackgroundTaskStatus.COMPLETED,
                output="wake output " + "q" * 14_000,
                total_output_bytes=14_013,
                truncated=False,
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            )
        )
        model_provider = _ScriptedProvider(())
        compaction_provider = _ScriptedProvider(())
        gate = _RecordingCompactionGate(
            ContextCompactionTriggerService(
                ContextCompactionApplicationService(reopened, compaction_provider),
                planner=ContextCompactionPlanner(
                    ContextCompactionPolicy(
                        minimum_recent_items=1,
                        max_summary_tokens=64,
                    )
                ),
            )
        )

        result = await self._runtime(
            model_provider,
            store=reopened,
            compaction_runtime_gate=gate,
            provider_context_window=window,
            provider_max_output_tokens=64,
            background_tasks=background,
        ).run(
            "",
            initial_items=reopened_items,
            source_provider=reopened_summary.provider,
            source_model=reopened_summary.model,
            source_context_affinity=reopened_summary.context_affinity,
            session_id=self.session_id,
            turn_source=TurnSource.BACKGROUND_TASK_AUTO_WAKE,
        )

        self.assertEqual(len(model_provider.calls), 0)
        self.assertEqual(len(compaction_provider.calls), 0)
        self.assertEqual(gate.trigger_count, 0)
        self.assertEqual(len(gate.automatic_usage_contexts), 1)
        self.assertTrue(
            any(
                isinstance(item, Message) and "C1 summary" in item.content
                for item in gate.automatic_usage_contexts[0].items
            )
        )
        self.assertEqual(await reopened.load_context_generation(self.session_id), 0)
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(preflights[-1].data["status"], "blocked")
        self.assertTrue(preflights[-1].data["compaction_attempted"])
        self.assertFalse(preflights[-1].data["automatic_rollover_attempted"])

    async def test_reopened_generation_resumes_only_compatible_compaction(self) -> None:
        window = ProviderContextWindow(
            "fixture-provider",
            "fixture-model",
            2_000,
            "fixture-affinity",
        )
        old_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "generation-zero-reasoning",
                "summary": [{"type": "summary_text", "text": "generation zero"}],
                "encrypted_content": "generation-zero-native-state",
            },
        )
        initial_items = (
            Message(Role.SYSTEM, "fixture system"),
            Message(Role.USER, "generation zero history"),
            old_native,
        )
        await self.store.save_session_items(self.session_id, initial_items)
        pre_rollover_compaction = build_durable_compaction_item(
            ModelContext(
                initial_items,
                window.provider_name,
                window.model_name,
                window.context_affinity,
            ),
            ContextSummaryRequest(
                provider_window=window,
                source_item_count=3,
                protected_item_count=1,
                recent_item_count=0,
                candidate_range=(1, 3),
                target_tokens=1_600,
                max_summary_tokens=32,
            ),
            compaction_id="generation-zero-compaction",
            summary="pre-rollover summary that must not be selected",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await self.store.save_compaction_item(self.session_id, pre_rollover_compaction)

        generation_one_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "generation-one-reasoning",
                "summary": [{"type": "summary_text", "text": "generation one"}],
                "encrypted_content": "generation-one-native-state",
            },
        )
        accumulated_context = "generation one accumulated evidence " + ("x" * 120_000)
        provider = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-1", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta(accumulated_context), ModelCompleted("stop")),
                (
                    ModelCompleted(
                        "stop",
                        response_text="generation-one durable summary",
                    ),
                ),
                (
                    ModelTextDelta("second turn finalized"),
                    ModelCompleted("stop", context_items=(generation_one_native,)),
                ),
                (ModelTextDelta("reopened with generation-one compaction"), ModelCompleted("stop")),
            )
        )

        def compaction_gate(store: SqliteSessionStore) -> ContextCompactionRuntimeGate:
            return ContextCompactionRuntimeGate(
                ContextCompactionTriggerService(
                    ContextCompactionApplicationService(store, provider),
                    planner=ContextCompactionPlanner(
                        ContextCompactionPolicy(
                            soft_limit_ratio=0.80,
                            hard_limit_ratio=0.95,
                            minimum_recent_items=1,
                            max_summary_tokens=64,
                        )
                    ),
                )
            )

        first = await self._runtime(
            provider,
            compaction_runtime_gate=compaction_gate(self.store),
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "generation one accumulation",
            initial_items=initial_items,
            source_provider=window.provider_name,
            source_model=window.model_name,
            source_context_affinity=window.context_affinity,
            session_id=self.session_id,
        )
        self.assertEqual(first.response, accumulated_context)
        self.assertEqual(await self.store.load_context_generation(self.session_id), 1)

        persisted_after_first = await self.store.load_session_items(self.session_id)
        second = await self._runtime(
            provider,
            compaction_runtime_gate=compaction_gate(self.store),
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "persist generation one compaction",
            initial_items=persisted_after_first,
            source_provider=window.provider_name,
            source_model=window.model_name,
            source_context_affinity=window.context_affinity,
            session_id=self.session_id,
        )
        self.assertEqual(second.response, "second turn finalized")
        stored_compactions = await self.store.load_compaction_items(self.session_id)
        self.assertEqual(len(stored_compactions), 2)
        self.assertEqual(stored_compactions[0].compaction_id, "generation-zero-compaction")
        generation_one_compaction = stored_compactions[1]
        self.assertNotEqual(generation_one_compaction.compaction_id, "generation-zero-compaction")
        self.assertGreater(generation_one_compaction.source_item_count, 1)

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        reopened_items = await reopened.load_session_items(self.session_id)
        reopened_summary = await reopened.get_session(self.session_id)
        third = await self._runtime(
            provider,
            store=reopened,
            compaction_runtime_gate=compaction_gate(reopened),
            provider_context_window=window,
            provider_max_output_tokens=64,
        ).run(
            "reopen after generation one compaction",
            initial_items=reopened_items,
            source_provider=reopened_summary.provider,
            source_model=reopened_summary.model,
            source_context_affinity=reopened_summary.context_affinity,
            session_id=self.session_id,
        )

        self.assertEqual(third.response, "reopened with generation-one compaction")
        resumed_items = provider.calls[-1].items
        self.assertTrue(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.COMPACTION_SUMMARY
                and "generation-one durable summary" in item.content
                for item in resumed_items
            )
        )
        self.assertFalse(
            any(
                isinstance(item, Message)
                and (
                    "generation zero history" in item.content
                    or "pre-rollover summary that must not be selected" in item.content
                    or "generation one accumulated evidence" in item.content
                )
                for item in resumed_items
            )
        )
        self.assertNotIn(old_native, resumed_items)
        self.assertIn(generation_one_native, resumed_items)
        self.assertEqual(
            sum(
                isinstance(item, Message)
                and item.role is Role.USER
                and item.content == "reopen after generation one compaction"
                for item in resumed_items
            ),
            1,
        )
        preflight = next(
            event for event in third.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        )
        self.assertEqual(preflight.data["status"], "safe")
        self.assertIsInstance(preflight.data["context_tokens"], int)
        self.assertLess(preflight.data["context_tokens"], 2_000)

    async def test_rollover_resets_local_provider_origin_after_foreign_native_failover(
        self,
    ) -> None:
        old_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "old-reasoning",
                "summary": [{"type": "summary_text", "text": "old"}],
                "encrypted_content": "old-provider-state",
            },
        )
        initial_items = (Message(Role.SYSTEM, "fixture system"), old_native)
        await self.store.save_session_items(self.session_id, initial_items)
        await self.store.update_session_provider(
            self.session_id,
            "primary",
            "primary-model",
            "primary-affinity",
        )
        primary = _ScriptedProvider(
            ((ProviderError("primary unavailable"),),),
            provider_name="primary",
            model_name="primary-model",
            context_affinity="primary-affinity",
        )
        fallback = _ScriptedProvider(
            (
                (
                    ModelToolCall(ToolCall("roll-1", "new_context", {})),
                    ModelCompleted("tool_calls"),
                ),
                (ModelTextDelta("fallback fresh"), ModelCompleted("stop")),
            ),
            provider_name="fallback",
            model_name="fallback-model",
            context_affinity="fallback-affinity",
        )
        provider = FailoverModelProvider(
            (
                ProviderCandidate(
                    "primary",
                    "primary-model",
                    "primary-affinity",
                    lambda: primary,
                ),
                ProviderCandidate(
                    "fallback",
                    "fallback-model",
                    "fallback-affinity",
                    lambda: fallback,
                ),
            )
        )

        result = await self._runtime(provider).run(
            "continue after failover",
            initial_items=initial_items,
            source_provider="primary",
            source_model="primary-model",
            source_context_affinity="primary-affinity",
            session_id=self.session_id,
        )

        self.assertEqual(result.response, "fallback fresh")
        self.assertEqual(len(fallback.calls), 2)
        self.assertEqual(fallback.calls[1].source_provider, "fallback")
        self.assertEqual(fallback.calls[1].source_model, "fallback-model")
        self.assertEqual(fallback.calls[1].source_context_affinity, "fallback-affinity")
        self.assertNotIn(old_native, fallback.calls[1].preserved_items)
        summary = await self.store.get_session(self.session_id)
        self.assertEqual(summary.provider, "primary")
        self.assertEqual(summary.context_affinity, "primary-affinity")

    async def test_rollover_filters_native_state_from_an_actual_responses_request(self) -> None:
        captured: list[dict[str, object]] = []
        responses = [
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "roll-1",
                        "name": "new_context",
                        "arguments": "{}",
                    }
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "wire fresh"}],
                    }
                ],
            },
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, text=self._responses_sse(responses[len(captured) - 1]))

        old_native = PreservedContextItem(
            ContextItemKind.REASONING,
            {
                "type": "reasoning",
                "id": "old-reasoning",
                "summary": [{"type": "summary_text", "text": "old"}],
                "encrypted_content": "old-generation-opaque-state",
            },
        )
        initial_items = (Message(Role.SYSTEM, "fixture system"), old_native)
        await self.store.save_session_items(self.session_id, initial_items)
        provider = OpenAIResponsesProvider(
            model="response-model",
            base_url="https://gateway.invalid/v1",
            api_key="fixture",
            provider_name="gateway",
            context_affinity="profile-v1:matching",
            transport=httpx.MockTransport(handler),
        )

        result = await self._runtime(provider).run(
            "continue on the wire",
            initial_items=initial_items,
            source_provider="gateway",
            source_model="response-model",
            source_context_affinity="profile-v1:matching",
            session_id=self.session_id,
        )

        self.assertEqual(result.response, "wire fresh")
        self.assertEqual(len(captured), 2)
        self.assertIn("old-generation-opaque-state", json.dumps(captured[0]))
        self.assertNotIn("old-generation-opaque-state", json.dumps(captured[1]))
        self.assertEqual(await self.store.load_context_generation(self.session_id), 1)


if __name__ == "__main__":
    unittest.main()
