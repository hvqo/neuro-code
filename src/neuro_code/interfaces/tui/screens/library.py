"""Session and project management screens.

会话与项目管理屏幕.

The library screen owns presentation and one bounded request vocabulary.  It
never writes to storage: every mutation is returned to the controller, which
performs it through the application service and then re-opens this screen.  That
keeps delete/rename/regroup behaviour testable without rendering the whole app.

会话库屏幕只拥有呈现与一份有界请求词汇,不直接写存储:每个变更都返回给控制器,由控制器
经应用服务执行后再重新打开本屏幕,从而让删除/重命名/重新归属的行为可在不渲染整个应用
的前提下测试.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from neuro_code.application.sessions.contracts import SessionOption
from neuro_code.domain.sessions import SessionProject
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.widgets import MenuOptionButton
from neuro_code.shared.ui_language import UiLanguage

__all__ = [
    "ConfirmActionScreen",
    "LibraryActionKind",
    "ProjectPickerScreen",
    "SessionLibraryAction",
    "SessionLibraryMutationHandler",
    "SessionLibraryProjection",
    "SessionLibraryScreen",
    "TextValueScreen",
]


class LibraryActionKind(StrEnum):
    """Requested library operation returned to the controller.

    返回给控制器的库操作请求."""

    OPEN_SESSION = "open-session"
    NEW_SESSION = "new-session"
    RENAME_SESSION = "rename-session"
    DELETE_SESSION = "delete-session"
    ASSIGN_PROJECT = "assign-project"
    CREATE_PROJECT = "create-project"
    RENAME_PROJECT = "rename-project"
    DELETE_PROJECT = "delete-project"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class SessionLibraryAction:
    """One bounded management request.

    一个管理请求."""

    kind: LibraryActionKind
    session_id: str | None = None
    project_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SessionLibraryProjection:
    """Refreshed state handed back to the open manager screen.

    交回已打开管理屏幕的刷新后状态."""

    options: tuple[SessionOption, ...]
    projects: tuple[SessionProject, ...]
    current_session_id: str | None


# A mutation runs while the manager stays open: the controller performs it and
# returns the refreshed projection, so the modal never has to be re-opened.
#
# 变更在管理器保持打开时执行:控制器完成变更并返回刷新后的投影,因此模态无需重新打开.
SessionLibraryMutationHandler = Callable[
    [SessionLibraryAction],
    Awaitable[SessionLibraryProjection | None],
]


class TextValueScreen(ModalScreen[str | None]):
    """Collect one bounded single-line value, or cancel.

    采集一个单行值,或取消."""

    CSS = """
    TextValueScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #text-value-dialog {
        width: 72%;
        max-width: 72;
        height: auto;
        padding: $space-2 $space-3;
        border: round $border;
        background: $surface;
    }

    #text-value-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #text-value-input {
        margin-bottom: 1;
    }

    #text-value-help {
        color: $text-muted;
        margin-bottom: 1;
    }

    #text-value-actions {
        height: auto;
        align-horizontal: right;
        border-top: solid $border;
        padding-top: 1;
    }

    #text-value-actions Button {
        margin-left: 2;
    }

    #text-value-actions Button:first-of-type {
        margin-left: 0;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        title: str,
        *,
        value: str = "",
        language: UiLanguage = UiLanguage.ENGLISH,
        accept_key: str = "library.prompt.save",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.initial_value = value
        self.language = language
        self.accept_key = accept_key

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self.title_text, id="text-value-title"),
            Input(value=self.initial_value, id="text-value-input"),
            Static(ui_text(self.language, "library.prompt.help"), id="text-value-help"),
            Horizontal(
                Button(ui_text(self.language, "library.prompt.cancel"), id="text-value-cancel"),
                Button(
                    ui_text(self.language, self.accept_key),
                    id="text-value-accept",
                    variant="primary",
                ),
                id="text-value-actions",
            ),
            id="text-value-dialog",
            classes="modal-dialog modal-s",
        )

    def on_mount(self) -> None:
        field = self.query_one("#text-value-input", Input)
        field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        self._accept()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "text-value-accept":
            self._accept()
        elif event.button.id == "text-value-cancel":
            self.dismiss(None)

    def _accept(self) -> None:
        value = " ".join(self.query_one("#text-value-input", Input).value.split())
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmActionScreen(ModalScreen[bool]):
    """Ask for an explicit confirmation before a destructive operation.

    在破坏性操作前要求明确确认."""

    CSS = """
    ConfirmActionScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #confirm-action-dialog {
        width: 76%;
        max-width: 76;
        height: auto;
        padding: $space-2 $space-3;
        border: round $border-focus;
        background: $surface;
    }

    #confirm-action-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    #confirm-action-body {
        color: $text-body;
        margin-bottom: 1;
    }

    #confirm-action-actions {
        height: auto;
        align-horizontal: right;
        border-top: solid $border;
        padding-top: 1;
    }

    #confirm-action-actions Button {
        margin-left: 2;
    }

    #confirm-action-actions Button:first-of-type {
        margin-left: 0;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        title: str,
        body: str,
        *,
        language: UiLanguage = UiLanguage.ENGLISH,
        accept_key: str = "library.confirm.delete",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.language = language
        self.accept_key = accept_key

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                f"\N{WARNING SIGN} {self.title_text}",
                id="confirm-action-title",
            ),
            Static(self.body_text, id="confirm-action-body"),
            Horizontal(
                Button(ui_text(self.language, "library.prompt.cancel"), id="confirm-action-cancel"),
                Button(
                    ui_text(self.language, self.accept_key),
                    id="confirm-action-accept",
                    variant="error",
                ),
                id="confirm-action-actions",
            ),
            id="confirm-action-dialog",
            classes="modal-dialog modal-s",
        )

    def on_mount(self) -> None:
        self.query_one("#confirm-action-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-action-accept":
            self.dismiss(True)
        elif event.button.id == "confirm-action-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ProjectPickerScreen(ModalScreen[str | None]):
    """Choose one project for a session, or detach it.

    为会话选择一个项目,或解除归属.

    ``None`` means the caller cancelled; the sentinel ``""`` means detach.
    """

    DETACH = "__detach__"

    CSS = """
    ProjectPickerScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #project-picker-dialog {
        width: 72%;
        max-width: 72;
        height: auto;
        max-height: 80%;
        padding: $space-2 $space-3;
        border: round $border;
        background: $surface;
    }

    #project-picker-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #project-picker-options {
        height: auto;
        max-height: 14;
    }

    #project-picker-options MenuOptionButton {
        width: 100%;
        height: 3;
        margin-bottom: $space-0;
        content-align: left middle;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(self, projects: tuple[SessionProject, ...], *, language: UiLanguage) -> None:
        super().__init__()
        self.projects = projects
        self.language = language

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(ui_text(self.language, "library.picker.title"), id="project-picker-title"),
            VerticalScroll(
                *(
                    MenuOptionButton(
                        project.name,
                        secondary=project.cwd,
                        muted=True,
                        secondary_justify="left",
                        id=f"project-picker-{index}",
                    )
                    for index, project in enumerate(self.projects)
                ),
                MenuOptionButton(
                    ui_text(self.language, "library.picker.detach"),
                    muted=True,
                    secondary_justify="left",
                    id="project-picker-detach",
                ),
                id="project-picker-options",
            ),
            id="project-picker-dialog",
            classes="modal-dialog modal-s",
        )

    def on_mount(self) -> None:
        # The first project keeps focus when one exists; with no projects the
        # always-present detach row stays focusable instead of raising.
        #
        # 存在项目时仍聚焦第一项;没有项目时改为聚焦始终存在的移出行,而不是抛错.
        selector = "#project-picker-0" if self.projects else "#project-picker-detach"
        self.query_one(selector, Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "project-picker-detach":
            self.dismiss(self.DETACH)
            return
        index = button_id.removeprefix("project-picker-")
        if button_id.startswith("project-picker-") and index.isdigit():
            self.dismiss(self.projects[int(index)].id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionLibraryScreen(ModalScreen[SessionLibraryAction | None]):
    """Manage sessions and projects in two switchable views.

    在两个可切换视图中管理会话与项目."""

    CSS = """
    SessionLibraryScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #library-dialog {
        width: 92%;
        max-width: 116;
        height: 86%;
        padding: $space-2 $space-3;
        background: $surface;
        border: round $border;
    }

    #library-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #library-description {
        color: $text-muted;
        margin-bottom: 1;
    }

    #library-tabs {
        height: auto;
        margin-bottom: 1;
    }

    #library-tabs Button {
        width: 1fr;
        margin-right: 1;
    }

    #library-tabs Button:last-of-type {
        margin-right: 0;
    }

    #library-search {
        margin-bottom: 1;
    }

    #library-sessions,
    #library-projects {
        height: 1fr;
    }

    #library-sessions MenuOptionButton,
    #library-projects MenuOptionButton {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        content-align: left middle;
    }

    #library-session-actions,
    #library-project-actions {
        height: auto;
        margin-top: 1;
        border-top: solid $border;
        padding-top: 1;
    }

    #library-session-actions Button,
    #library-project-actions Button {
        margin-right: 1;
    }

    #library-session-actions Button:last-of-type,
    #library-project-actions Button:last-of-type {
        margin-right: 0;
    }

    #library-help {
        color: $text-muted;
        margin-top: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        options: tuple[SessionOption, ...],
        projects: tuple[SessionProject, ...],
        *,
        current_session_id: str | None,
        query: str | None = None,
        view: str = "sessions",
        language: UiLanguage = UiLanguage.ENGLISH,
        mutation_handler: SessionLibraryMutationHandler | None = None,
    ) -> None:
        super().__init__()
        self.options = options
        self.projects = projects
        self.current_session_id = current_session_id
        self.query_text = query or ""
        self.view = view if view in {"sessions", "projects"} else "sessions"
        self.language = language
        self.mutation_handler = mutation_handler
        self.selected_session_id: str | None = None
        self.selected_project_id: str | None = None
        self._selection_ready = False

    async def apply_projection(self, projection: SessionLibraryProjection) -> None:
        """Adopt refreshed state without leaving the screen.

        在不离开屏幕的前提下采用刷新后的状态."""

        self.options = projection.options
        self.projects = projection.projects
        self.current_session_id = projection.current_session_id
        self._sync_selection()
        await self._refresh_rows()

    async def _mutate(self, action: SessionLibraryAction) -> None:
        """Run one mutation through the controller and re-render in place.

        通过控制器执行一次变更并就地重绘.

        Without a handler the screen keeps its original contract of returning the
        request to its caller instead of performing it.

        没有处理器时,屏幕保持原有契约:将请求返回给调用方而不是自行执行."""

        handler = self.mutation_handler
        if handler is None:
            self.dismiss(action)
            return
        projection = await handler(action)
        if projection is not None:
            await self.apply_projection(projection)

    def _project_name(self, project_id: str | None) -> str:
        if project_id is None:
            return ui_text(self.language, "library.project.none")
        for project in self.projects:
            if project.id == project_id:
                return project.name
        return ui_text(self.language, "library.project.missing")

    def _visible_options(self) -> tuple[SessionOption, ...]:
        query = self.query_text.strip().casefold()
        if not query:
            return self.options
        matched: list[SessionOption] = []
        for option in self.options:
            haystack = " ".join(
                (
                    option.title or "",
                    option.session_id,
                    option.source_provider,
                    option.source_model,
                    self._project_name(option.project_id),
                )
            ).casefold()
            if query in haystack:
                matched.append(option)
        return tuple(matched)

    def _session_label(self, option: SessionOption) -> tuple[str, str]:
        timestamp = option.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        title = option.title or ui_text(self.language, "library.session.untitled")
        markers = [
            self._project_name(option.project_id),
            timestamp,
            f"{option.source_provider}/{option.source_model}",
        ]
        if option.current:
            markers.insert(0, ui_text(self.language, "library.session.current"))
        return title, " · ".join(markers)

    def _session_rows(self) -> list[MenuOptionButton]:
        return [
            MenuOptionButton(
                primary,
                secondary=secondary,
                selected=option.session_id == self.selected_session_id,
                primary_width=30,
                secondary_justify="left",
                id=f"library-session-{index}",
            )
            for index, option in enumerate(self._visible_options())
            for primary, secondary in (self._session_label(option),)
        ]

    def _project_rows(self) -> list[MenuOptionButton]:
        counts: dict[str | None, int] = {}
        for option in self.options:
            counts[option.project_id] = counts.get(option.project_id, 0) + 1
        return [
            MenuOptionButton(
                project.name,
                secondary=f"{counts.get(project.id, 0)}"
                f" {ui_text(self.language, 'library.project.sessions')} · {project.cwd}",
                selected=project.id == self.selected_project_id,
                primary_width=30,
                secondary_justify="left",
                id=f"library-project-{index}",
            )
            for index, project in enumerate(self.projects)
        ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(ui_text(self.language, "library.title"), id="library-title"),
            Static(ui_text(self.language, "library.description"), id="library-description"),
            Horizontal(
                Button(ui_text(self.language, "library.tab.sessions"), id="library-tab-sessions"),
                Button(ui_text(self.language, "library.tab.projects"), id="library-tab-projects"),
                id="library-tabs",
            ),
            Input(
                value=self.query_text,
                placeholder=ui_text(self.language, "library.search"),
                id="library-search",
            ),
            VerticalScroll(
                *self._session_rows(),
                Static(
                    ui_text(self.language, "library.sessions.empty"), id="library-sessions-empty"
                ),
                id="library-sessions",
            ),
            Horizontal(
                Button(
                    ui_text(self.language, "library.action.open"),
                    id="library-open",
                    variant="primary",
                ),
                Button(
                    ui_text(self.language, "library.action.rename"), id="library-rename-session"
                ),
                Button(ui_text(self.language, "library.action.move"), id="library-move-session"),
                Button(
                    ui_text(self.language, "library.action.delete"), id="library-delete-session"
                ),
                Button(
                    ui_text(self.language, "library.action.new_session"), id="library-new-session"
                ),
                id="library-session-actions",
            ),
            VerticalScroll(
                *self._project_rows(),
                Static(
                    ui_text(self.language, "library.projects.empty"), id="library-projects-empty"
                ),
                id="library-projects",
            ),
            Horizontal(
                Button(
                    ui_text(self.language, "library.action.show_sessions"),
                    id="library-project-sessions",
                ),
                Button(
                    ui_text(self.language, "library.action.rename"), id="library-rename-project"
                ),
                Button(
                    ui_text(self.language, "library.action.delete"), id="library-delete-project"
                ),
                Button(
                    ui_text(self.language, "library.action.new_project"), id="library-new-project"
                ),
                id="library-project-actions",
            ),
            Static(ui_text(self.language, "library.help"), id="library-help"),
            id="library-dialog",
            classes="modal-dialog modal-l",
        )

    async def on_mount(self) -> None:
        self._selection_ready = True
        self._sync_selection()
        await self._show_view(self.view)

    def _sync_selection(self) -> None:
        visible = self._visible_options()
        if self.selected_session_id not in {option.session_id for option in visible}:
            current = next(
                (option for option in visible if option.session_id == self.current_session_id),
                None,
            )
            self.selected_session_id = (
                current.session_id if current else (visible[0].session_id if visible else None)
            )
        if self.selected_project_id not in {project.id for project in self.projects}:
            self.selected_project_id = self.projects[0].id if self.projects else None

    async def _show_view(self, view: str) -> None:
        self.view = view
        sessions_view = view == "sessions"
        self.query_one("#library-search", Input).display = sessions_view
        self.query_one("#library-sessions").display = sessions_view
        self.query_one("#library-session-actions").display = sessions_view
        self.query_one("#library-projects").display = not sessions_view
        self.query_one("#library-project-actions").display = not sessions_view
        self.query_one("#library-tab-sessions", Button).variant = (
            "primary" if sessions_view else "default"
        )
        self.query_one("#library-tab-projects", Button).variant = (
            "default" if sessions_view else "primary"
        )
        await self._refresh_rows()

    async def _refresh_rows(self) -> None:
        """Rebuild both row lists from the current projections.

        Rows are rebuilt because ``MenuOptionButton`` renders its selected state
        once at construction; the removal must complete before the replacement
        rows mount so that widget identifiers stay unique.

        行会被重建,因为 MenuOptionButton 在构造时一次性渲染选中状态;
        必须等移除完成后再挂载新行,以保证组件标识唯一."""

        sessions = self.query_one("#library-sessions", VerticalScroll)
        projects = self.query_one("#library-projects", VerticalScroll)
        await sessions.remove_children(
            [child for child in sessions.children if isinstance(child, MenuOptionButton)]
        )
        await projects.remove_children(
            [child for child in projects.children if isinstance(child, MenuOptionButton)]
        )
        rows = self._session_rows()
        if rows:
            await sessions.mount_all(rows, before=0)
        project_rows = self._project_rows()
        if project_rows:
            await projects.mount_all(project_rows, before=0)
        self.query_one("#library-sessions-empty", Static).display = not rows
        self.query_one("#library-projects-empty", Static).display = not project_rows
        self._sync_actions()

    def _sync_actions(self) -> None:
        has_session = self.selected_session_id is not None
        for action_id in (
            "library-open",
            "library-rename-session",
            "library-move-session",
            "library-delete-session",
        ):
            self.query_one(f"#{action_id}", Button).disabled = not has_session
        has_project = self.selected_project_id is not None
        for action_id in (
            "library-project-sessions",
            "library-rename-project",
            "library-delete-project",
        ):
            self.query_one(f"#{action_id}", Button).disabled = not has_project

    async def _select_session(self, session_id: str) -> None:
        self.selected_session_id = session_id
        await self._refresh_rows()

    async def _select_project(self, project_id: str) -> None:
        self.selected_project_id = project_id
        await self._refresh_rows()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "library-search":
            return
        self.query_text = event.value
        await self._refresh_rows()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        # Action buttons are matched exactly before the index-suffixed rows, or a
        # longer action id would be parsed as a row index (``library-project-sessions``
        # starts with the row prefix ``library-project-``).
        #
        # 操作按钮先精确匹配,再匹配带序号的行,否则更长的操作 ID 会被当作行序号解析
        # (``library-project-sessions`` 以行前缀 ``library-project-`` 开头).
        if button_id == "library-tab-sessions":
            await self._show_view("sessions")
            return
        if button_id == "library-tab-projects":
            await self._show_view("projects")
            return
        if button_id == "library-open":
            self._request_session(LibraryActionKind.OPEN_SESSION)
            return
        if button_id == "library-new-session":
            self._request(LibraryActionKind.NEW_SESSION)
            return
        if button_id == "library-rename-session":
            self._prompt_session_name()
            return
        if button_id == "library-move-session":
            self._pick_project()
            return
        if button_id == "library-delete-session":
            self._confirm_delete_session()
            return
        if button_id == "library-project-sessions":
            await self._show_view("sessions")
            return
        if button_id == "library-new-project":
            self._prompt_project_name(None)
            return
        if button_id == "library-rename-project":
            self._prompt_project_name(self.selected_project_id)
            return
        if button_id == "library-delete-project":
            self._confirm_delete_project()
            return
        session_index = button_id.removeprefix("library-session-")
        if button_id.startswith("library-session-") and session_index.isdigit():
            options = self._visible_options()
            index = int(session_index)
            if 0 <= index < len(options):
                await self._select_session(options[index].session_id)
            return
        project_index = button_id.removeprefix("library-project-")
        if button_id.startswith("library-project-") and project_index.isdigit():
            index = int(project_index)
            if 0 <= index < len(self.projects):
                await self._select_project(self.projects[index].id)

    def _request(
        self,
        kind: LibraryActionKind,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        name: str | None = None,
    ) -> None:
        self.dismiss(
            SessionLibraryAction(
                kind,
                session_id=session_id,
                project_id=project_id,
                name=name,
            )
        )

    def _request_session(self, kind: LibraryActionKind) -> None:
        session_id = self.selected_session_id
        if session_id is not None:
            self._request(kind, session_id=session_id)

    def _prompt_session_name(self) -> None:
        session_id = self.selected_session_id
        if session_id is None:
            return
        option = next(
            (item for item in self.options if item.session_id == session_id),
            None,
        )
        self.app.push_screen(
            TextValueScreen(
                ui_text(self.language, "library.prompt.rename_session"),
                value=option.title or "" if option is not None else "",
                language=self.language,
            ),
            partial(self._renamed_session, session_id),
        )

    async def _renamed_session(self, session_id: str, value: str | None) -> None:
        if value is not None:
            await self._mutate(
                SessionLibraryAction(
                    LibraryActionKind.RENAME_SESSION,
                    session_id=session_id,
                    name=value,
                )
            )

    def _pick_project(self) -> None:
        session_id = self.selected_session_id
        if session_id is None:
            return
        self.app.push_screen(
            ProjectPickerScreen(self.projects, language=self.language),
            partial(self._project_picked, session_id),
        )

    async def _project_picked(self, session_id: str, choice: str | None) -> None:
        if choice is None:
            return
        detached = choice == ProjectPickerScreen.DETACH
        await self._mutate(
            SessionLibraryAction(
                LibraryActionKind.ASSIGN_PROJECT,
                session_id=session_id,
                project_id=None if detached else choice,
            )
        )

    def _confirm_delete_session(self) -> None:
        session_id = self.selected_session_id
        if session_id is None:
            return
        option = next(
            (item for item in self.options if item.session_id == session_id),
            None,
        )
        label = (option.title if option is not None and option.title else None) or session_id
        self.app.push_screen(
            ConfirmActionScreen(
                ui_text(self.language, "library.confirm.delete_session.title"),
                ui_text(self.language, "library.confirm.delete_session.body", session=label),
                language=self.language,
            ),
            partial(
                self._declared,
                kind=LibraryActionKind.DELETE_SESSION,
                session_id=session_id,
            ),
        )

    def _prompt_project_name(self, project_id: str | None) -> None:
        project = next(
            (item for item in self.projects if item.id == project_id),
            None,
        )
        key = (
            "library.prompt.rename_project"
            if project_id is not None
            else "library.prompt.new_project"
        )
        self.app.push_screen(
            TextValueScreen(
                ui_text(self.language, key),
                value=project.name if project is not None else "",
                language=self.language,
            ),
            partial(self._project_named, project_id),
        )

    async def _project_named(self, project_id: str | None, value: str | None) -> None:
        if value is None:
            return
        if project_id is None:
            await self._mutate(SessionLibraryAction(LibraryActionKind.CREATE_PROJECT, name=value))
            return
        await self._mutate(
            SessionLibraryAction(
                LibraryActionKind.RENAME_PROJECT,
                project_id=project_id,
                name=value,
            )
        )

    def _confirm_delete_project(self) -> None:
        project_id = self.selected_project_id
        if project_id is None:
            return
        project = next((item for item in self.projects if item.id == project_id), None)
        label = project.name if project is not None else project_id
        self.app.push_screen(
            ConfirmActionScreen(
                ui_text(self.language, "library.confirm.delete_project.title"),
                ui_text(self.language, "library.confirm.delete_project.body", project=label),
                language=self.language,
            ),
            partial(
                self._declared,
                kind=LibraryActionKind.DELETE_PROJECT,
                project_id=project_id,
            ),
        )

    async def _declared(
        self,
        confirmed: bool | None,
        *,
        kind: LibraryActionKind,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        if confirmed:
            await self._mutate(
                SessionLibraryAction(
                    kind,
                    session_id=session_id,
                    project_id=project_id,
                )
            )

    def action_cancel(self) -> None:
        self.dismiss(SessionLibraryAction(LibraryActionKind.CLOSE))
