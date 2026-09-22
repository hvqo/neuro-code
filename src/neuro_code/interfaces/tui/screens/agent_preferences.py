"""Forms for persistent agent execution and tool preferences.

持久化 Agent 执行与工具偏好的编辑表单。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from neuro_code.application.ports.agent_preferences import AgentPreferences
from neuro_code.application.ports.ui_preferences import UiPreferencesStore
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.shared.ui_language import UiLanguage

# Each form edits a vertical user capability while preserving all other preferences.
PREFERENCE_GROUPS: dict[str, tuple[str, ...]] = {
    "input": ("enter_behavior", "prompt_soft_wrap"),
    "context": ("compaction_recent_items", "compaction_summary_tokens"),
    "notifications": ("notify_completed", "notify_failed"),
    "wake-limits": ("wake_max_per_session", "wake_cooldown_seconds"),
    "execution": ("execution_profile", "max_steps"),
    "model-requests": ("timeout_seconds", "max_output_tokens", "failover"),
    "web-tools": ("web_search_mode", "web_fetch_mode"),
    "language-tools": ("lsp_enabled",),
    "verification": ("verification_command",),
    "tool-intent": ("show_tool_intent",),
}
CHOICES: dict[str, tuple[str, ...]] = {
    "enter_behavior": ("send", "newline"),
    "prompt_soft_wrap": ("enabled", "disabled"),
    "notify_completed": ("enabled", "disabled"),
    "notify_failed": ("enabled", "disabled"),
    "execution_profile": ("normal", "deep"),
    "failover": ("enabled", "disabled"),
    "web_search_mode": ("disabled", "auto", "inline", "sidecar"),
    "web_fetch_mode": ("disabled", "auto", "local", "inline"),
    "lsp_enabled": ("enabled", "disabled"),
    "show_tool_intent": ("enabled", "disabled"),
}


class AgentPreferencesScreen(ModalScreen[None]):
    """Persist validated values without mutating an active runtime."""

    CSS = """
    AgentPreferencesScreen { align: center middle; background: $modal-overlay 25%; }
    #agent-preferences-dialog {
        width: 92%; max-width: 100; height: 90%; padding: $space-2 $space-3;
        background: $surface; border: round $border;
    }
    #agent-preferences-title { height: auto; text-style: bold; color: $text-primary; margin-bottom: 1; }
    #agent-preferences-description { height: auto; margin-bottom: 2; color: $text-muted; }
    #agent-preferences-fields { height: 1fr; }
    .agent-preference-label {
        height: auto; margin-top: 2; margin-bottom: 1;
        text-style: bold; color: $text-primary;
    }
    .agent-preference-help { height: auto; color: $text-muted; margin-top: 1; margin-bottom: 1; }
    #agent-preferences-fields Input, #agent-preferences-fields Select { width: 100%; margin-bottom: 0; }
    #agent-preferences-status { height: auto; margin-top: 1; color: $text-primary; }
    #agent-preferences-actions {
        height: auto; align-horizontal: right;
        border-top: solid $border; padding-top: 1; margin-top: 1;
    }
    #agent-preferences-actions Button { min-width: 8; margin-left: 2; }
    #agent-preferences-actions Button:first-of-type { margin-left: 0; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", show=False)]

    def __init__(
        self,
        category: str,
        preferences: AgentPreferences,
        store: UiPreferencesStore,
        *,
        language: UiLanguage,
        workspace: Path | None = None,
    ) -> None:
        super().__init__()
        self.category = category
        self.preferences = preferences
        self.store = store
        self.language = language
        self.workspace = workspace
        self._workspace_scope: Path | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-preferences-dialog"):
            yield Label(
                ui_text(self.language, f"settings.extra.{self.category}"),
                id="agent-preferences-title",
            )
            yield Static(
                ui_text(self.language, "settings.extra.description"),
                id="agent-preferences-description",
            )
            if self.workspace is not None:
                yield Select(
                    [
                        (ui_text(self.language, "settings.scope.user"), "user"),
                        (ui_text(self.language, "settings.scope.project"), "project"),
                    ],
                    value="user",
                    allow_blank=False,
                    id="agent-preferences-scope",
                )
            with VerticalScroll(id="agent-preferences-fields"):
                for name in PREFERENCE_GROUPS[self.category]:
                    value = getattr(self.preferences, name)
                    yield Label(
                        ui_text(self.language, f"settings.option.{name}"),
                        classes="agent-preference-label",
                    )
                    if name in CHOICES:
                        selected = (
                            ""
                            if value is None
                            else (
                                "enabled"
                                if value is True
                                else "disabled"
                                if value is False
                                else value
                            )
                        )
                        options = [(ui_text(self.language, "settings.extra.inherit"), "")]
                        options.extend(
                            (ui_text(self.language, f"settings.choice.{choice}"), choice)
                            for choice in CHOICES[name]
                        )
                        yield Select(
                            options, value=selected, allow_blank=False, id=f"preference-{name}"
                        )
                    else:
                        yield Input(
                            value="" if value is None else str(value),
                            id=f"preference-{name}",
                            placeholder=ui_text(self.language, "settings.extra.inherit"),
                        )
                    yield Static(
                        ui_text(self.language, f"settings.option.{name}.help"),
                        classes="agent-preference-help",
                    )
            yield Static("", id="agent-preferences-status")
            with Horizontal(id="agent-preferences-actions"):
                yield Button(ui_text(self.language, "settings.back"), id="agent-preferences-back")
                yield Button(
                    ui_text(self.language, "settings.extra.reset"), id="agent-preferences-reset"
                )
                yield Button(
                    ui_text(self.language, "settings.extra.save"), id="agent-preferences-save"
                )

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "agent-preferences-scope":
            return
        workspace = self.workspace if event.value == "project" else None
        if workspace == self._workspace_scope:
            return
        try:
            preferences = await self.store.load_agent_preferences(workspace)
        except Exception:
            self.query_one("#agent-preferences-scope", Select).value = (
                "project" if self._workspace_scope is not None else "user"
            )
            self.query_one("#agent-preferences-status", Static).update(
                ui_text(self.language, "settings.extra.load_failed")
            )
            return
        self._workspace_scope = workspace
        self.preferences = preferences
        for name in PREFERENCE_GROUPS[self.category]:
            value = getattr(preferences, name)
            control = self.query_one(f"#preference-{name}")
            if isinstance(control, Select):
                control.value = (
                    ""
                    if value is None
                    else ("enabled" if value is True else "disabled" if value is False else value)
                )
            elif isinstance(control, Input):
                control.value = "" if value is None else str(value)
        self.query_one("#agent-preferences-status", Static).update("")

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "agent-preferences-back":
            self.dismiss(None)
        elif event.button.id == "agent-preferences-reset":
            for name in PREFERENCE_GROUPS[self.category]:
                control = self.query_one(f"#preference-{name}")
                if isinstance(control, (Select, Input)):
                    control.value = ""
            self.query_one("#agent-preferences-status", Static).update(
                ui_text(self.language, "settings.extra.reset_pending")
            )
        elif event.button.id == "agent-preferences-save":
            try:
                values = asdict(await self.store.load_agent_preferences(self._workspace_scope))
            except Exception:
                self.query_one("#agent-preferences-status", Static).update(
                    ui_text(self.language, "settings.extra.load_failed")
                )
                return
            for name in PREFERENCE_GROUPS[self.category]:
                control = self.query_one(f"#preference-{name}")
                raw = str(control.value).strip() if isinstance(control, (Input, Select)) else ""
                try:
                    if not raw:
                        values[name] = None
                    elif name in (
                        "failover",
                        "lsp_enabled",
                        "prompt_soft_wrap",
                        "notify_completed",
                        "notify_failed",
                        "show_tool_intent",
                    ):
                        values[name] = raw == "enabled"
                    elif name in (
                        "max_steps",
                        "timeout_seconds",
                        "max_output_tokens",
                        "wake_max_per_session",
                        "wake_cooldown_seconds",
                        "compaction_recent_items",
                        "compaction_summary_tokens",
                    ):
                        values[name] = int(raw)
                    else:
                        values[name] = raw
                    AgentPreferences(**values)
                except (TypeError, ValueError):
                    control.focus()
                    self.query_one("#agent-preferences-status", Static).update(
                        ui_text(
                            self.language,
                            "settings.extra.invalid",
                            name=ui_text(self.language, f"settings.option.{name}"),
                        )
                    )
                    return
            event.button.disabled = True
            try:
                saved = AgentPreferences(**values)
                await self.store.save_agent_preferences(saved, self._workspace_scope)
            except Exception:
                self.query_one("#agent-preferences-status", Static).update(
                    ui_text(self.language, "settings.extra.save_failed")
                )
            else:
                self.preferences = saved
                self.query_one("#agent-preferences-status", Static).update(
                    ui_text(self.language, "settings.extra.saved")
                )
            finally:
                event.button.disabled = False


class AgentPreferencesOverview(ModalScreen[None]):
    """Show startup preference snapshots alongside saved scopes, without secrets."""

    CSS = AgentPreferencesScreen.CSS.replace("AgentPreferencesScreen", "AgentPreferencesOverview")
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", show=False)]

    def __init__(
        self,
        startup: AgentPreferences,
        user: AgentPreferences,
        project: AgentPreferences,
        *,
        language: UiLanguage,
    ) -> None:
        super().__init__()
        self.startup = startup
        self.user = user
        self.project = project
        self.language = language

    def compose(self) -> ComposeResult:
        from rich.text import Text

        with Vertical(id="agent-preferences-dialog"):
            yield Label(
                ui_text(self.language, "settings.extra.preferences-overview"),
                id="agent-preferences-title",
            )
            yield Static(
                ui_text(self.language, "settings.overview.description"),
                id="agent-preferences-description",
            )
            with VerticalScroll(id="agent-preferences-fields"):
                for name in asdict(self.startup):
                    user = getattr(self.user, name)
                    project = getattr(self.project, name)
                    saved = project if project is not None else user
                    startup = getattr(self.startup, name)
                    source = (
                        "project"
                        if project is not None
                        else "user"
                        if user is not None
                        else "default"
                    )

                    def display(value: object, field: str = name) -> str:
                        if value is None:
                            return ui_text(self.language, "settings.extra.inherit")
                        if field == "verification_command":
                            return ui_text(self.language, "settings.overview.command_set")
                        if isinstance(value, bool):
                            return ui_text(
                                self.language,
                                "settings.choice.enabled" if value else "settings.choice.disabled",
                            )
                        return str(value)

                    yield Label(
                        ui_text(self.language, f"settings.option.{name}"),
                        classes="agent-preference-label",
                    )
                    yield Static(
                        Text(
                            ui_text(
                                self.language,
                                "settings.overview.row",
                                startup=display(startup),
                                saved=display(saved),
                                source=ui_text(self.language, f"settings.scope.{source}"),
                            )
                        ),
                        classes="agent-preference-help",
                    )
            yield Button(ui_text(self.language, "settings.back"), id="agent-preferences-back")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
