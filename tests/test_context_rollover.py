from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from neuro_code.application.memory.compaction import (
    ContextCompactionPlanner,
    ContextCompactionPolicy,
    ContextSummaryRequest,
    ProviderContextWindow,
    build_durable_compaction_item,
)
from neuro_code.application.memory.compaction_runtime import ContextCompactionRuntimeGate
from neuro_code.application.memory.compaction_service import ContextCompactionApplicationService
from neuro_code.application.memory.compaction_trigger import ContextCompactionTriggerService
from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
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
from neuro_code.application.runtime.agent_loop import AgentRunResult
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.sessions.context_rollover import (
    SessionContextRolloverApplicationService,
)
from neuro_code.application.sessions.recovery import TurnRecoveryService
from neuro_code.application.sessions.working_set import SessionWorkingSetApplicationService
from neuro_code.domain.conversation.context import ModelContext
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
from neuro_code.domain.execution import TurnInput, TurnRecoveryAttempt, TurnRecoveryStatus
from neuro_code.domain.tools import ToolDefinition
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
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
        store: SqliteSessionStore | None = None,
        compaction_runtime_gate: ContextCompactionRuntimeGate | None = None,
        provider_context_window: ProviderContextWindow | None = None,
        provider_max_output_tokens: int | None = None,
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
            compaction_runtime_gate=compaction_runtime_gate,
            provider_context_window=provider_context_window,
            provider_max_output_tokens=provider_max_output_tokens,
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
                (33,),
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
