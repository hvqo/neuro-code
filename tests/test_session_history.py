from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.session_history import (
    ListSessionItemsRequest,
    ReadSessionItemRequest,
    SearchSessionItemsRequest,
    SessionItemReference,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.application.sessions import SessionApplicationService
from neuro_code.application.sessions.item_queries import (
    SessionItemQueryService,
)
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import (
    ContextItemKind,
    Message,
    PreservedContextItem,
    Role,
    SyntheticReason,
    ToolCall,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry
from neuro_code.infrastructure.tools.session_history import SessionHistoryTool
from neuro_code.infrastructure.workspace.changes import FilesystemWorkspaceChangeObserver
from neuro_code.shared.errors import SessionError, ToolError


class _HistoryOnlyStore:
    def __init__(self, items: tuple[Message | PreservedContextItem, ...]) -> None:
        self._items = items

    async def load_session_items(self, session_id: str) -> list[Message | PreservedContextItem]:
        del session_id
        return list(self._items)


class SessionHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.store = SqliteSessionStore(Path(self._temporary.name) / "sessions.db")
        await self.store.initialize()
        self.session_id = await self.store.create_session(
            self._temporary.name,
            "fixture-provider",
            "fixture-model",
        )
        self.query = SessionItemQueryService(
            self.store,
            redaction_values=("configured-history-secret",),
        )

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    async def _save(self, *items: Message | PreservedContextItem) -> None:
        await self.store.save_session_items(self.session_id, items)

    def _context(self, **kwargs: object) -> ToolContext:
        return ToolContext(
            Path(self._temporary.name),
            session_id=self.session_id,
            **kwargs,
        )

    async def test_reference_survives_append_and_reads_exact_safe_content(self) -> None:
        initial = (
            Message(Role.USER, "first durable prompt"),
            Message(
                Role.ASSISTANT,
                "durable answer",
                reasoning_content="private reasoning must not be rehydrated",
            ),
        )
        await self._save(*initial)

        page = await self.query.list_session_items(ListSessionItemsRequest(self.session_id))
        first = next(item for item in page.items if item.ordinal == 1)
        self.assertTrue(first.reference.token.startswith("shr1_"))
        self.assertNotIn(self.session_id, first.reference.token)

        await self.store.save_session_items(
            self.session_id,
            (*initial, Message(Role.USER, "appended prompt")),
        )

        read = await self.query.read_session_item(
            ReadSessionItemRequest(self.session_id, first.reference)
        )
        self.assertEqual(read.ordinal, 1)
        self.assertEqual(read.content, "first durable prompt")
        self.assertNotIn("private reasoning", read.to_dict())

        updated = await self.query.list_session_items(ListSessionItemsRequest(self.session_id))
        updated_first = next(item for item in updated.items if item.ordinal == 1)
        self.assertEqual(updated_first.reference, first.reference)

    async def test_reference_rejects_tampering_cross_session_and_stale_items(self) -> None:
        item = Message(Role.USER, "addressed item")
        await self._save(item)
        reference = (
            (await self.query.list_session_items(ListSessionItemsRequest(self.session_id)))
            .items[0]
            .reference
        )

        replacement = "A" if reference.token[10] != "A" else "B"
        tampered = SessionItemReference(reference.token[:10] + replacement + reference.token[11:])
        with self.assertRaises(SessionError):
            await self.query.read_session_item(ReadSessionItemRequest(self.session_id, tampered))

        other_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture-provider",
            "fixture-model",
        )
        await self.store.save_session_items(other_session_id, (item,))
        with self.assertRaises(SessionError):
            await self.query.read_session_item(ReadSessionItemRequest(other_session_id, reference))

        stale = SessionItemReference.for_item(self.session_id, 2, item)
        with self.assertRaises(SessionError):
            await self.query.read_session_item(ReadSessionItemRequest(self.session_id, stale))

    async def test_list_search_and_read_expose_only_visible_textual_items(self) -> None:
        items = (
            Message(Role.SYSTEM, "system-only marker"),
            Message(Role.USER, "visible user marker with configured-history-secret"),
            Message(
                Role.ASSISTANT,
                "visible assistant marker",
                reasoning_content="hidden reasoning marker",
            ),
            Message(Role.TOOL, "visible tool result marker", name="read_file"),
            Message(
                Role.USER,
                "synthetic marker",
                synthetic_reason=SyntheticReason.RUNTIME_BUDGET,
            ),
            PreservedContextItem(
                ContextItemKind.REASONING,
                {"type": "reasoning", "text": "native context marker"},
            ),
        )
        query = SessionItemQueryService(
            cast(SessionStore, _HistoryOnlyStore(items)),
            redaction_values=("configured-history-secret",),
        )

        first_page = await query.list_session_items(
            ListSessionItemsRequest(self.session_id, limit=2)
        )
        self.assertEqual([item.ordinal for item in first_page.items], [4, 3])
        self.assertEqual(first_page.total_estimate, 3)
        self.assertEqual(first_page.next_offset, 2)
        second_page = await query.list_session_items(
            ListSessionItemsRequest(self.session_id, limit=2, offset=2)
        )
        self.assertEqual([item.ordinal for item in second_page.items], [2])
        self.assertIsNone(second_page.next_offset)
        self.assertTrue(
            all(
                item.role in {Role.USER, Role.ASSISTANT, Role.TOOL}
                for item in first_page.items + second_page.items
            )
        )
        self.assertNotIn("configured-history-secret", second_page.items[0].preview)

        assistant_matches = await query.search_session_items(
            SearchSessionItemsRequest(self.session_id, "visible assistant marker")
        )
        self.assertEqual([item.role for item in assistant_matches.items], [Role.ASSISTANT])
        self.assertIn("visible assistant marker", assistant_matches.items[0].snippet or "")
        assistant_read = await query.read_session_item(
            ReadSessionItemRequest(self.session_id, assistant_matches.items[0].reference)
        )
        self.assertEqual(assistant_read.content, "visible assistant marker")
        self.assertNotIn("hidden reasoning marker", assistant_read.content)
        self.assertNotIn("reasoning_content", assistant_read.to_dict())

        tool_matches = await query.search_session_items(
            SearchSessionItemsRequest(self.session_id, "visible tool result marker")
        )
        self.assertEqual(tool_matches.items[0].role, Role.TOOL)
        self.assertEqual(tool_matches.items[0].kind.value, "tool_result")
        self.assertEqual(tool_matches.items[0].tool_name, "read_file")

        for hidden_query in (
            "system-only marker",
            "hidden reasoning marker",
            "synthetic marker",
            "native context marker",
            "configured-history-secret",
        ):
            hidden = await query.search_session_items(
                SearchSessionItemsRequest(self.session_id, hidden_query)
            )
            self.assertEqual(hidden.items, (), hidden_query)

    async def test_large_item_reads_are_bounded_and_utf8_pageable(self) -> None:
        content = "头" + ("0123456789中" * 100)
        await self._save(Message(Role.USER, content))
        reference = (
            (await self.query.list_session_items(ListSessionItemsRequest(self.session_id)))
            .items[0]
            .reference
        )

        chunks: list[str] = []
        offset = 0
        while True:
            read = await self.query.read_session_item(
                ReadSessionItemRequest(
                    self.session_id,
                    reference,
                    offset=offset,
                    max_bytes=32,
                )
            )
            self.assertLessEqual(len(read.content.encode("utf-8")), 32)
            chunks.append(read.content)
            if read.next_offset is None:
                break
            self.assertGreater(read.next_offset, offset)
            offset = read.next_offset

        self.assertEqual("".join(chunks), content)
        with self.assertRaises(SessionError):
            await self.query.read_session_item(
                ReadSessionItemRequest(self.session_id, reference, offset=1, max_bytes=4)
            )

    async def test_fork_has_independent_reference_scope(self) -> None:
        await self._save(Message(Role.USER, "fork-visible item"))
        parent_page = await self.query.list_session_items(ListSessionItemsRequest(self.session_id))
        parent_reference = parent_page.items[0].reference
        fork_id = await self.store.fork_session(self.session_id)

        fork_page = await self.query.list_session_items(ListSessionItemsRequest(fork_id))
        self.assertEqual(fork_page.items[0].preview, "fork-visible item")
        self.assertNotEqual(fork_page.items[0].reference, parent_reference)
        with self.assertRaises(SessionError):
            await self.query.read_session_item(ReadSessionItemRequest(fork_id, parent_reference))

    async def test_application_service_and_model_tool_share_redaction_boundary(self) -> None:
        secret = "configured-history-secret"
        await self._save(Message(Role.USER, f"ordinary history {secret}"))
        application = SessionApplicationService(self.store, redaction_values=(secret,))
        before = await self.store.load_session_items(self.session_id)

        listed = await application.list_session_items(ListSessionItemsRequest(self.session_id))
        self.assertEqual(listed.items[0].preview, "ordinary history [REDACTED]")
        reference = listed.items[0].reference

        searched = await application.search_session_items(
            SearchSessionItemsRequest(self.session_id, "ordinary history")
        )
        self.assertIn("ordinary history", searched.items[0].snippet or "")
        self.assertNotIn(secret, searched.items[0].snippet or "")

        read = await application.read_session_item(
            ReadSessionItemRequest(self.session_id, reference)
        )
        self.assertEqual(read.content, "ordinary history [REDACTED]")
        self.assertNotIn(secret, read.content)

        tool = SessionHistoryTool(self.query)
        tool_list = json.loads((await tool.execute({"operation": "list"}, self._context())).content)
        tool_read = await tool.execute(
            {
                "operation": "read",
                "reference": tool_list["items"][0]["reference"],
            },
            self._context(),
        )
        self.assertNotIn(secret, tool_read.content)
        self.assertIn("ordinary history", tool_read.content)
        self.assertEqual(await self.store.load_session_items(self.session_id), before)

    async def test_model_tool_is_read_only_and_uses_only_bound_session(self) -> None:
        await self._save(Message(Role.USER, "tool-visible history"))
        tool = SessionHistoryTool(self.query)
        self.assertFalse(tool.side_effecting)
        self.assertNotIn("session_id", tool.definition.input_schema["properties"])

        before = await self.store.load_session_items(self.session_id)
        listed = await tool.execute({"operation": "list"}, self._context())
        listed_payload = json.loads(listed.content)
        self.assertEqual(listed_payload["operation"], "list")
        self.assertTrue(listed.metadata and listed.metadata["read_only"])

        searched = await tool.execute(
            {"operation": "search", "query": "tool-visible"},
            self._context(),
        )
        searched_payload = json.loads(searched.content)
        self.assertEqual(searched_payload["operation"], "search")
        self.assertEqual(searched_payload["items"][0]["ordinal"], 1)

        read = await tool.execute(
            {
                "operation": "read",
                "reference": listed_payload["items"][0]["reference"],
                "max_bytes": 4,
            },
            self._context(),
        )
        self.assertEqual(json.loads(read.content)["item"]["content"], "tool")
        self.assertLessEqual(len(read.content.encode("utf-8")), self._context().output_byte_limit)
        self.assertEqual(await self.store.load_session_items(self.session_id), before)

        with self.assertRaises(ToolError):
            await tool.execute(
                {"operation": "list", "session_id": "unrelated-session"},
                self._context(),
            )
        with self.assertRaises(ToolError):
            await tool.execute({"operation": "list"}, ToolContext(Path(self._temporary.name)))

        registry = default_tool_registry(
            allowed_tool_names=("session_history",),
            session_item_query=self.query,
        )
        self.assertIsInstance(registry.get("session_history"), SessionHistoryTool)
        self.assertIsNone(
            default_tool_registry(allowed_tool_names=("session_history",)).get("session_history")
        )

    async def test_tool_executor_binds_the_current_session_to_history_tool(self) -> None:
        tool = SessionHistoryTool(self.query)
        executor = ToolExecutor(
            tools=ToolRegistry([tool]),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            approver=None,
            tool_context=ToolContext(Path(self._temporary.name)),
            session_store=None,
            workspace_change_observer=FilesystemWorkspaceChangeObserver(),
            context_builder=ContextBuilder(
                reasoning_effort=ReasoningEffort.HIGH,
                interaction_mode=InteractionMode.NORMAL,
                plan=None,
                instruction_provider=None,
                skill_provider=None,
            ),
        )
        events: list[AgentEvent] = []

        async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
            event = AgentEvent.create(len(events) + 1, kind, data)
            events.append(event)
            return event

        messages: list[Message] = []
        context_items: list[Message | PreservedContextItem] = []
        await self._save(Message(Role.USER, "executor-bound history"))
        await executor.execute(
            ToolCall("history-1", "session_history", {"operation": "list"}),
            messages,
            context_items,
            emit,
            self.session_id,
        )

        self.assertEqual(json.loads(messages[-1].content)["items"][0]["ordinal"], 1)
        self.assertEqual(context_items, messages)


if __name__ == "__main__":
    unittest.main()
