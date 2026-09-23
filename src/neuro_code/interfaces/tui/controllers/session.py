from __future__ import annotations

from neuro_code.application.sessions.contracts import NewSessionResult
from neuro_code.domain.conversation.context import estimate_context_tokens
from neuro_code.interfaces.tui.contracts import SessionLibraryController
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.screens import (
    LibraryActionKind,
    SessionLibraryAction,
    SessionLibraryProjection,
    SessionLibraryScreen,
    SessionSelectionScreen,
)
from neuro_code.interfaces.tui.text import ui_text


class SessionControllerMixin(TuiAppControllerMixin):
    async def action_select_session(self) -> None:
        await self._select_session(None)

    async def _select_session(
        self,
        requested: str | None,
        *,
        query: str | None = None,
    ) -> None:
        controller = self._session_selection_owner()
        if controller is None:
            self._write_ui_entry("error", "session.resume_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.resume_running")
            return
        if requested is not None:
            await self._apply_session_selection(requested)
            return
        try:
            options = await controller.list_sessions(query)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not options:
            if query is None:
                self._write_ui_entry("status", "session.none")
            else:
                self._write_ui_entry(
                    "status",
                    "session.none_matching",
                    query=query,
                )
            return
        self.push_screen(
            SessionSelectionScreen(
                options,
                query=query,
                language=self._language,
                search_callback=controller.list_sessions,
            ),
            self._session_selected,
        )

    async def _rename_session(self, title: str) -> None:
        controller = self._session_selection_owner()
        if controller is None:
            self._write_ui_entry("error", "session.rename_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.rename_running")
            return
        try:
            summary = await controller.rename_session(title)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_ui_entry(
            "status",
            "session.renamed",
            session_id=summary.id,
            title=summary.title,
        )

    async def _session_selected(self, session_id: str | None) -> None:
        if session_id is not None:
            await self._apply_session_selection(session_id)

    async def _apply_session_selection(self, session_id: str) -> None:
        controller = self._session_selection_owner()
        assert controller is not None
        try:
            result = await controller.select_session(session_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._provider_name = result.provider_name
        self._model_name = result.model_name
        if self._plan_controller is not None:
            self._plan = self._plan_controller.plan
        self._context_window_tokens = result.context_window_tokens
        self._context_preflight_status = None
        self._context_preflight_capacity_tokens = None
        self._context_preflight_total_tokens = False
        self._context_preflight_notice = None
        if result.changed:
            self._context_used_tokens = estimate_context_tokens(result.items)
            self._context_usage_estimated = True
        self._refresh_runtime_bar()
        if not result.changed:
            self._write_ui_entry(
                "status",
                "session.already_open",
                session_id=result.session_id,
            )
            await self._announce_recovery_state()
            return

        self._queued_interjections.clear()
        self._reset_background_task_tracking()
        await self._ensure_background_wake_state()
        self._replace_transcript(result.items)
        self._execution_record = self._session_execution_record()
        profile_note = (
            ui_text(
                self._language,
                "session.profile",
                profile=result.profile_name,
            )
            if result.source_profile_match
            else ui_text(
                self._language,
                "session.profile_unavailable",
                profile=result.profile_name,
                source=result.source_provider,
            )
        )
        previous_note = (
            ui_text(
                self._language,
                "session.previous_saved",
                session_id=result.previous_session_id,
            )
            if result.previous_session_id is not None
            else ""
        )
        self._write_ui_entry(
            "system",
            "session.resumed",
            session_id=result.session_id,
            profile_note=profile_note,
            provider=result.provider_name,
            model=result.model_name,
            previous=previous_note,
            stopped=self._stopped_task_note(result.stopped_background_tasks),
        )
        self._write_recoverable_resume_notice(self._execution_record)
        await self._announce_recovery_state()

    def _session_library(self) -> SessionLibraryController | None:
        """Return the bounded session/project management boundary.

        返回有界的会话/项目管理边界."""

        return self._session_library_service

    async def _open_session_library(
        self,
        *,
        query: str | None = None,
        view: str = "sessions",
    ) -> None:
        """Open the session and project manager at one view.

        在指定视图打开会话与项目管理器."""

        library = self._session_library()
        if library is None:
            await self._select_session(None, query=query)
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.resume_running")
            return
        try:
            options = await library.list_sessions(query)
            projects = await library.list_projects()
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self.push_screen(
            SessionLibraryScreen(
                options,
                projects,
                current_session_id=library.active_session_id(),
                query=query,
                view=view,
                language=self._language,
                mutation_handler=self._session_library_mutation,
            ),
            self._session_library_action,
        )

    async def _session_library_action(self, action: SessionLibraryAction | None) -> None:
        """Handle the manager's terminal actions.

        处理管理器的终态动作.

        Mutations never reach this callback: the screen performs them in place
        through ``_session_library_mutation`` so the manager is not closed and
        re-opened for every edit (which used to flash the main screen).

        变更不会到达此回调:屏幕通过 ``_session_library_mutation`` 就地执行它们,因此管理器不会
        为每次编辑而关闭并重新打开(此前会闪一下主界面).
        """

        if action is None or action.kind is LibraryActionKind.CLOSE:
            return
        if action.kind is LibraryActionKind.OPEN_SESSION:
            if action.session_id is not None:
                await self._apply_session_selection(action.session_id)
            return
        if action.kind is LibraryActionKind.NEW_SESSION:
            library = self._session_library()
            if library is None:
                return
            try:
                await self._apply_session_library_mutation(library, action)
            except Exception as error:
                self._write_entry("error", f"{type(error).__name__}: {error}")

    async def _session_library_mutation(
        self,
        action: SessionLibraryAction,
    ) -> SessionLibraryProjection | None:
        """Apply one in-place mutation and return the refreshed projection.

        执行一次就地变更并返回刷新后的投影."""

        library = self._session_library()
        if library is None:
            return None
        try:
            await self._apply_session_library_mutation(library, action)
            options = await library.list_sessions()
            projects = await library.list_projects()
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return None
        return SessionLibraryProjection(
            options,
            projects,
            library.active_session_id(),
        )

    async def _apply_session_library_mutation(
        self,
        library: SessionLibraryController,
        action: SessionLibraryAction,
    ) -> None:
        kind = action.kind
        if kind is LibraryActionKind.NEW_SESSION:
            result = await library.start_new_session(action.project_id)
            await self._apply_new_session(result)
        elif kind is LibraryActionKind.RENAME_SESSION:
            assert action.session_id is not None
            summary = await library.rename_session(action.session_id, action.name or "")
            self._write_ui_entry(
                "status",
                "session.renamed",
                session_id=summary.id,
                title=summary.title,
            )
        elif kind is LibraryActionKind.DELETE_SESSION:
            assert action.session_id is not None
            await library.delete_session(action.session_id)
            self._write_ui_entry(
                "status",
                "library.status.session_deleted",
                session=action.session_id[:12],
            )
        elif kind is LibraryActionKind.ASSIGN_PROJECT:
            assert action.session_id is not None
            summary = await library.assign_session_project(action.session_id, action.project_id)
            project_name = next(
                (
                    item.name
                    for item in await library.list_projects()
                    if item.id == summary.project_id
                ),
                None,
            )
            if project_name is None:
                self._write_ui_entry("status", "library.status.session_detached")
            else:
                self._write_ui_entry(
                    "status",
                    "library.status.session_assigned",
                    project=project_name,
                )
        elif kind is LibraryActionKind.CREATE_PROJECT:
            created = await library.create_project(action.name or "", str(self._cwd))
            self._write_ui_entry(
                "status",
                "library.status.project_created",
                name=created.name,
            )
        elif kind is LibraryActionKind.RENAME_PROJECT:
            assert action.project_id is not None
            renamed = await library.rename_project(action.project_id, action.name or "")
            self._write_ui_entry(
                "status",
                "library.status.project_renamed",
                name=renamed.name,
            )
        elif kind is LibraryActionKind.DELETE_PROJECT:
            assert action.project_id is not None
            projects = await library.list_projects()
            deleted_name = next(
                (item.name for item in projects if item.id == action.project_id),
                action.project_id,
            )
            await library.delete_project(action.project_id)
            self._write_ui_entry(
                "status",
                "library.status.project_deleted",
                name=deleted_name,
            )

    async def _apply_new_session(self, result: NewSessionResult) -> None:
        """Reset the interface onto the freshly bound session.

        将界面重置到新绑定的会话上."""

        self._plan = self._plan_controller.plan if self._plan_controller is not None else None
        self._plan_comments = ()
        self._plan_entry_index = None
        self._context_preflight_status = None
        self._context_preflight_capacity_tokens = None
        self._context_preflight_total_tokens = False
        self._context_preflight_notice = None
        self._context_used_tokens = 0
        self._context_usage_estimated = False
        self._queued_interjections.clear()
        self._reset_background_task_tracking()
        await self._ensure_background_wake_state()
        self._replace_transcript(())
        self._execution_record = self._session_execution_record()
        self._refresh_runtime_bar()
        self._write_ui_entry(
            "system",
            "library.status.new_session",
            previous=(
                ui_text(
                    self._language,
                    "session.previous_saved",
                    session_id=result.previous_session_id,
                )
                if result.previous_session_id is not None
                else ""
            ),
            stopped=self._stopped_task_note(result.stopped_background_tasks),
        )
