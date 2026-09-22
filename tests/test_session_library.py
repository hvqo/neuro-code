"""Session library application-service tests.

会话库应用服务测试.

The service is exercised against the real SQLite store so the project schema,
the active-session guard, and the delegation to the conversation owner are all
covered by one boundary.

本测试针对真实 SQLite 存储执行服务,从而用同一个边界覆盖项目表结构、当前会话保护以及
对会话所有者的委托.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.memory.project_memory_extraction import ProjectMemoryExtractionManager
from neuro_code.application.sessions.contracts import (
    NewSessionResult,
    SessionOption,
)
from neuro_code.application.sessions.library import SessionLibraryService
from neuro_code.domain.memory import ProjectMemory, ProjectMemoryMetadata, ProjectMemoryType
from neuro_code.domain.sessions import SessionSummary
from neuro_code.infrastructure.persistence.project_memory_files import FileProjectMemoryStore
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError


class LibraryOwnerFixture:
    """Minimal conversation owner used to observe delegated lifecycle calls."""

    def __init__(self, *, session_id: str | None = None) -> None:
        self._session_id = session_id
        self.options: tuple[SessionOption, ...] = ()
        self.queries: list[str | None] = []
        self.new_session_calls = 0
        self.active_project_id: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]:
        self.queries.append(query)
        return self.options

    @asynccontextmanager
    async def session_project_lifecycle(
        self,
        session_id: str,
        project_id: str | None,
    ) -> AsyncIterator[None]:
        yield
        if session_id == self._session_id:
            self.active_project_id = project_id

    @asynccontextmanager
    async def project_lifecycle(self, project_id: str) -> AsyncIterator[None]:
        yield
        if self.active_project_id == project_id:
            self.active_project_id = None

    async def start_new_session(self, project_id: str | None = None) -> NewSessionResult:
        self.active_project_id = project_id
        self.new_session_calls += 1
        previous = self._session_id
        self._session_id = None
        return NewSessionResult(previous)


class SessionLibraryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_projects_and_sessions_support_full_management_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            memory_store = FileProjectMemoryStore(Path(directory) / "state")
            memory_lifecycle = ProjectMemoryExtractionManager(store, memory_store)
            open_session = await store.create_session("/workspace", "provider", "model")
            other_session = await store.create_session("/workspace", "provider", "model")
            owner = LibraryOwnerFixture(session_id=open_session)
            service = SessionLibraryService(
                store,
                owner=owner,
                memory_store=memory_store,
                memory_lifecycle=memory_lifecycle,
            )

            self.assertEqual(service.active_session_id(), open_session)

            project = await service.create_project("Alpha", "/workspace")
            self.assertEqual([item.id for item in await service.list_projects()], [project.id])
            self.assertEqual(
                (await service.rename_project(project.id, "Beta")).name,
                "Beta",
            )

            assigned = await service.assign_session_project(other_session, project.id)
            self.assertEqual(assigned.project_id, project.id)
            self.assertEqual(
                (await service.assign_session_project(other_session, None)).project_id,
                None,
            )
            active_assignment = await service.assign_session_project(open_session, project.id)
            self.assertEqual(active_assignment.project_id, project.id)
            self.assertEqual(owner.active_project_id, project.id)
            now = datetime.now(UTC)
            memory = ProjectMemory(
                ProjectMemoryMetadata(
                    memory_id="mem-" + "a" * 32,
                    name="Architecture decision",
                    description="Why: recovery. How to apply: check project state.",
                    type=ProjectMemoryType.PROJECT,
                    origin_session_id=open_session,
                    created_at=now,
                    updated_at=now,
                ),
                "The project chose a durable owner.",
            )
            memory_store.upsert_memory(project.id, memory)

            renamed = await service.rename_session(other_session, "Renamed by library")
            self.assertEqual(renamed.title, "Renamed by library")
            self.assertEqual(
                (await store.get_session(other_session)).title,
                "Renamed by library",
            )

            renamed_project = await service.rename_project(project.id, "Gamma")
            self.assertEqual(renamed_project.id, project.id)
            self.assertEqual(
                memory_store.read_memory(project.id, memory.metadata.memory_id).content,
                memory.content,
            )

            await service.delete_project(project.id)
            self.assertEqual(await service.list_projects(), ())
            self.assertFalse((Path(directory) / "state" / "project-memory" / project.id).exists())
            self.assertIsNone(owner.active_project_id)
            self.assertEqual(
                (await store.get_session(other_session)).project_id,
                None,
            )
            self.assertIsNone((await store.get_session(open_session)).project_id)

            await service.delete_session(other_session)
            with self.assertRaisesRegex(Exception, "unknown session"):
                await store.get_session(other_session)

    async def test_deleting_the_open_session_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            open_session = await store.create_session("/workspace", "provider", "model")
            service = SessionLibraryService(
                store, owner=LibraryOwnerFixture(session_id=open_session)
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "cannot delete the session that is currently open",
            ):
                await service.delete_session(open_session)

            self.assertEqual((await store.get_session(open_session)).id, open_session)

    async def test_listing_and_new_session_delegate_to_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            owner = LibraryOwnerFixture(session_id="open-session")
            service = SessionLibraryService(store, owner=owner)
            timestamp = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
            owner.options = (
                SessionOption(
                    "open-session",
                    "provider",
                    "model",
                    timestamp,
                    "provider",
                    True,
                    True,
                    True,
                    title="Open session",
                ),
            )

            options = await service.list_sessions("open")
            self.assertEqual([option.session_id for option in options], ["open-session"])
            self.assertEqual(owner.queries, ["open"])

            result = await service.start_new_session()
            self.assertEqual(result.previous_session_id, "open-session")
            self.assertEqual(owner.new_session_calls, 1)
            self.assertIsNone(service.active_session_id())

    async def test_missing_owner_disables_listing_and_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "sessions.db")
            await store.initialize()
            service = SessionLibraryService(store)

            self.assertIsNone(service.active_session_id())
            with self.assertRaisesRegex(ConfigurationError, "listing is unavailable"):
                await service.list_sessions()
            with self.assertRaisesRegex(ConfigurationError, "new-session is unavailable"):
                await service.start_new_session()

            # Projects stay available without a bound conversation.
            project = await service.create_project("Standalone", "/workspace")
            self.assertEqual(project.name, "Standalone")


class SessionProjectModelTests(unittest.TestCase):
    def test_project_names_are_normalized_and_bounded(self) -> None:
        from neuro_code.domain.sessions import MAX_PROJECT_NAME_CHARS, normalize_project_name

        self.assertEqual(normalize_project_name("  Release   train  "), "Release train")
        self.assertEqual(
            len(normalize_project_name("x" * (MAX_PROJECT_NAME_CHARS + 50))),
            MAX_PROJECT_NAME_CHARS,
        )
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            normalize_project_name("   ")
        with self.assertRaisesRegex(ValueError, "NUL"):
            normalize_project_name("bad\u0000name")

    def test_session_summary_rejects_an_empty_project_id(self) -> None:
        timestamp = datetime(2026, 9, 21, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "project id must not be empty"):
            SessionSummary(
                "session",
                "/workspace",
                "provider",
                "model",
                timestamp,
                timestamp,
                project_id="",
            )
