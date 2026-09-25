"""Forms for effective Agent settings and their advanced overrides.

Agent 设置的有效值表单与高级覆盖项.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from neuro_code.application.execution_policy import ExecutionBudgetPolicy, ExecutionProfile
from neuro_code.application.ports.agent_preferences import (
    AgentPreferenceResolution,
    AgentPreferences,
    AgentPreferenceSource,
)
from neuro_code.application.ports.runtime_capabilities import (
    RuntimeSearchProviderOption,
    RuntimeWebCapabilityInspection,
    WebSearchAvailability,
    WebSearchUnavailableReason,
)
from neuro_code.application.ports.ui_preferences import UiPreferencesStore
from neuro_code.application.ports.web_search import WebSearchExecutionPath
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.shared.ui_language import UiLanguage

PREFERENCE_GROUPS: dict[str, tuple[str, ...]] = {
    "input": ("enter_behavior", "prompt_soft_wrap"),
    "context": ("compaction_recent_items", "compaction_summary_tokens"),
    "notifications": ("notify_completed", "notify_failed"),
    "wake-limits": ("wake_max_per_session", "wake_cooldown_seconds"),
    "execution": ("execution_profile", "max_steps"),
    "model-requests": ("timeout_seconds", "max_output_tokens", "failover"),
    "web-tools": ("web_search_mode", "web_search_profile", "web_fetch_mode"),
    "web-routing": ("web_search_mode", "web_search_profile", "web_fetch_mode"),
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
    "web_search_mode": ("disabled", "auto", "inline", "sidecar", "custom"),
    "web_fetch_mode": ("disabled", "auto", "local", "inline"),
    "lsp_enabled": ("enabled", "disabled"),
    "show_tool_intent": ("enabled", "disabled"),
}

BASIC_CHOICES: dict[str, tuple[str, ...]] = {
    "web_search_mode": ("off", "auto", "custom"),
    "web_fetch_mode": ("off", "auto"),
}

BOOLEAN_FIELDS = frozenset(
    {
        "failover",
        "lsp_enabled",
        "prompt_soft_wrap",
        "notify_completed",
        "notify_failed",
        "show_tool_intent",
    }
)
INTEGER_FIELDS = frozenset(
    {
        "max_steps",
        "timeout_seconds",
        "max_output_tokens",
        "wake_max_per_session",
        "wake_cooldown_seconds",
        "compaction_recent_items",
        "compaction_summary_tokens",
    }
)
ADVANCED_CATEGORIES = frozenset(
    {"context", "notifications", "wake-limits", "model-requests", "web-routing", "tool-intent"}
)
RUNTIME_RELOAD_FIELDS = frozenset(
    {
        "compaction_recent_items",
        "compaction_summary_tokens",
        "execution_profile",
        "max_steps",
        "failover",
        "timeout_seconds",
        "max_output_tokens",
        "web_search_mode",
        "web_search_profile",
        "web_fetch_mode",
        "lsp_enabled",
        "verification_command",
        "wake_max_per_session",
        "wake_cooldown_seconds",
    }
)


@dataclass(frozen=True, slots=True)
class AgentPreferencesScreenResult:
    resolution: AgentPreferenceResolution | None = None
    reload_required: bool = False
    manage_providers: bool = False


class AgentPreferencesScreen(ModalScreen[AgentPreferencesScreenResult | None]):
    """Edit effective values while preserving the explicit per-scope overrides."""

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
    .agent-preference-source { height: auto; color: $text-secondary; margin-bottom: 1; }
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
        resolution: AgentPreferenceResolution | None = None,
        web_capabilities: RuntimeWebCapabilityInspection | None = None,
        provider_settings_available: bool = False,
        runtime_busy: bool = False,
    ) -> None:
        super().__init__()
        self.category = category
        self.preferences = preferences
        self.store = store
        self.language = language
        self.workspace = workspace
        self.resolution = resolution or AgentPreferenceResolution(
            defaults=AgentPreferences(
                enter_behavior="send",
                prompt_soft_wrap=True,
                execution_profile="normal",
                max_steps=24,
                failover=True,
                web_search_mode="auto",
                web_fetch_mode="disabled",
                lsp_enabled=False,
                show_tool_intent=True,
            ),
            user=preferences,
        )
        self.web_capabilities = web_capabilities
        self.provider_settings_available = provider_settings_available
        self.runtime_busy = runtime_busy
        self.advanced = category in ADVANCED_CATEGORIES
        self._workspace_scope: Path | None = None
        self._scope_value = "user"
        self._reset_pending = False
        self._baseline: dict[str, str] = {}
        self._dirty_fields: set[str] = set()

    @property
    def _search_options(self) -> tuple[RuntimeSearchProviderOption, ...]:
        if self.web_capabilities is None:
            return ()
        return self.web_capabilities.search_providers

    def _effective(self) -> AgentPreferences:
        return self.resolution.effective(scope=self._scope_value)

    def _target(self) -> AgentPreferences:
        return self.resolution.project if self._scope_value == "project" else self.resolution.user

    def _source_text(self, name: str, effective: AgentPreferences) -> str:
        source = self.resolution.source(name, scope=self._scope_value)
        value = getattr(effective, name)
        if name == "max_steps" and value is None:
            value = ExecutionBudgetPolicy.resolve(
                ExecutionProfile(effective.execution_profile or "normal"),
                max_steps=None,
            ).max_model_calls
        if name == "verification_command":
            value_text = ui_text(
                self.language,
                "settings.overview.command_set" if value else "settings.overview.command_default",
            )
        elif name == "web_search_profile":
            option = next(
                (item for item in self._search_options if item.profile == value),
                None,
            )
            value_text = (
                ui_text(self.language, "settings.web.search_profile_default")
                if value is None
                else str(value)
                if option is None
                else ui_text(
                    self.language,
                    "settings.web.search_profile_option",
                    profile=option.profile,
                    model=option.model,
                )
            )
        elif name == "web_search_mode":
            value_text = ui_text(self.language, f"settings.choice.{self._display_mode(value)}")
        elif name == "web_fetch_mode":
            value_text = ui_text(
                self.language,
                "settings.choice.off" if value == "disabled" else "settings.choice.auto",
            )
        elif isinstance(value, bool):
            value_text = ui_text(
                self.language,
                "settings.choice.enabled" if value else "settings.choice.disabled",
            )
        elif value is None:
            value_text = ui_text(self.language, "settings.overview.command_default")
        else:
            value_text = str(value)
        return ui_text(
            self.language,
            "settings.value_source",
            value=value_text,
            source=ui_text(self.language, f"settings.source.{source.value}"),
        )

    @staticmethod
    def _display_mode(value: object) -> str:
        if value in {"disabled", "off"}:
            return "off"
        if value in {"sidecar", "custom"}:
            return "custom"
        return "auto"

    def _display_value(self, name: str, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        if name == "web_search_mode" and not self.advanced:
            return self._display_mode(value)
        if name == "web_fetch_mode" and not self.advanced:
            return "off" if value == "disabled" else "auto"
        return str(value)

    def _field_value(self, name: str, effective: AgentPreferences, target: AgentPreferences) -> str:
        if self.advanced:
            value = getattr(target, name)
            if name == "web_search_profile" and value not in {
                option.profile for option in self._search_options
            }:
                value = None
            return self._display_value(name, value)
        value = getattr(effective, name)
        if name == "max_steps" and value is None:
            value = ExecutionBudgetPolicy.resolve(
                ExecutionProfile(effective.execution_profile or "normal"),
                max_steps=None,
            ).max_model_calls
        if name == "web_search_profile" and value not in {
            option.profile for option in self._search_options
        }:
            value = None
        return self._display_value(name, value)

    def _profile_choices(self) -> list[tuple[str, str]]:
        options = (
            []
            if self.advanced
            else [(ui_text(self.language, "settings.web.search_profile_choose"), "")]
        )
        options.extend(
            (
                ui_text(
                    self.language,
                    "settings.web.search_profile_option",
                    profile=option.profile,
                    model=option.model,
                ),
                option.profile,
            )
            for option in self._search_options
        )
        return options

    def _choice_options(self, name: str, selected: str) -> list[tuple[str, str]]:
        if name == "web_search_profile":
            profile_options = self._profile_choices()
            if self.advanced:
                profile_options.insert(0, (ui_text(self.language, "settings.extra.inherit"), ""))
            return profile_options
        choices = BASIC_CHOICES.get(name) if not self.advanced else None
        choices = choices or CHOICES.get(name, ())
        control_options: list[tuple[str, str]] = []
        if self.advanced:
            control_options.append((ui_text(self.language, "settings.extra.inherit"), ""))
        control_options.extend(
            (ui_text(self.language, f"settings.choice.{choice}"), choice) for choice in choices
        )
        return control_options

    def _web_status_text(self) -> str:
        inspection = self.web_capabilities
        if inspection is None:
            return ui_text(self.language, "settings.web.status.unknown")
        if inspection.search_availability is WebSearchAvailability.DISABLED:
            return ui_text(self.language, "settings.web.status.disabled")
        if inspection.search_availability is WebSearchAvailability.UNAVAILABLE:
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
            return ui_text(
                self.language,
                "settings.web.status.unavailable",
                reason=ui_text(self.language, reason_key),
            )
        path = ui_text(
            self.language,
            {
                WebSearchExecutionPath.INLINE_HOSTED: "settings.web.path.inline",
                WebSearchExecutionPath.SIDECAR_HOSTED: "settings.web.path.provider",
                WebSearchExecutionPath.SEARCH_API: "settings.web.path.search_api",
            }[inspection.search_path],
        )
        model = "/".join(
            value for value in (inspection.search_profile, inspection.search_model) if value
        )
        status = ui_text(
            self.language,
            "settings.web.status.available",
            path=path,
            model=model or ui_text(self.language, "settings.web.status.model_unknown"),
        )
        if inspection.search_fallback is not None:
            return " · ".join(
                (
                    status,
                    ui_text(
                        self.language,
                        "settings.web.search_fallback",
                        fallback=inspection.search_fallback,
                    ),
                )
            )
        return status

    def _search_provider_blocker(self) -> str:
        if (
            self.web_capabilities is not None
            and self.web_capabilities.search_path is WebSearchExecutionPath.SEARCH_API
        ):
            return ui_text(self.language, "settings.web.search_api_auto")
        if (
            self.web_capabilities is not None
            and self.web_capabilities.search_reason is WebSearchUnavailableReason.TOOL_NOT_ALLOWED
        ):
            return ui_text(self.language, "settings.web.no_search_tool_permission")
        return ui_text(self.language, "settings.web.no_search_provider")

    def _can_manage_search_providers(self) -> bool:
        return self.provider_settings_available and not (
            self.web_capabilities is not None
            and self.web_capabilities.search_reason is WebSearchUnavailableReason.TOOL_NOT_ALLOWED
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-preferences-dialog"):
            yield Label(
                ui_text(self.language, f"settings.extra.{self.category}"),
                id="agent-preferences-title",
            )
            yield Static(
                ui_text(
                    self.language,
                    "settings.extra.advanced_description"
                    if self.advanced
                    else "settings.extra.description",
                ),
                id="agent-preferences-description",
            )
            if self.workspace is not None:
                yield Select(
                    [
                        (ui_text(self.language, "settings.scope.user"), "user"),
                        (ui_text(self.language, "settings.scope.project"), "project"),
                    ],
                    value=self._scope_value,
                    allow_blank=False,
                    id="agent-preferences-scope",
                )
            if self.category == "web-tools":
                yield Static(self._web_status_text(), id="agent-preferences-web-status")
            with VerticalScroll(id="agent-preferences-fields"):
                effective = self._effective()
                target = self._target()
                for name in PREFERENCE_GROUPS[self.category]:
                    selected = self._field_value(name, effective, target)
                    self._baseline[name] = selected
                    yield Label(
                        ui_text(self.language, f"settings.option.{name}"),
                        classes="agent-preference-label",
                    )
                    if name in CHOICES or name == "web_search_profile":
                        control = Select(
                            self._choice_options(name, selected),
                            value=selected,
                            allow_blank=False,
                            id=f"preference-{name}",
                            disabled=(
                                self.resolution.source(name, scope=self._scope_value)
                                is AgentPreferenceSource.CLI
                                or (name == "web_search_profile" and self._mode_is_cli_overridden())
                            ),
                        )
                        yield control
                    else:
                        yield Input(
                            value=selected,
                            id=f"preference-{name}",
                            placeholder=ui_text(
                                self.language,
                                "settings.extra.inherit"
                                if self.advanced
                                else "settings.value.current",
                            ),
                            disabled=(
                                self.resolution.source(name, scope=self._scope_value)
                                is AgentPreferenceSource.CLI
                            ),
                        )
                    yield Static(
                        self._source_text(name, effective),
                        id=f"preference-source-{name}",
                        classes="agent-preference-source",
                    )
                    yield Static(
                        ui_text(self.language, f"settings.option.{name}.help"),
                        classes="agent-preference-help",
                    )
                    if name == "web_search_profile" and not self._search_options:
                        yield Static(
                            self._search_provider_blocker(),
                            id="agent-preferences-no-search-provider",
                            classes="agent-preference-help",
                        )
                        if self._can_manage_search_providers():
                            yield Button(
                                ui_text(self.language, "settings.web.manage_providers"),
                                id="agent-preferences-manage-providers",
                            )
            yield Static("", id="agent-preferences-status")
            with Horizontal(id="agent-preferences-actions"):
                yield Button(ui_text(self.language, "settings.back"), id="agent-preferences-back")
                if self.advanced:
                    yield Button(
                        ui_text(self.language, "settings.extra.reset"),
                        id="agent-preferences-reset",
                    )
                yield Button(
                    ui_text(self.language, "settings.extra.save"), id="agent-preferences-save"
                )

    def _mode_is_cli_overridden(self) -> bool:
        return (
            self.resolution.source("web_search_mode", scope=self._scope_value)
            is AgentPreferenceSource.CLI
        )

    def _refresh_controls(self) -> None:
        effective = self._effective()
        target = self._target()
        self._baseline.clear()
        self._dirty_fields.clear()
        for name in PREFERENCE_GROUPS[self.category]:
            selected = self._field_value(name, effective, target)
            self._baseline[name] = selected
            control = self.query_one(f"#preference-{name}")
            if isinstance(control, (Select, Input)):
                control.value = selected
                control.disabled = self.resolution.source(
                    name, scope=self._scope_value
                ) is AgentPreferenceSource.CLI or (
                    name == "web_search_profile" and self._mode_is_cli_overridden()
                )
            self.query_one(f"#preference-source-{name}", Static).update(
                self._source_text(name, effective)
            )
        if self.category == "web-tools":
            self.query_one("#agent-preferences-web-status", Static).update(self._web_status_text())
        self._update_profile_visibility()

    def _update_profile_visibility(self) -> None:
        if self.category != "web-tools":
            return
        mode = self.query_one("#preference-web_search_mode", Select)
        profile = self.query_one("#preference-web_search_profile", Select)
        profile.display = mode.value == "custom"
        profile.disabled = (
            self._mode_is_cli_overridden()
            or self.resolution.source("web_search_profile", scope=self._scope_value)
            is AgentPreferenceSource.CLI
            or mode.value != "custom"
        )

    def on_mount(self) -> None:
        self._update_profile_visibility()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "agent-preferences-scope":
            scope = str(event.value)
            workspace = self.workspace if scope == "project" else None
            try:
                user = await self.store.load_agent_preferences()
                project = (
                    await self.store.load_agent_preferences(self.workspace)
                    if self.workspace is not None
                    else AgentPreferences()
                )
            except Exception:
                self.query_one("#agent-preferences-scope", Select).value = self._scope_value
                self.query_one("#agent-preferences-status", Static).update(
                    ui_text(self.language, "settings.extra.load_failed")
                )
                return
            self.resolution = replace(self.resolution, user=user, project=project)
            self._scope_value = scope
            self._workspace_scope = workspace
            self._reset_pending = False
            self.preferences = project if scope == "project" else user
            self._refresh_controls()
            self.query_one("#agent-preferences-status", Static).update("")
            return
        if event.select.id == "preference-web_search_mode":
            if self.is_mounted:
                self._dirty_fields.add("web_search_mode")
            self._update_profile_visibility()
            if event.value == "custom" and self._search_options:
                profile = self.query_one("#preference-web_search_profile", Select)
                if not profile.value:
                    profile.value = self._search_options[0].profile
        elif event.select.id == "preference-web_search_profile" and self.is_mounted:
            self._dirty_fields.add("web_search_profile")
        elif event.select.id and event.select.id.startswith("preference-") and self.is_mounted:
            self._dirty_fields.add(event.select.id.removeprefix("preference-"))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("preference-") and self.is_mounted:
            self._dirty_fields.add(event.input.id.removeprefix("preference-"))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _decode_value(self, name: str, raw: str) -> object:
        if name == "web_search_mode" and not self.advanced:
            return {"off": "disabled", "auto": "auto", "custom": "custom"}[raw]
        if name == "web_fetch_mode" and not self.advanced:
            return {"off": "disabled", "auto": "auto"}[raw]
        if not raw:
            return None
        if name in BOOLEAN_FIELDS:
            return raw == "enabled"
        if name in INTEGER_FIELDS:
            return int(raw)
        return raw

    async def _save(self, button: Button) -> None:
        workspace = self._workspace_scope
        try:
            current_target = await self.store.load_agent_preferences(workspace)
        except Exception:
            self.query_one("#agent-preferences-status", Static).update(
                ui_text(self.language, "settings.extra.load_failed")
            )
            return
        values = asdict(current_target)
        if self._reset_pending:
            for name in PREFERENCE_GROUPS[self.category]:
                values[name] = None
        else:
            for name in PREFERENCE_GROUPS[self.category]:
                control = self.query_one(f"#preference-{name}")
                raw = str(control.value).strip() if isinstance(control, (Input, Select)) else ""
                if raw == self._baseline.get(name, "") and name not in self._dirty_fields:
                    continue
                try:
                    values[name] = self._decode_value(name, raw)
                except (KeyError, TypeError, ValueError):
                    control.focus()
                    self.query_one("#agent-preferences-status", Static).update(
                        ui_text(
                            self.language,
                            "settings.extra.invalid",
                            name=ui_text(self.language, f"settings.option.{name}"),
                        )
                    )
                    return
            if self.category in {"web-tools", "web-routing"}:
                mode_was_edited = "web_search_mode" in self._dirty_fields
                profile_was_edited = "web_search_profile" in self._dirty_fields
                if self.advanced:
                    if profile_was_edited and not mode_was_edited:
                        values["web_search_mode"] = "custom"
                    if values["web_search_mode"] != "custom":
                        values["web_search_profile"] = None
                    elif profile_was_edited:
                        values["web_search_profile"] = self.query_one(
                            "#preference-web_search_profile", Select
                        ).value
                elif mode_was_edited or profile_was_edited:
                    visible_mode = str(self.query_one("#preference-web_search_mode", Select).value)
                    if visible_mode == "custom":
                        values["web_search_mode"] = "custom"
                        values["web_search_profile"] = self.query_one(
                            "#preference-web_search_profile", Select
                        ).value
                    else:
                        if mode_was_edited:
                            values["web_search_mode"] = self._decode_value(
                                "web_search_mode", visible_mode
                            )
                        values["web_search_profile"] = None
        try:
            saved = AgentPreferences(**values)
            if saved.web_search_mode == "custom" and saved.web_search_profile not in {
                option.profile for option in self._search_options
            }:
                raise ValueError("select an executable search provider")
        except (TypeError, ValueError):
            self.query_one("#agent-preferences-status", Static).update(
                ui_text(self.language, "settings.web.choose_search_provider")
            )
            return

        updated = replace(
            self.resolution,
            user=saved if workspace is None else self.resolution.user,
            project=saved if workspace is not None else self.resolution.project,
        )
        before = self.resolution.effective(scope=self._scope_value)
        after = updated.effective(scope=self._scope_value)
        reload_required = any(
            getattr(before, name) != getattr(after, name) for name in RUNTIME_RELOAD_FIELDS
        )
        if self.runtime_busy and reload_required:
            self.query_one("#agent-preferences-status", Static).update(
                ui_text(self.language, "settings.extra.wait_for_turn")
            )
            return

        button.disabled = True
        try:
            await self.store.save_agent_preferences(saved, workspace)
        except Exception:
            self.query_one("#agent-preferences-status", Static).update(
                ui_text(self.language, "settings.extra.save_failed")
            )
            return
        finally:
            button.disabled = False

        self.preferences = saved
        self.resolution = updated
        self.dismiss(AgentPreferencesScreenResult(updated, reload_required))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "agent-preferences-back":
            self.dismiss(None)
        elif event.button.id == "agent-preferences-manage-providers":
            self.dismiss(AgentPreferencesScreenResult(manage_providers=True))
        elif event.button.id == "agent-preferences-reset":
            self._reset_pending = True
            for name in PREFERENCE_GROUPS[self.category]:
                control = self.query_one(f"#preference-{name}")
                if isinstance(control, (Select, Input)):
                    control.value = ""
            self.query_one("#agent-preferences-status", Static).update(
                ui_text(self.language, "settings.extra.reset_pending")
            )
        elif event.button.id == "agent-preferences-save":
            await self._save(event.button)


class AgentPreferencesOverview(ModalScreen[None]):
    """Advanced read-only view of effective values and their source layers."""

    CSS = AgentPreferencesScreen.CSS.replace("AgentPreferencesScreen", "AgentPreferencesOverview")
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", show=False)]

    def __init__(
        self,
        startup: AgentPreferences,
        user: AgentPreferences,
        project: AgentPreferences,
        *,
        language: UiLanguage,
        resolution: AgentPreferenceResolution | None = None,
    ) -> None:
        super().__init__()
        self.startup = startup
        self.user = user
        self.project = project
        self.language = language
        self.resolution = resolution or AgentPreferenceResolution(
            defaults=AgentPreferences(), user=user, project=project
        )

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
                    effective = getattr(self.startup, name)
                    source = self.resolution.source(name).value
                    saved = getattr(self.project, name)
                    if saved is None:
                        saved = getattr(self.user, name)

                    def display(value: object, field: str = name) -> str:
                        if field == "verification_command":
                            return ui_text(
                                self.language,
                                "settings.overview.command_set"
                                if value
                                else "settings.overview.command_default",
                            )
                        if value is None:
                            return ui_text(self.language, "settings.extra.inherit")
                        if isinstance(value, bool):
                            return ui_text(
                                self.language,
                                "settings.choice.enabled" if value else "settings.choice.disabled",
                            )
                        if field == "web_search_mode":
                            return ui_text(
                                self.language,
                                f"settings.choice.{AgentPreferencesScreen._display_mode(value)}",
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
                                startup=display(effective),
                                saved=display(saved),
                                source=ui_text(self.language, f"settings.source.{source}"),
                            )
                        ),
                        classes="agent-preference-help",
                    )
            yield Button(ui_text(self.language, "settings.back"), id="agent-preferences-back")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


__all__ = [
    "ADVANCED_CATEGORIES",
    "PREFERENCE_GROUPS",
    "AgentPreferencesOverview",
    "AgentPreferencesScreen",
    "AgentPreferencesScreenResult",
]
