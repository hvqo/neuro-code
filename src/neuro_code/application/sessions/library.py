"""Application-owned session and project library management.

应用层拥有的会话与项目库管理.

Projects are an optional grouping over sessions: a session may belong to at most
one project, and it may belong to none.  This service owns the bounded CRUD
vocabulary an inbound interface needs, while two invariants stay with their
existing owners:

* starting a fresh session replaces the conversation binding, so it is delegated
  to the profile controller;
* deleting the session that is currently bound is refused, because removal would
  silently unload the open conversation.

项目是会话的可选分组:会话最多归属一个项目,也可以不归属任何项目.本服务拥有入站接口所需的
有界 CRUD 词汇,同时两条不变量仍归既有所有者:

* 开启全新会话会替换会话绑定,因此委托给 profile 控制器;
* 删除当前已绑定的会话会被拒绝,因为删除会静默卸载正在打开的会话.
"""

from __future__ import annotations

from typing import Protocol

from neuro_code.application.sessions.contracts import NewSessionResult, SessionOption
from neuro_code.domain.sessions import SessionProject, SessionSummary
from neuro_code.shared.errors import ConfigurationError

MAX_LIBRARY_PROJECTS = 200
MAX_LIBRARY_SESSIONS = 200

__all__ = [
    "MAX_LIBRARY_PROJECTS",
    "MAX_LIBRARY_SESSIONS",
    "SessionLibraryOwner",
    "SessionLibraryService",
    "SessionLibraryStore",
]


class SessionLibraryStore(Protocol):
    """Durable project and session operations the library depends on.

    库依赖的持久化项目与会话操作."""

    async def list_projects(self, *, limit: int = MAX_LIBRARY_PROJECTS) -> list[SessionProject]: ...

    async def create_project(self, name: str, cwd: str) -> SessionProject: ...

    async def rename_project(self, project_id: str, name: str) -> SessionProject: ...

    async def delete_project(self, project_id: str) -> None: ...

    async def assign_session_project(
        self,
        session_id: str,
        project_id: str | None,
    ) -> SessionSummary: ...

    async def update_session_title(self, session_id: str, title: str) -> SessionSummary: ...

    async def delete_session(self, session_id: str) -> None: ...


class SessionLibraryOwner(Protocol):
    """Conversation lifecycle operations the library delegates to.

    库委托的会话生命周期操作."""

    @property
    def session_id(self) -> str | None: ...

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]: ...

    async def start_new_session(self) -> NewSessionResult: ...


class SessionLibraryService:
    """Expose bounded session/project management without owning lifecycle.

    暴露有界的会话/项目管理能力,但不拥有生命周期.

    The service is intentionally thin: it validates the caller's intent, applies
    the active-session guard, and delegates the write to the durable store or to
    the profile controller.  It never caches projects or sessions.

    本服务有意保持轻薄:校验调用方意图、实施当前会话保护,并把写入委托给持久化存储或
    profile 控制器;它不缓存项目或会话.
    """

    __slots__ = ("_owner", "_store")

    def __init__(
        self,
        store: SessionLibraryStore,
        *,
        owner: SessionLibraryOwner | None = None,
    ) -> None:
        self._store = store
        self._owner = owner

    def active_session_id(self) -> str | None:
        """Return the currently bound session id, when one exists.

        返回当前已绑定的会话 ID(若存在)."""

        return None if self._owner is None else self._owner.session_id

    async def list_projects(self) -> tuple[SessionProject, ...]:
        """List projects newest-updated first.

        按最近更新顺序列出项目."""

        projects = await self._store.list_projects(limit=MAX_LIBRARY_PROJECTS)
        return tuple(projects)

    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]:
        """List the workspace sessions the resume path can open.

        列出恢复路径可以打开的当前工作区会话."""

        if self._owner is None:
            raise ConfigurationError("session library listing is unavailable")
        return await self._owner.list_sessions(query)

    async def create_project(self, name: str, cwd: str) -> SessionProject:
        """Create one project rooted at the caller's workspace.

        在调用方工作区下创建一个项目."""

        return await self._store.create_project(name, cwd)

    async def rename_project(self, project_id: str, name: str) -> SessionProject:
        """Rename one project.

        重命名一个项目."""

        return await self._store.rename_project(project_id, name)

    async def delete_project(self, project_id: str) -> None:
        """Delete one project, keeping its sessions and only detaching them.

        删除项目并保留其会话,只解除归属."""

        await self._store.delete_project(project_id)

    async def rename_session(self, session_id: str, title: str) -> SessionSummary:
        """Rename any workspace session, including the open one.

        重命名任意会话,包括当前打开的会话."""

        return await self._store.update_session_title(session_id, title)

    async def assign_session_project(
        self,
        session_id: str,
        project_id: str | None,
    ) -> SessionSummary:
        """Attach one session to a project, or detach it with ``None``.

        将会话归属到项目;传入 None 表示解除归属."""

        return await self._store.assign_session_project(session_id, project_id)

    async def delete_session(self, session_id: str) -> None:
        """Delete one session unless it is the session currently bound.

        删除一个会话;若它是当前已绑定的会话则拒绝."""

        if self._owner is not None and self._owner.session_id == session_id:
            raise ConfigurationError("cannot delete the session that is currently open")
        await self._store.delete_session(session_id)

    async def start_new_session(self) -> NewSessionResult:
        """Start a fresh session in the current provider profile.

        在当前供应配置下开启全新会话."""

        if self._owner is None:
            raise ConfigurationError("session library new-session is unavailable")
        return await self._owner.start_new_session()
