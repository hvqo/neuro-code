from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import closing
from pathlib import Path

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.session_history import (
    ListSessionItemsRequest,
    SessionItemReference,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.working_set import (
    MAX_WORKING_SET_CONTEXT_BYTES,
    MAX_WORKING_SET_ENTRIES_PER_SECTION,
    MAX_WORKING_SET_ENTRY_BYTES,
    MAX_WORKING_SET_TOTAL_BYTES,
    WORKING_SET_SECTION_ORDER,
    ReadWorkingSetRequest,
    UpdateWorkingSetRequest,
    WorkingSetEntry,
    WorkingSetEntryProvenance,
    WorkingSetSection,
    WorkingSetSectionState,
    WorkingSetSnapshot,
    WorkingSetUpdate,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.sessions import SessionApplicationService
from neuro_code.application.sessions.item_queries import SessionItemQueryService
from neuro_code.application.sessions.working_set import SessionWorkingSetApplicationService
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelCompleted, ModelEvent, ModelTextDelta
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.tools import ToolDefinition, ToolExecutionMode
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.infrastructure.tools.registry import ToolRegistry, default_tool_registry
from neuro_code.infrastructure.tools.session_working_set import SessionWorkingSetTool
from neuro_code.shared.errors import SessionError, ToolError
from tests.fakes import EmptyWorkspaceChangeObserver


def _sections(
    **entries: tuple[WorkingSetEntry, ...],
) -> tuple[WorkingSetSectionState, ...]:
    return tuple(
        WorkingSetSectionState(section, entries.get(section.value, ()))
        for section in WORKING_SET_SECTION_ORDER
    )


def _update(
    expected_revision: int,
    **entries: tuple[WorkingSetEntry, ...],
) -> WorkingSetUpdate:
    return WorkingSetUpdate(expected_revision, _sections(**entries))


class _RecordingProvider:
    provider_name = "fixture-provider"
    model_name = "fixture-model"
    context_affinity = "fixture-affinity"

    def __init__(self, events: Sequence[ModelEvent]) -> None:
        self._events = tuple(events)
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
        for event in self._events:
            yield event


class _WorkingSetStoreFixture:
    def __init__(self, *, loaded: object, saved: object | None = None) -> None:
        self.loaded = loaded
        self.saved = saved

    async def load_working_set(self, session_id: str) -> object:
        del session_id
        return self.loaded

    async def load_session_items(self, session_id: str) -> list[Message]:
        del session_id
        return []

    async def save_working_set(
        self,
        session_id: str,
        snapshot: WorkingSetSnapshot,
        *,
        expected_revision: int,
    ) -> object:
        del session_id, snapshot, expected_revision
        return self.saved


class WorkingSetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database = Path(self._temporary.name) / "sessions.db"
        self.store = SqliteSessionStore(self.database)
        await self.store.initialize()
        self.session_id = await self.store.create_session(
            self._temporary.name,
            "fixture-provider",
            "fixture-model",
        )
        self.secret = "configured-working-set-secret"
        self.application = SessionApplicationService(
            self.store,
            redaction_values=(self.secret,),
        )

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    def _context(
        self, *, session_id: str | None = None, output_byte_limit: int = 200_000
    ) -> ToolContext:
        return ToolContext(
            Path(self._temporary.name),
            session_id=self.session_id if session_id is None else session_id,
            output_byte_limit=output_byte_limit,
        )

    async def _history_reference(self, session_id: str, item: Message) -> SessionItemReference:
        await self.store.save_session_items(session_id, (item,))
        page = await SessionItemQueryService(self.store).list_session_items(
            ListSessionItemsRequest(session_id)
        )
        return page.items[0].reference

    async def test_legacy_empty_state_is_redacted_and_survives_reopen(self) -> None:
        empty = await self.application.read_working_set(ReadWorkingSetRequest(self.session_id))
        self.assertEqual(empty, WorkingSetSnapshot.empty(self.session_id))
        self.assertIsNone(await self.store.load_working_set(self.session_id))

        source = Message(Role.USER, "durable source evidence")
        reference = await self._history_reference(self.session_id, source)
        before_history = await self.store.load_session_items(self.session_id)
        updated = await self.application.update_working_set(
            UpdateWorkingSetRequest(
                self.session_id,
                _update(
                    0,
                    goal=(WorkingSetEntry(f"goal {self.secret}"),),
                    decisions=(WorkingSetEntry("confirmed by source", reference),),
                ),
            )
        )

        self.assertEqual(updated.revision, 1)
        self.assertEqual(updated.sections[0].entries[0].text, "goal [REDACTED]")
        self.assertEqual(
            updated.sections[0].entries[0].provenance,
            WorkingSetEntryProvenance.MODEL,
        )
        self.assertEqual(
            updated.sections[2].entries[0].provenance,
            WorkingSetEntryProvenance.HISTORY,
        )
        self.assertNotIn(self.secret, json.dumps(updated.to_dict()))
        self.assertEqual(await self.store.load_session_items(self.session_id), before_history)

        reopened = SqliteSessionStore(self.database)
        await reopened.initialize()
        reopened_application = SessionApplicationService(
            reopened,
            redaction_values=(self.secret,),
        )
        self.assertEqual(
            await reopened_application.read_working_set(ReadWorkingSetRequest(self.session_id)),
            updated,
        )
        self.assertEqual(await reopened.load_session_items(self.session_id), before_history)

    async def test_revision_cas_is_atomic_and_failed_update_preserves_previous_snapshot(
        self,
    ) -> None:
        first = await self.application.update_working_set(
            UpdateWorkingSetRequest(
                self.session_id,
                _update(0, goal=(WorkingSetEntry("initial goal"),)),
            )
        )
        with self.assertRaises(SessionError):
            await self.application.update_working_set(
                UpdateWorkingSetRequest(
                    self.session_id,
                    _update(0, goal=(WorkingSetEntry("stale goal"),)),
                )
            )
        self.assertEqual(
            await self.application.read_working_set(ReadWorkingSetRequest(self.session_id)),
            first,
        )

        outcomes = await asyncio.gather(
            self.application.update_working_set(
                UpdateWorkingSetRequest(
                    self.session_id,
                    _update(1, progress=(WorkingSetEntry("winner A"),)),
                )
            ),
            self.application.update_working_set(
                UpdateWorkingSetRequest(
                    self.session_id,
                    _update(1, progress=(WorkingSetEntry("winner B"),)),
                )
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(outcome, WorkingSetSnapshot) for outcome in outcomes), 1)
        self.assertEqual(sum(isinstance(outcome, SessionError) for outcome in outcomes), 1)
        current = await self.application.read_working_set(ReadWorkingSetRequest(self.session_id))
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.entry_count, 1)

    async def test_history_references_fail_closed_on_update_and_read(self) -> None:
        source = Message(Role.USER, "reference source")
        reference = await self._history_reference(self.session_id, source)
        replacement = "A" if reference.token[10] != "A" else "B"
        tampered = SessionItemReference(reference.token[:10] + replacement + reference.token[11:])
        stale = SessionItemReference.for_item(self.session_id, 2, source)

        other_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture-provider",
            "fixture-model",
        )
        await self.store.save_session_items(other_session_id, (source,))
        invalid_requests = (
            ("tampered", self.session_id, tampered),
            ("cross-session", other_session_id, reference),
            ("stale", self.session_id, stale),
        )
        for name, session_id, invalid in invalid_requests:
            with self.subTest(reference=name), self.assertRaises(SessionError):
                await self.application.update_working_set(
                    UpdateWorkingSetRequest(
                        session_id,
                        _update(0, goal=(WorkingSetEntry("invalid", invalid),)),
                    )
                )
        self.assertIsNone(await self.store.load_working_set(self.session_id))

        persisted_invalid = WorkingSetSnapshot(
            self.session_id,
            1,
            _sections(goal=(WorkingSetEntry("stale persisted", stale),)),
        )
        await self.store.save_working_set(
            self.session_id,
            persisted_invalid,
            expected_revision=0,
        )
        with self.assertRaises(SessionError):
            await self.application.read_working_set(ReadWorkingSetRequest(self.session_id))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE session_working_sets SET snapshot_json = ? WHERE session_id = ?",
                (sqlite3.Binary(b"not-json"), self.session_id),
            )
            connection.commit()
        with self.assertRaises(SessionError):
            await self.store.load_working_set(self.session_id)

    async def test_malformed_persisted_state_fails_closed(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO session_working_sets(session_id, revision, snapshot_json)
                VALUES (?, ?, ?)
                """,
                (self.session_id, 1, "not-json"),
            )
            connection.commit()
        with self.assertRaises(SessionError):
            await self.application.read_working_set(ReadWorkingSetRequest(self.session_id))

    async def test_application_boundary_rejects_invalid_requests_and_store_projections(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            SessionWorkingSetApplicationService(
                self.store,
                redaction_values=(object(),),
            )
        fixture = _WorkingSetStoreFixture(loaded=object())
        service = SessionWorkingSetApplicationService(fixture)
        with self.assertRaises(ValueError):
            await service.read_working_set(object())
        with self.assertRaises(SessionError):
            await service.read_working_set(ReadWorkingSetRequest(self.session_id))

        wrong_scope = _WorkingSetStoreFixture(
            loaded=WorkingSetSnapshot("other-session", 0, _sections())
        )
        with self.assertRaises(SessionError):
            await SessionWorkingSetApplicationService(wrong_scope).read_working_set(
                ReadWorkingSetRequest(self.session_id)
            )
        with self.assertRaises(ValueError):
            await service.update_working_set(object())
        with self.assertRaises(SessionError):
            await self.application.update_working_set(
                UpdateWorkingSetRequest(
                    self.session_id,
                    WorkingSetUpdate(2**63 - 1, _sections()),
                )
            )

        unexpected_revision = WorkingSetSnapshot(self.session_id, 2, _sections())
        saved_fixture = _WorkingSetStoreFixture(loaded=None, saved=unexpected_revision)
        with self.assertRaises(SessionError):
            await SessionWorkingSetApplicationService(saved_fixture).update_working_set(
                UpdateWorkingSetRequest(
                    self.session_id,
                    _update(0),
                )
            )

    async def test_typed_sections_and_snapshot_bounds_are_strict(self) -> None:
        for invalid_session_id in ("", "\x00", "\x01", "x" * 513):
            with self.subTest(session_id=repr(invalid_session_id)), self.assertRaises(ValueError):
                ReadWorkingSetRequest(invalid_session_id)
        for invalid_revision in (True, -1, 2**63, "0"):
            with self.subTest(revision=repr(invalid_revision)), self.assertRaises(ValueError):
                WorkingSetSnapshot(self.session_id, invalid_revision, _sections())
        with self.assertRaises(ValueError):
            WorkingSetEntry("x" * (MAX_WORKING_SET_ENTRY_BYTES + 1))
        with self.assertRaises(ValueError):
            WorkingSetEntry("x", object())
        with self.assertRaises(ValueError):
            WorkingSetEntry.from_dict(None)
        with self.assertRaises(ValueError):
            WorkingSetEntry.from_dict({"text": 1})
        with self.assertRaises(ValueError):
            WorkingSetEntry.from_dict({"text": "x", "source_ref": 1})
        with self.assertRaises(ValueError):
            WorkingSetEntry.from_dict({"text": "x", "provenance": "invalid"})
        with self.assertRaises(ValueError):
            WorkingSetEntry.from_dict({"text": "x", "unexpected": True})
        with self.assertRaises(ValueError):
            WorkingSetSectionState(
                WorkingSetSection.GOAL,
                tuple(
                    WorkingSetEntry(f"entry-{index}")
                    for index in range(MAX_WORKING_SET_ENTRIES_PER_SECTION + 1)
                ),
            )
        with self.assertRaises(ValueError):
            WorkingSetSectionState("goal", ())
        with self.assertRaises(ValueError):
            WorkingSetSectionState(WorkingSetSection.GOAL, (object(),))
        with self.assertRaises(ValueError):
            WorkingSetSnapshot(self.session_id, 0, ())
        with self.assertRaises(ValueError):
            WorkingSetSnapshot(self.session_id, 0, tuple(reversed(_sections())))
        with self.assertRaises(ValueError):
            WorkingSetSnapshot(self.session_id, 0, (*_sections()[:-1], object()))
        with self.assertRaises(ValueError):
            WorkingSetEntry.from_dict(
                {"text": "history", "source_ref": None, "provenance": "history"}
            )
        with self.assertRaises(ValueError):
            WorkingSetUpdate.from_dict({"expected_revision": 0, "sections": {}, "unexpected": True})

        oversized_sections = tuple(
            WorkingSetSectionState(
                section,
                tuple(WorkingSetEntry("x" * MAX_WORKING_SET_ENTRY_BYTES) for _ in range(8)),
            )
            for section in WORKING_SET_SECTION_ORDER
        )
        self.assertGreater(
            len(
                json.dumps(
                    WorkingSetSnapshot(
                        self.session_id,
                        0,
                        _sections(),
                    ).to_dict(),
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
            0,
        )
        self.assertGreater(MAX_WORKING_SET_TOTAL_BYTES, MAX_WORKING_SET_ENTRY_BYTES)
        with self.assertRaises(ValueError):
            WorkingSetSnapshot(self.session_id, 0, oversized_sections)
        empty = WorkingSetSnapshot.empty(self.session_id)
        self.assertIsNone(empty.context_message())
        self.assertEqual(WorkingSetSnapshot.from_dict(empty.to_dict()), empty)
        self.assertGreater(empty.encoded_bytes, 0)
        with self.assertRaises(ValueError):
            WorkingSetSnapshot.from_dict(None)
        with self.assertRaises(ValueError):
            WorkingSetSnapshot.from_dict({"session_id": self.session_id})
        canonical = WorkingSetSnapshot.empty(self.session_id).to_dict()
        with self.assertRaises(ValueError):
            WorkingSetSnapshot.from_dict(
                {
                    "session_id": self.session_id,
                    "revision": 0,
                    "sections": None,
                }
            )
        canonical_sections = canonical["sections"]
        assert isinstance(canonical_sections, dict)
        malformed_sections = dict(canonical_sections)
        malformed_sections["goal"] = {}
        with self.assertRaises(ValueError):
            WorkingSetSnapshot.from_dict(
                {
                    "session_id": self.session_id,
                    "revision": 0,
                    "sections": malformed_sections,
                }
            )
        with self.assertRaises(ValueError):
            WorkingSetSnapshot.from_dict(
                {
                    "session_id": self.session_id,
                    "revision": 0,
                    "sections": {"goal": []},
                }
            )
        update = _update(0, goal=(WorkingSetEntry("round trip"),))
        self.assertEqual(WorkingSetUpdate.from_dict(update.to_dict()), update)
        with self.assertRaises(ValueError):
            WorkingSetUpdate.from_dict(None)
        with self.assertRaises(ValueError):
            WorkingSetUpdate.from_dict({"expected_revision": "zero", "sections": {}})
        with self.assertRaises(ValueError):
            UpdateWorkingSetRequest(self.session_id, object())

    async def test_store_rejects_invalid_scope_revision_and_persisted_identity(self) -> None:
        snapshot = WorkingSetSnapshot(self.session_id, 1, _sections())
        with self.assertRaises(TypeError):
            await self.store.save_working_set(
                self.session_id,
                object(),
                expected_revision=0,
            )
        with self.assertRaises(SessionError):
            await self.store.load_working_set("missing-session")
        with self.assertRaises(SessionError):
            await self.store.save_working_set(
                "missing-session",
                WorkingSetSnapshot("missing-session", 1, _sections()),
                expected_revision=0,
            )
        with self.assertRaises(SessionError):
            await self.store.save_working_set(
                self.session_id,
                snapshot,
                expected_revision=True,
            )
        with self.assertRaises(SessionError):
            await self.store.save_working_set(
                self.session_id,
                WorkingSetSnapshot("other-session", 1, _sections()),
                expected_revision=0,
            )
        with self.assertRaises(SessionError):
            await self.store.save_working_set(
                self.session_id,
                snapshot,
                expected_revision=1,
            )
        with self.assertRaises(SessionError):
            await self.store.save_working_set(
                self.session_id,
                WorkingSetSnapshot(self.session_id, 2, _sections()),
                expected_revision=1,
            )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO session_working_sets(session_id, revision, snapshot_json)
                VALUES (?, ?, ?)
                """,
                (self.session_id, 2, json.dumps(snapshot.to_dict())),
            )
            connection.commit()
        with self.assertRaises(SessionError):
            await self.store.load_working_set(self.session_id)

    async def test_context_projection_is_bounded_redacted_and_not_durable(self) -> None:
        source = Message(Role.USER, "context source")
        reference = await self._history_reference(self.session_id, source)
        snapshot = WorkingSetSnapshot(
            self.session_id,
            3,
            _sections(
                goal=(WorkingSetEntry(f"goal {self.secret}"),),
                decisions=(WorkingSetEntry("decision from history", reference),),
            ),
        )
        working_set_message = snapshot.context_message((self.secret,))
        self.assertIsNotNone(working_set_message)
        assert working_set_message is not None
        self.assertEqual(working_set_message.synthetic_reason, SyntheticReason.WORKING_SET)
        self.assertNotIn(self.secret, working_set_message.content)
        self.assertNotIn(self.session_id, working_set_message.content)
        self.assertLessEqual(
            len(working_set_message.content.encode("utf-8")),
            MAX_WORKING_SET_CONTEXT_BYTES,
        )

        builder = ContextBuilder(
            reasoning_effort=ReasoningEffort.HIGH,
            interaction_mode=InteractionMode.NORMAL,
            plan=None,
            instruction_provider=None,
            skill_provider=None,
        )
        rendered = builder.build(
            (Message(Role.USER, "current prompt"), working_set_message),
            working_set_message=working_set_message,
        )
        self.assertEqual(
            sum(
                isinstance(item, Message) and item.synthetic_reason is SyntheticReason.WORKING_SET
                for item in rendered
            ),
            1,
        )
        self.assertIsInstance(rendered[-1], Message)
        self.assertEqual(rendered[-1], working_set_message)
        self.assertEqual(await self.store.load_session_items(self.session_id), [source])

    async def test_runtime_refreshes_working_set_without_persisting_synthetic_context(self) -> None:
        await self.application.update_working_set(
            UpdateWorkingSetRequest(
                self.session_id,
                _update(0, goal=(WorkingSetEntry(f"runtime {self.secret}"),)),
            )
        )
        provider = _RecordingProvider((ModelTextDelta("done"), ModelCompleted("stop")))
        runtime = AgentRuntime(
            provider=provider,
            tools=ToolRegistry(),
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            tool_context=self._context(),
            session_store=self.store,
            working_set=self.application,
            max_steps=1,
        )

        result = await runtime.run("current prompt", session_id=self.session_id)

        self.assertEqual(result.response, "done")
        self.assertGreaterEqual(len(provider.calls), 1)
        working_set_messages = [
            item
            for item in provider.calls[0].messages
            if isinstance(item, Message) and item.synthetic_reason is SyntheticReason.WORKING_SET
        ]
        self.assertEqual(len(working_set_messages), 1)
        self.assertNotIn(self.secret, working_set_messages[0].content)
        durable_items = await self.store.load_session_items(self.session_id)
        self.assertFalse(
            any(
                isinstance(item, Message) and item.synthetic_reason is SyntheticReason.WORKING_SET
                for item in durable_items
            )
        )
        self.assertFalse(any("Structured Working Set" in item.content for item in durable_items))

    async def test_model_tool_is_bound_and_reports_durable_metadata(self) -> None:
        tool = SessionWorkingSetTool(self.application)
        self.assertFalse(tool.side_effecting)
        self.assertIs(tool.definition.execution_mode, ToolExecutionMode.EXCLUSIVE)
        self.assertNotIn("session_id", tool.definition.input_schema["properties"])
        arguments = {
            "operation": "update",
            "expected_revision": 0,
            "sections": {
                section.value: (
                    [{"text": f"goal {self.secret}"}] if section is WorkingSetSection.GOAL else []
                )
                for section in WORKING_SET_SECTION_ORDER
            },
        }
        updated = await tool.execute(arguments, self._context())
        self.assertFalse(updated.is_error)
        self.assertIsNotNone(updated.metadata)
        assert updated.metadata is not None
        self.assertTrue(updated.metadata["durable_write"])
        self.assertNotIn(self.secret, updated.content)

        read = await tool.execute({"operation": "read"}, self._context())
        self.assertIn('"revision":1', read.content)
        self.assertNotIn("session_id", read.content)
        with self.assertRaises(ToolError):
            await tool.execute(None, self._context())
        with self.assertRaises(ToolError):
            await tool.execute({"operation": 1}, self._context())
        with self.assertRaises(ToolError):
            await tool.execute({"operation": "unknown"}, self._context())
        with self.assertRaises(ToolError):
            await tool.execute({"operation": "read", "extra": True}, self._context())
        with self.assertRaises(ToolError):
            await tool.execute(
                {"operation": "update", "expected_revision": 0, "sections": {}},
                self._context(),
            )
        with self.assertRaises(ToolError):
            await tool.execute(
                {**arguments, "unexpected": True},
                self._context(),
            )
        with self.assertRaises(ToolError):
            await tool.execute(
                {**arguments, "expected_revision": "zero"},
                self._context(),
            )
        with self.assertRaises(ToolError):
            await tool.execute({"operation": "read"}, self._context(output_byte_limit=0))
        with self.assertRaises(ToolError):
            await tool.execute(
                {"operation": "read", "session_id": "other-session"},
                self._context(),
            )
        with self.assertRaises(ToolError):
            await tool.execute({"operation": "read"}, ToolContext(Path(self._temporary.name)))
        limited = await tool.execute(
            {"operation": "read"},
            self._context(output_byte_limit=16),
        )
        self.assertTrue(limited.is_error)
        self.assertEqual(limited.metadata and limited.metadata["failure_kind"], "output_limit")

        registry = default_tool_registry(
            allowed_tool_names=("session_working_set",),
            session_working_set=self.application,
        )
        self.assertIsInstance(registry.get("session_working_set"), SessionWorkingSetTool)
        self.assertIsNone(
            default_tool_registry(allowed_tool_names=("session_working_set",)).get(
                "session_working_set"
            )
        )

    async def test_update_returns_success_ack_when_snapshot_projection_does_not_fit(self) -> None:
        tool = SessionWorkingSetTool(self.application)
        entry_text = "working-set-entry-" + ("x" * 120)
        arguments = {
            "operation": "update",
            "expected_revision": 0,
            "sections": {
                section.value: (
                    [{"text": entry_text} for _ in range(4)]
                    if section is WorkingSetSection.PROGRESS
                    else []
                )
                for section in WORKING_SET_SECTION_ORDER
            },
        }

        result = await tool.execute(arguments, self._context(output_byte_limit=256))

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["operation"], "update")
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["entry_count"], 4)
        self.assertEqual(payload["text_bytes"], len(entry_text.encode("utf-8")) * 4)
        self.assertTrue(payload["durable_write"])
        self.assertTrue(payload["snapshot_omitted"])
        self.assertNotIn("sections", payload)
        self.assertIsNotNone(result.metadata)
        assert result.metadata is not None
        self.assertTrue(result.metadata["durable_write"])
        self.assertTrue(result.metadata["snapshot_omitted"])
        committed = await self.application.read_working_set(ReadWorkingSetRequest(self.session_id))
        self.assertEqual(committed.revision, 1)
        self.assertEqual(committed.entry_count, 4)

    async def test_update_rejects_before_mutation_when_acknowledgement_does_not_fit(self) -> None:
        tool = SessionWorkingSetTool(self.application)
        before = await self.application.update_working_set(
            UpdateWorkingSetRequest(
                self.session_id,
                _update(0, goal=(WorkingSetEntry("stable state"),)),
            )
        )
        arguments = {
            "operation": "update",
            "expected_revision": 1,
            "sections": {
                section.value: (
                    [{"text": "replacement"}] if section is WorkingSetSection.GOAL else []
                )
                for section in WORKING_SET_SECTION_ORDER
            },
        }

        with self.assertRaises(ToolError):
            await tool.execute(arguments, self._context(output_byte_limit=32))

        after = await self.application.read_working_set(ReadWorkingSetRequest(self.session_id))
        self.assertEqual(after, before)

    async def test_low_limit_read_remains_error_without_mutation(self) -> None:
        tool = SessionWorkingSetTool(self.application)
        before = await self.application.update_working_set(
            UpdateWorkingSetRequest(
                self.session_id,
                _update(0, goal=(WorkingSetEntry("read-only state"),)),
            )
        )

        result = await tool.execute(
            {"operation": "read"},
            self._context(output_byte_limit=16),
        )

        self.assertTrue(result.is_error)
        self.assertEqual(result.metadata and result.metadata["failure_kind"], "output_limit")
        after = await self.application.read_working_set(ReadWorkingSetRequest(self.session_id))
        self.assertEqual(after, before)

    async def test_fork_and_subagent_sessions_start_with_isolated_empty_working_sets(self) -> None:
        parent = await self.application.update_working_set(
            UpdateWorkingSetRequest(
                self.session_id,
                _update(0, goal=(WorkingSetEntry("parent state"),)),
            )
        )
        forked_id = await self.store.fork_session(self.session_id)
        subagent_id = await self.store.create_session(
            self._temporary.name,
            "fixture-provider",
            "fixture-model",
        )
        child_application = SessionApplicationService(
            self.store,
            redaction_values=(self.secret,),
        )
        forked_empty = await child_application.read_working_set(ReadWorkingSetRequest(forked_id))
        subagent_empty = await child_application.read_working_set(
            ReadWorkingSetRequest(subagent_id)
        )
        self.assertTrue(forked_empty.is_empty)
        self.assertTrue(subagent_empty.is_empty)
        self.assertEqual(forked_empty.revision, 0)
        self.assertEqual(subagent_empty.revision, 0)

        forked_state = await child_application.update_working_set(
            UpdateWorkingSetRequest(
                forked_id,
                _update(0, goal=(WorkingSetEntry("child state"),)),
            )
        )
        self.assertEqual(forked_state.revision, 1)
        self.assertEqual(
            await self.application.read_working_set(ReadWorkingSetRequest(self.session_id)),
            parent,
        )
        self.assertTrue(
            (await child_application.read_working_set(ReadWorkingSetRequest(subagent_id))).is_empty
        )

    async def test_schema_30_migrates_the_additive_working_set_table(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE session_working_sets")
            connection.execute("UPDATE schema_meta SET version = 30 WHERE singleton = 1")
            connection.commit()

        migrated = SqliteSessionStore(self.database)
        await migrated.initialize()
        self.assertEqual(SCHEMA_VERSION, 35)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT version FROM schema_meta").fetchone(),
                (35,),
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'session_working_sets'"
                ).fetchone()
            )
        self.assertTrue(
            (
                await SessionApplicationService(migrated).read_working_set(
                    ReadWorkingSetRequest(self.session_id)
                )
            ).is_empty
        )


if __name__ == "__main__":
    unittest.main()
