"""Settings screens for user-owned TUI preferences.

TUI 用户偏好设置屏幕.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from neuro_code.application.execution_policy import ExecutionBudgetPolicy, ExecutionProfile
from neuro_code.application.ports.agent_preferences import (
    AgentPreferenceResolution,
    AgentPreferences,
)
from neuro_code.application.ports.configuration import resolve_http_client_policy
from neuro_code.application.ports.provider_settings import (
    ManagedProviderSettings,
    ManagedProxyPolicy,
    ProviderSettingsStore,
)
from neuro_code.application.ports.runtime_capabilities import (
    RuntimeWebCapabilityInspection,
    WebSearchAvailability,
    WebSearchUnavailableReason,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskWakePolicy
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.tui.screens.agent_preferences import PREFERENCE_GROUPS
from neuro_code.interfaces.tui.state import _ERROR_MARK
from neuro_code.interfaces.tui.text import language_name, ui_text
from neuro_code.interfaces.tui.theme import ERROR_TEXT_STYLE, theme_style
from neuro_code.interfaces.tui.widgets import MenuOptionButton
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.shared.ui_theme import UiTheme


def _simple_web_search_choice(value: str | None) -> str:
    if value == "disabled":
        return "off"
    if value in {"sidecar", "custom"}:
        return "custom"
    return "auto"


class SettingsScreen(ModalScreen[str | None]):
    """First-level settings navigation; detailed forms live on child screens.

    一级设置导航;详细表单位于子界面."""

    CSS = """
    SettingsScreen { align: center middle; background: $modal-overlay 25%; }
    #settings-dialog {
        width: 94%; max-width: 132; height: 90%;
        padding: 1 2; border: round $border; background: $surface;
    }
    #settings-title { text-style: bold; color: $text-primary; height: 1; }
    #settings-description, #settings-help { color: $text-muted; height: auto; }
    #settings-search { width: 100%; margin: 1 0; }
    #settings-body { height: 1fr; }
    #settings-navigation { width: 24; height: 1fr; margin-right: 3; padding-right: 1; border-right: solid $border; }
    #settings-navigation Button {
        width: 100%; min-width: 0; height: 3; border: none;
        background: $surface; color: $text-muted; content-align: left middle;
    }
    #settings-navigation Button.active, #settings-navigation Button:focus {
        background: $boost; color: $text-primary; text-style: bold;
    }
    #settings-categories { width: 1fr; height: 1fr; }
    .settings-group { height: auto; margin-bottom: 2; padding: 1 2; background: $boost 35%; }
    .settings-group-title { color: $text-primary; text-style: bold; margin: 0 1 1 1; }
    .settings-entry { height: auto; padding: 1 0; border-top: solid $border; }
    .settings-entry MenuOptionButton { width: 100%; height: 2; content-align: left middle; background: transparent; }
    .settings-entry MenuOptionButton:focus { background: $surface; }
    .settings-entry-description { color: $text-muted; height: auto; margin: 0 1; }
    #settings-empty { color: $text-muted; margin: 1; height: auto; }
    #settings-help { margin-top: 1; }
    SettingsScreen.compact #settings-dialog { width: 100%; height: 100%; padding: 0 1; }
    SettingsScreen.compact #settings-navigation { display: none; }
    SettingsScreen.compact #settings-description { display: none; }
    SettingsScreen.compact .settings-group { padding: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+f", "search", "Search", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        selected: UiLanguage,
        *,
        language: UiLanguage,
        provider_settings_available: bool,
        reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH,
        interaction_mode: InteractionMode = InteractionMode.NORMAL,
        permission_level: str = "ask",
        ui_theme: UiTheme = UiTheme.PORCELAIN,
        initial_category: str | None = None,
        provider_settings: ManagedProviderSettings | None = None,
        preference_resolution: AgentPreferenceResolution | None = None,
        web_capabilities: RuntimeWebCapabilityInspection | None = None,
    ) -> None:
        super().__init__()
        self.selected = selected
        self.language = language
        self.provider_settings_available = provider_settings_available
        self.reasoning_effort = reasoning_effort
        self.interaction_mode = interaction_mode
        self.permission_level = permission_level
        self.ui_theme = ui_theme
        self.provider_settings = provider_settings
        self.preference_resolution = preference_resolution or AgentPreferenceResolution(
            defaults=AgentPreferences(
                execution_profile="normal",
                max_steps=24,
                failover=True,
                web_search_mode="auto",
                web_fetch_mode="disabled",
                lsp_enabled=False,
            )
        )
        self.web_capabilities = web_capabilities
        self._initial_category = initial_category
        self._group = "all"

    GROUPS: ClassVar[dict[str, tuple[str, ...]]] = {
        "appearance": ("language", "theme", "input"),
        "connection": ("providers", "network"),
        "agent": ("agent-reasoning", "agent-interaction-mode", "execution"),
        "web": ("web-tools", "search-api-key"),
        "development": ("language-tools", "verification"),
        "security": ("agent-permissions", "tool-intent"),
        "advanced": (
            "model-requests",
            "web-routing",
            "context",
            "notifications",
            "wake-limits",
            "background-wake",
            "preferences-overview",
        ),
    }

    def _entries(self) -> dict[str, tuple[str, str]]:
        effective = self.preference_resolution.effective()

        def setting_value(name: str) -> str:
            value = getattr(effective, name)
            if name == "max_steps" and value is None:
                value = ExecutionBudgetPolicy.resolve(
                    ExecutionProfile(effective.execution_profile or "normal"),
                    max_steps=None,
                ).max_model_calls
            source_key = self.preference_resolution.source(name).value
            source = ui_text(self.language, f"settings.source.{source_key}")
            if name == "execution_profile":
                value_text = ui_text(
                    self.language,
                    "settings.choice.deep" if value == "deep" else "settings.choice.normal",
                )
            elif name == "web_search_mode":
                value_text = ui_text(
                    self.language,
                    f"settings.choice.{_simple_web_search_choice(value)}",
                )
            elif name == "lsp_enabled":
                value_text = ui_text(
                    self.language,
                    "settings.choice.enabled" if value else "settings.choice.disabled",
                )
            elif name == "verification_command":
                value_text = ui_text(
                    self.language,
                    "settings.overview.command_set"
                    if value
                    else "settings.overview.command_default",
                )
            else:
                value_text = str(value) if value is not None else ""
            return ui_text(
                self.language,
                "settings.summary.value_source",
                value=value_text,
                source=source,
            )

        search_summary = self._search_status_summary(effective.web_search_mode)
        entries = {
            "language": (
                "settings.category.language.label",
                language_name(self.selected, in_language=self.language),
            ),
            "theme": (
                "settings.theme.title",
                ui_text(self.language, f"settings.theme.{self.ui_theme.value}"),
            ),
            "providers": (
                "settings.category.providers.label",
                ui_text(self.language, "settings.category.providers.value"),
            ),
            "network": (
                "settings.category.network.label",
                ui_text(self.language, "settings.category.network.value"),
            ),
            "search-api-key": (
                "settings.category.search_api_key.label",
                ui_text(
                    self.language,
                    "settings.search_api_key.saved"
                    if self.provider_settings is not None
                    and self.provider_settings.brave_search_api_key is not None
                    else "settings.search_api_key.not_saved",
                ),
            ),
            "agent-reasoning": (
                "settings.category.agent_reasoning.label",
                self.reasoning_effort.value,
            ),
            "agent-interaction-mode": (
                "settings.category.agent_interaction_mode.label",
                self.interaction_mode.value,
            ),
            "agent-permissions": (
                "settings.category.agent_permissions.label",
                ui_text(self.language, f"permission.value.{self.permission_level}"),
            ),
            "background-wake": (
                "settings.category.background_wake.label",
                ui_text(self.language, "settings.category.background_wake.value"),
            ),
        }

        for category in PREFERENCE_GROUPS:
            if category == "web-tools":
                value = search_summary
            elif category == "execution":
                profile = setting_value("execution_profile")
                steps = setting_value("max_steps")
                value = ui_text(
                    self.language,
                    "settings.summary.agent",
                    profile=profile,
                    steps=steps,
                )
            elif category == "language-tools":
                value = setting_value("lsp_enabled")
            elif category == "verification":
                value = setting_value("verification_command")
            elif category == "model-requests":
                value = setting_value("failover")
            elif category == "web-routing":
                value = ui_text(self.language, "settings.summary.advanced")
            else:
                value = ui_text(self.language, "settings.summary.edit")
            entries[category] = (
                f"settings.extra.{category}",
                value,
            )

        entries["preferences-overview"] = (
            "settings.extra.preferences-overview",
            ui_text(self.language, "settings.category.providers.value"),
        )
        settings = self.provider_settings
        if settings is not None:
            for category, value in (
                (
                    "providers",
                    settings.default_provider or ui_text(self.language, "settings.no_default"),
                ),
                (
                    "network",
                    ui_text(self.language, f"network_settings.{settings.proxy_defaults.mode}"),
                ),
                (
                    "background-wake",
                    ui_text(
                        self.language,
                        f"background_wake_settings.{settings.background_task_wake_policy.value}",
                    ),
                ),
            ):
                entries[category] = (entries[category][0], value)
        return entries

    def _search_status_summary(self, mode: str | None) -> str:
        mode_key = _simple_web_search_choice(mode)
        mode_text = ui_text(self.language, f"settings.choice.{mode_key}")
        inspection = self.web_capabilities
        if inspection is None:
            return mode_text
        if inspection.search_availability is WebSearchAvailability.AVAILABLE:
            status = ui_text(self.language, "settings.web.short_available")
            identity = "/".join(
                part for part in (inspection.search_profile, inspection.search_model) if part
            )
            fallback = (
                ui_text(
                    self.language,
                    "settings.web.search_fallback",
                    fallback=inspection.search_fallback,
                )
                if inspection.search_fallback is not None
                else None
            )
            return " · ".join(part for part in (mode_text, status, identity, fallback) if part)
        if inspection.search_availability is WebSearchAvailability.DISABLED:
            return " · ".join(
                (
                    ui_text(self.language, "settings.choice.off"),
                    ui_text(self.language, "settings.web.short_disabled"),
                )
            )
        reason = inspection.search_reason
        reason_key = (
            {
                WebSearchUnavailableReason.NO_COMPATIBLE_PROVIDER: "settings.web.reason.no_provider",
                WebSearchUnavailableReason.CONFIGURED_ROUTE_UNAVAILABLE: "settings.web.reason.route",
                WebSearchUnavailableReason.MAIN_CAPABILITY_UNSUPPORTED: "settings.web.reason.main",
                WebSearchUnavailableReason.TOOL_NOT_ALLOWED: "settings.web.reason.not_allowed",
            }.get(reason, "settings.web.status.unknown")
            if reason is not None
            else "settings.web.status.unknown"
        )
        return " · ".join(
            (
                mode_text,
                ui_text(self.language, "settings.web.short_unavailable"),
                ui_text(self.language, reason_key),
            )
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog"):
            yield Label(ui_text(self.language, "settings.title"), id="settings-title")
            yield Static(ui_text(self.language, "settings.description"), id="settings-description")
            yield Input(placeholder=ui_text(self.language, "settings.search"), id="settings-search")
            with Horizontal(id="settings-body"):
                with VerticalScroll(id="settings-navigation"):
                    for group in ("all", *self.GROUPS):
                        yield Button(
                            ui_text(self.language, f"settings.group.{group}"),
                            id=f"settings-nav-{group}",
                            classes="active" if group == "appearance" else "",
                        )
                with VerticalScroll(id="settings-categories"):
                    entries = self._entries()
                    for group, categories in self.GROUPS.items():
                        with Vertical(id=f"settings-group-{group}", classes="settings-group"):
                            yield Label(
                                ui_text(self.language, f"settings.group.{group}"),
                                classes="settings-group-title",
                            )
                            for category in categories:
                                key, value = entries[category]
                                unavailable = (
                                    category
                                    in {
                                        "providers",
                                        "network",
                                        "background-wake",
                                        "search-api-key",
                                    }
                                    and not self.provider_settings_available
                                )
                                with Vertical(
                                    id=f"settings-entry-{category}", classes="settings-entry"
                                ):
                                    yield MenuOptionButton(
                                        ui_text(self.language, key),
                                        secondary=value,
                                        id=f"settings-category-{category}",
                                        disabled=unavailable,
                                    )
                                    description = ui_text(
                                        self.language, f"settings.detail.{category}"
                                    )
                                    if unavailable:
                                        description += " " + ui_text(
                                            self.language, "settings.unavailable"
                                        )
                                    yield Static(description, classes="settings-entry-description")
                    yield Static(ui_text(self.language, "settings.empty"), id="settings-empty")
            yield Static(ui_text(self.language, "settings.help"), id="settings-help")

    def on_mount(self) -> None:
        self._group = (
            next(
                (
                    group
                    for group, entries in self.GROUPS.items()
                    if self._initial_category in entries
                ),
                "appearance",
            )
            if self.app.size.width >= 88
            else "all"
        )
        self.set_class(self.app.size.width < 88, "compact")
        self._filter_entries()
        category = self._initial_category
        if category is not None and category in self._entries():
            self.query_one(f"#settings-category-{category}", Button).focus()
        else:
            self.query_one("#settings-search", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 88, "compact")
        if self.is_mounted and event.size.width < 88:
            self._group = "all"
            self._filter_entries()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "settings-search":
            self._filter_entries()

    def action_search(self) -> None:
        self.query_one("#settings-search", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "settings-search":
            return
        event.stop()
        for group, categories in self.GROUPS.items():
            if not self.query_one(f"#settings-group-{group}").display:
                continue
            for category in categories:
                entry = self.query_one(f"#settings-entry-{category}")
                button = entry.query_one(Button)
                if entry.display and not button.disabled:
                    button.focus()
                    return

    def _filter_entries(self) -> None:
        search = self.query_one("#settings-search", Input).value.casefold().strip()
        count = 0
        for group, categories in self.GROUPS.items():
            visible = 0
            for category in categories:
                key, value = self._entries()[category]
                terms = " ".join(
                    (
                        category,
                        value,
                        ui_text(self.language, key),
                        ui_text(self.language, f"settings.group.{group}"),
                        ui_text(self.language, f"settings.detail.{category}"),
                    )
                ).casefold()
                match = (bool(search) or self._group in ("all", group)) and all(
                    word in terms for word in search.split()
                )
                self.query_one(f"#settings-entry-{category}").display = match
                visible += int(match)
            self.query_one(f"#settings-group-{group}").display = visible > 0
            count += visible
        self.query_one("#settings-empty").display = count == 0
        for button in self.query("#settings-navigation Button"):
            button.set_class(button.id == f"settings-nav-{self._group}" and not search, "active")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id or ""
        if identifier.startswith("settings-nav-"):
            event.stop()
            self._group = identifier.removeprefix("settings-nav-")
            self.query_one("#settings-search", Input).value = ""
            self._filter_entries()
            self.query_one("#settings-categories", VerticalScroll).scroll_home(animate=False)
        elif identifier.startswith("settings-category-"):
            event.stop()
            self.dismiss(identifier.removeprefix("settings-category-"))

    def action_cancel(self) -> None:
        search = self.query_one("#settings-search", Input)
        if search.value:
            search.value = ""
            search.focus()
            return
        self.dismiss(None)


class LanguageSettingsScreen(ModalScreen[UiLanguage | None]):
    """Edit one interface preference without rendering unrelated provider fields.

    编辑一项界面偏好,不渲染无关的 Provider 字段."""

    CSS = """
    LanguageSettingsScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #language-settings-dialog {
        width: 76%;
        max-width: 72;
        height: auto;
        padding: $space-2 $space-3;
        border: round $border;
        background: $surface;
    }

    #language-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #language-settings-description,
    #language-settings-help {
        color: $text-muted;
        margin-bottom: 1;
    }

    #settings-languages,
    #language-settings-actions {
        height: auto;
    }

    #settings-languages MenuOptionButton {
        width: 100%;
        height: 3;
    }

    #language-settings-actions {
        align-horizontal: right;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(self, selected: UiLanguage, *, language: UiLanguage) -> None:
        super().__init__()
        self.selected = selected
        self.language = language

    def _choice_label(self, choice: UiLanguage) -> str:
        label = language_name(choice, in_language=choice)
        if choice is self.selected:
            label += f" · {ui_text(self.language, 'settings.current')}"
        return label

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                ui_text(self.language, "settings.language.title"),
                id="language-settings-title",
            ),
            Static(
                ui_text(self.language, "settings.language.description"),
                id="language-settings-description",
            ),
            Vertical(
                MenuOptionButton(
                    language_name(
                        UiLanguage.SIMPLIFIED_CHINESE,
                        in_language=UiLanguage.SIMPLIFIED_CHINESE,
                    ),
                    id="settings-language-zh-cn",
                    selected=self.selected is UiLanguage.SIMPLIFIED_CHINESE,
                ),
                MenuOptionButton(
                    language_name(UiLanguage.ENGLISH, in_language=UiLanguage.ENGLISH),
                    id="settings-language-en",
                    selected=self.selected is UiLanguage.ENGLISH,
                ),
                id="settings-languages",
            ),
            Static(
                ui_text(self.language, "settings.language.help"),
                id="language-settings-help",
            ),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="language-settings-back"),
                id="language-settings-actions",
            ),
            id="language-settings-dialog",
            classes="modal-dialog modal-s",
        )

    def on_mount(self) -> None:
        selector = (
            "#settings-language-zh-cn"
            if self.selected is UiLanguage.SIMPLIFIED_CHINESE
            else "#settings-language-en"
        )
        self.query_one(selector, Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "settings-language-zh-cn": UiLanguage.SIMPLIFIED_CHINESE,
            "settings-language-en": UiLanguage.ENGLISH,
        }
        choice = choices.get(event.button.id or "")
        if choice is not None:
            self.dismiss(choice)
        elif event.button.id == "language-settings-back":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ThemeSettingsScreen(ModalScreen[UiTheme | None]):
    """Choose a light or dark palette with a separate current-choice marker.

    选择浅色或深色主题,并独立标记当前选项。"""

    CSS = """
    ThemeSettingsScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #theme-settings-dialog {
        width: 76%;
        max-width: 72;
        height: 85%;
        max-height: 42;
        padding: 1 2;
        border: round $border;
        background: $surface;
    }

    #theme-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #theme-settings-description,
    #theme-settings-help {
        color: $text-muted;
        margin-bottom: 1;
    }

    #settings-themes {
        height: 1fr;
        scrollbar-size-vertical: 1;
        margin-bottom: 1;
    }

    #theme-settings-actions { height: auto; }

    #settings-themes MenuOptionButton {
        width: 100%;
        height: 2;
        min-height: 2;
        border: none;
        padding: 0 1;
    }

    #theme-settings-actions {
        align-horizontal: right;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        selected: UiTheme,
        *,
        language: UiLanguage,
        preview: Callable[[UiTheme], None] | None = None,
    ) -> None:
        super().__init__()
        self.selected = selected
        self.language = language
        self.preview = preview

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                ui_text(self.language, "settings.theme.title"),
                id="theme-settings-title",
            ),
            Static(
                ui_text(self.language, "settings.theme.description"),
                id="theme-settings-description",
            ),
            VerticalScroll(
                *(
                    MenuOptionButton(
                        ui_text(self.language, f"settings.theme.{choice.value}"),
                        secondary=ui_text(self.language, f"settings.theme.{choice.value}.detail"),
                        id=f"settings-theme-{choice.value}",
                        selected=self.selected is choice,
                    )
                    for choice in UiTheme
                ),
                id="settings-themes",
            ),
            Static(
                ui_text(self.language, "settings.theme.help"),
                id="theme-settings-help",
            ),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="theme-settings-back"),
                id="theme-settings-actions",
            ),
            id="theme-settings-dialog",
            classes="modal-dialog modal-s",
        )

    def on_mount(self) -> None:
        self.query_one(f"#settings-theme-{self.selected.value}", Button).focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if self.preview is not None:
            for choice in UiTheme:
                if event.widget.id == f"settings-theme-{choice.value}":
                    self.preview(choice)
                    break

    def on_key(self, event: events.Key) -> None:
        if event.key in {"up", "down"}:
            event.stop()
            event.prevent_default()
            choices = list(self.query("#settings-themes MenuOptionButton"))
            if self.focused in choices:
                index = choices.index(self.focused)
                choices[(index + (1 if event.key == "down" else -1)) % len(choices)].focus()
            else:
                choices[0].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        for choice in UiTheme:
            if event.button.id == f"settings-theme-{choice.value}":
                self.dismiss(choice)
                return
        if event.button.id == "theme-settings-back":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NetworkProxySettingsScreen(ModalScreen[ManagedProviderSettings | None]):
    """Edit the user-wide proxy default independently of provider credentials.

    独立编辑用户级代理默认值,不涉及 Provider 凭据."""

    CSS = """
    NetworkProxySettingsScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #network-settings-dialog {
        width: 82%;
        max-width: 88;
        height: auto;
        padding: $space-2 $space-3;
        border: round $border;
        background: $surface;
    }

    #network-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #network-settings-description {
        color: $text-muted;
        margin-bottom: 1;
    }

    #network-settings-dialog Label {
        text-style: bold;
        color: $text-primary;
        margin-top: 2;
        margin-bottom: 1;
    }

    #network-settings-hint {
        color: $text-muted;
        margin-top: 1;
        margin-bottom: 1;
    }

    #network-settings-error {
        margin-top: 1;
        margin-bottom: 1;
        padding-left: 1;
        border-left: tall $border-focus;
        color: $text-primary;
        text-style: bold;
    }

    #network-settings-error.empty {
        padding-left: 0;
        border-left: none;
    }

    #network-settings-modes,
    #network-settings-actions {
        height: auto;
    }

    #network-settings-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #network-settings-modes Button:last-of-type {
        margin-right: 0;
    }

    #network-settings-proxy-env {
        margin-top: 1;
    }

    #network-settings-actions {
        align-horizontal: right;
        border-top: solid $border;
        padding-top: 1;
        margin-top: 1;
    }

    #network-settings-actions Button {
        margin-left: 2;
    }

    #network-settings-actions Button:first-of-type {
        margin-left: 0;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        *,
        language: UiLanguage,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
        socks_supported: bool = False,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store
        self.socks_supported = socks_supported
        self._active_proxy_mode = provider_settings.proxy_defaults.mode

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(ui_text(self.language, "network_settings.title"), id="network-settings-title"),
            Static(
                ui_text(self.language, "network_settings.description"),
                id="network-settings-description",
            ),
            Label(ui_text(self.language, "network_settings.default_policy")),
            Horizontal(
                Button(
                    ui_text(self.language, "network_settings.environment"),
                    id="network-settings-environment",
                    variant="primary",
                ),
                Button(
                    ui_text(self.language, "network_settings.direct"),
                    id="network-settings-direct",
                ),
                Button(
                    ui_text(self.language, "network_settings.explicit"),
                    id="network-settings-explicit",
                ),
                id="network-settings-modes",
            ),
            Input(
                value=self.provider_settings.proxy_defaults.proxy_url_env or "",
                placeholder=ui_text(self.language, "network_settings.environment_variable"),
                id="network-settings-proxy-env",
                disabled=self.provider_settings.proxy_defaults.mode != "explicit",
            ),
            Static("", id="network-settings-hint"),
            Static("", id="network-settings-error", classes="empty"),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="network-settings-back"),
                Button(
                    ui_text(self.language, "network_settings.save"),
                    id="network-settings-save",
                    variant="success",
                ),
                id="network-settings-actions",
            ),
            id="network-settings-dialog",
            classes="modal-dialog modal-m",
        )

    def on_mount(self) -> None:
        self._select_proxy_mode(self._active_proxy_mode)
        self.query_one(f"#network-settings-{self._active_proxy_mode}", Button).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("network-settings-"):
            mode = button_id.removeprefix("network-settings-")
            if mode in {"environment", "direct", "explicit"}:
                self._select_proxy_mode(mode)
                return
        if button_id == "network-settings-save":
            await self._save()
        elif button_id == "network-settings-back":
            self.dismiss(None)

    def _select_proxy_mode(self, proxy_mode: str) -> None:
        if proxy_mode not in {"environment", "direct", "explicit"}:
            return
        self._active_proxy_mode = proxy_mode
        for candidate in ("environment", "direct", "explicit"):
            button = self.query_one(f"#network-settings-{candidate}", Button)
            button.variant = "primary" if candidate == proxy_mode else "default"
        self.query_one("#network-settings-proxy-env", Input).disabled = proxy_mode != "explicit"
        self.query_one("#network-settings-hint", Static).update(
            ui_text(self.language, f"network_settings.hint.{proxy_mode}")
        )

    async def _save(self) -> None:
        proxy_url_env = (
            self.query_one("#network-settings-proxy-env", Input).value.strip() or None
            if self._active_proxy_mode == "explicit"
            else None
        )
        try:
            proxy_defaults = ManagedProxyPolicy(self._active_proxy_mode, proxy_url_env)
            resolve_http_client_policy(
                proxy_mode=proxy_defaults.mode,
                proxy_url_env=proxy_defaults.proxy_url_env,
                environ=os.environ,
                socks_supported=self.socks_supported,
            )
            settings = await self.provider_settings_store.save_proxy_defaults(proxy_defaults)
        except Exception as error:
            error_widget = self.query_one("#network-settings-error", Static)
            error_widget.set_class(False, "empty")
            error_widget.update(
                Text(f"{_ERROR_MARK} {error}", style=theme_style(self, ERROR_TEXT_STYLE))
            )
            return
        self.dismiss(settings)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BraveSearchApiKeySettingsScreen(ModalScreen[ManagedProviderSettings | None]):
    """Manage the independent Brave credential without displaying its value.

    在不显示密钥内容的前提下管理独立 Brave 凭据."""

    CSS = """
    BraveSearchApiKeySettingsScreen { align: center middle; background: $modal-overlay 25%; }
    #search-api-key-dialog {
        width: 82%; max-width: 88; height: auto; padding: $space-2 $space-3;
        border: round $border; background: $surface;
    }
    #search-api-key-title { text-style: bold; color: $text-primary; margin-bottom: 1; }
    #search-api-key-description, #search-api-key-status, #search-api-key-error {
        color: $text-muted; height: auto; margin-bottom: 1;
    }
    #search-api-key-error { color: $text-primary; text-style: bold; }
    #search-api-key-actions {
        align-horizontal: right; border-top: solid $border; padding-top: 1; margin-top: 1;
    }
    #search-api-key-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        *,
        language: UiLanguage,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store

    def compose(self) -> ComposeResult:
        key_is_saved = self.provider_settings.brave_search_api_key is not None
        yield Vertical(
            Label(ui_text(self.language, "search_api_key.title"), id="search-api-key-title"),
            Static(
                ui_text(self.language, "search_api_key.description"),
                id="search-api-key-description",
            ),
            Static(
                ui_text(
                    self.language,
                    "settings.search_api_key.saved"
                    if key_is_saved
                    else "settings.search_api_key.not_saved",
                ),
                id="search-api-key-status",
            ),
            Input(
                password=True,
                placeholder=ui_text(
                    self.language,
                    "search_api_key.replace_placeholder"
                    if key_is_saved
                    else "search_api_key.enter_placeholder",
                ),
                id="search-api-key-input",
            ),
            Static("", id="search-api-key-error"),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="search-api-key-back"),
                Button(
                    ui_text(self.language, "search_api_key.remove"),
                    id="search-api-key-remove",
                    disabled=not key_is_saved,
                    variant="error",
                ),
                Button(
                    ui_text(self.language, "search_api_key.save"),
                    id="search-api-key-save",
                    variant="success",
                ),
                id="search-api-key-actions",
            ),
            id="search-api-key-dialog",
            classes="modal-dialog modal-m",
        )

    def on_mount(self) -> None:
        self.query_one("#search-api-key-input", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "search-api-key-back":
            self.dismiss(None)
        elif button_id == "search-api-key-remove":
            await self._remove()
        elif button_id == "search-api-key-save":
            await self._save()

    async def _save(self) -> None:
        api_key = self.query_one("#search-api-key-input", Input).value.strip()
        if not api_key:
            if self.provider_settings.brave_search_api_key is not None:
                self.dismiss(None)
            else:
                self._show_error("search_api_key.error.required")
            return
        try:
            settings = await self.provider_settings_store.save_brave_search_api_key(api_key)
        except Exception as error:
            self._show_error(f"{type(error).__name__}: {error}")
            return
        self.dismiss(settings)

    async def _remove(self) -> None:
        if self.provider_settings.brave_search_api_key is None:
            return
        try:
            settings = await self.provider_settings_store.save_brave_search_api_key(None)
        except Exception as error:
            self._show_error(f"{type(error).__name__}: {error}")
            return
        self.dismiss(settings)

    def _show_error(self, message: str) -> None:
        self.query_one("#search-api-key-error", Static).update(
            Text(
                f"{_ERROR_MARK} {message}",
                style=theme_style(self, ERROR_TEXT_STYLE),
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class BackgroundWakeSettingsScreen(ModalScreen[ManagedProviderSettings | None]):
    """Edit the user-wide background-task wake default.

    编辑用户级后台任务唤醒默认值."""

    CSS = """
    BackgroundWakeSettingsScreen {
        align: center middle;
        background: $modal-overlay 25%;
    }

    #background-wake-settings-dialog {
        width: 82%;
        max-width: 88;
        height: auto;
        padding: $space-2 $space-3;
        border: round $border;
        background: $surface;
    }

    #background-wake-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #background-wake-settings-description {
        color: $text-muted;
        margin-bottom: 1;
    }

    #background-wake-settings-dialog Label {
        text-style: bold;
        color: $text-primary;
        margin-top: 2;
        margin-bottom: 1;
    }

    #background-wake-settings-hint {
        color: $text-muted;
        margin-top: 1;
        margin-bottom: 1;
    }

    #background-wake-settings-error {
        margin-top: 1;
        margin-bottom: 1;
        padding-left: 1;
        border-left: tall $border-focus;
        color: $text-primary;
        text-style: bold;
    }

    #background-wake-settings-error.empty {
        padding-left: 0;
        border-left: none;
    }

    #background-wake-settings-modes,
    #background-wake-settings-actions {
        height: auto;
    }

    #background-wake-settings-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #background-wake-settings-modes Button:last-of-type {
        margin-right: 0;
    }

    #background-wake-settings-actions {
        align-horizontal: right;
        border-top: solid $border;
        padding-top: 1;
        margin-top: 1;
    }

    #background-wake-settings-actions Button {
        margin-left: 2;
    }

    #background-wake-settings-actions Button:first-of-type {
        margin-left: 0;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        *,
        language: UiLanguage,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store
        self._active_policy = provider_settings.background_task_wake_policy

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                ui_text(self.language, "background_wake_settings.title"),
                id="background-wake-settings-title",
            ),
            Static(
                ui_text(self.language, "background_wake_settings.description"),
                id="background-wake-settings-description",
            ),
            Label(ui_text(self.language, "background_wake_settings.default_policy")),
            Horizontal(
                Button(
                    ui_text(self.language, "background_wake_settings.disabled"),
                    id="background-wake-settings-disabled",
                ),
                Button(
                    ui_text(self.language, "background_wake_settings.enabled"),
                    id="background-wake-settings-enabled",
                ),
                id="background-wake-settings-modes",
            ),
            Static("", id="background-wake-settings-hint"),
            Static("", id="background-wake-settings-error", classes="empty"),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="background-wake-settings-back"),
                Button(
                    ui_text(self.language, "background_wake_settings.save"),
                    id="background-wake-settings-save",
                    variant="success",
                ),
                id="background-wake-settings-actions",
            ),
            id="background-wake-settings-dialog",
            classes="modal-dialog modal-m",
        )

    def on_mount(self) -> None:
        self._select_policy(self._active_policy)
        self.query_one(f"#background-wake-settings-{self._active_policy.value}", Button).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id in {"background-wake-settings-disabled", "background-wake-settings-enabled"}:
            self._select_policy(
                BackgroundTaskWakePolicy(button_id.removeprefix("background-wake-settings-"))
            )
        elif button_id == "background-wake-settings-save":
            try:
                settings = await self.provider_settings_store.save_background_task_wake_policy(
                    self._active_policy
                )
            except Exception as error:
                error_widget = self.query_one("#background-wake-settings-error", Static)
                error_widget.set_class(False, "empty")
                error_widget.update(
                    Text(f"{_ERROR_MARK} {error}", style=theme_style(self, ERROR_TEXT_STYLE))
                )
                return
            self.dismiss(settings)
        elif button_id == "background-wake-settings-back":
            self.dismiss(None)

    def _select_policy(self, policy: BackgroundTaskWakePolicy) -> None:
        self._active_policy = policy
        for candidate in BackgroundTaskWakePolicy:
            self.query_one(f"#background-wake-settings-{candidate.value}", Button).variant = (
                "primary" if candidate is policy else "default"
            )
        self.query_one("#background-wake-settings-hint", Static).update(
            ui_text(
                self.language,
                f"background_wake_settings.hint.{policy.value}",
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = [
    "BackgroundWakeSettingsScreen",
    "BraveSearchApiKeySettingsScreen",
    "LanguageSettingsScreen",
    "NetworkProxySettingsScreen",
    "SettingsScreen",
]
