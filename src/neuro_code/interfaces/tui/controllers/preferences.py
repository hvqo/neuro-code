from __future__ import annotations

from contextlib import suppress
from functools import partial

from textual.screen import ModalScreen

from neuro_code.application.ports.provider_settings import (
    ManagedProviderSettings,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.screens import (
    BackgroundWakeSettingsScreen,
    InteractionModeScreen,
    LanguageSettingsScreen,
    NetworkProxySettingsScreen,
    PermissionSettingsScreen,
    ProviderSettingsScreen,
    ReasoningEffortScreen,
    SettingsScreen,
    ThemeSettingsScreen,
)
from neuro_code.interfaces.tui.screens.agent_preferences import (
    PREFERENCE_GROUPS,
    AgentPreferencesOverview,
    AgentPreferencesScreen,
)
from neuro_code.interfaces.tui.state import (
    TUI_RELOAD_PROVIDER_SETTINGS,
    ProviderSettingsSubmission,
    permission_level_key,
)
from neuro_code.interfaces.tui.text import language_name
from neuro_code.interfaces.tui.theme import markdown_theme
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.shared.ui_theme import UiTheme


class PreferencesControllerMixin(TuiAppControllerMixin):
    _settings_last_category: str | None = None

    async def action_select_reasoning_effort(self) -> None:
        await self._select_reasoning_effort(None)

    async def action_cycle_interaction_mode(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_previous()
            return
        await self._apply_interaction_mode(self._interaction_mode.next)

    async def action_open_settings(self) -> None:
        self.push_screen(
            SettingsScreen(
                self._language,
                language=self._language,
                provider_settings_available=(
                    self._managed_provider_settings is not None
                    and self._provider_settings_store is not None
                ),
                reasoning_effort=self._reasoning_effort,
                interaction_mode=self._interaction_mode,
                permission_level=permission_level_key(
                    self._interaction_mode,
                    auto_unrestricted=self._auto_mode_unrestricted,
                ),
                ui_theme=UiTheme.from_textual_name(self.theme),
                initial_category=self._settings_last_category,
                provider_settings=self._managed_provider_settings,
            ),
            self._settings_category_selected,
        )

    async def _settings_category_selected(self, category: str | None) -> None:
        self._settings_last_category = category
        if category == "preferences-overview":
            if self._ui_preferences is not None:
                try:
                    user = await self._ui_preferences.load_agent_preferences()
                    project = await self._ui_preferences.load_agent_preferences(self._cwd)
                except Exception:
                    self._write_ui_entry("error", "settings.extra.load_failed")
                else:
                    self.push_screen(
                        AgentPreferencesOverview(
                            self._agent_preferences, user, project, language=self._language
                        ),
                        self._agent_preferences_closed,
                    )
                    return
            await self.action_open_settings()
            return
        if category in PREFERENCE_GROUPS:
            if self._ui_preferences is None:
                self._write_ui_entry("error", "settings.extra.unavailable")
                await self.action_open_settings()
                return
            try:
                preferences = await self._ui_preferences.load_agent_preferences()
            except Exception:
                self._write_ui_entry("error", "settings.extra.load_failed")
                await self.action_open_settings()
                return
            self.push_screen(
                AgentPreferencesScreen(
                    category,
                    preferences,
                    self._ui_preferences,
                    language=self._language,
                    workspace=self._cwd,
                ),
                self._agent_preferences_closed,
            )
            return
        if category == "theme":
            original = UiTheme.from_textual_name(self.theme)
            self.push_screen(
                ThemeSettingsScreen(
                    original, language=self._language, preview=self._apply_ui_theme
                ),
                partial(self._theme_settings_selected, original=original),
            )
            return
        if category == "language":
            self.push_screen(
                LanguageSettingsScreen(self._language, language=self._language),
                self._language_settings_selected,
            )
            return
        if category == "agent-reasoning":
            await self._select_reasoning_effort(None, return_to_settings=True)
            return
        if category == "agent-interaction-mode":
            await self._select_interaction_mode(None, return_to_settings=True)
            return
        if category == "agent-permissions":
            await self._select_permission_approval()
            return
        if category == "providers":
            if self._managed_provider_settings is None or self._provider_settings_store is None:
                return
            self.push_screen(
                ProviderSettingsScreen(
                    language=self._language,
                    provider_settings=self._managed_provider_settings,
                    provider_settings_store=self._provider_settings_store,
                    provider_catalog=self._provider_catalog,
                    socks_supported=self._socks_supported,
                ),
                self._provider_settings_selected,
            )
            return
        if category == "network":
            if self._managed_provider_settings is None or self._provider_settings_store is None:
                return
            self.push_screen(
                NetworkProxySettingsScreen(
                    language=self._language,
                    provider_settings=self._managed_provider_settings,
                    provider_settings_store=self._provider_settings_store,
                    socks_supported=self._socks_supported,
                ),
                self._network_proxy_settings_selected,
            )
            return
        if category == "background-wake":
            if self._managed_provider_settings is None or self._provider_settings_store is None:
                return
            self.push_screen(
                BackgroundWakeSettingsScreen(
                    language=self._language,
                    provider_settings=self._managed_provider_settings,
                    provider_settings_store=self._provider_settings_store,
                ),
                self._background_wake_settings_selected,
            )

    async def _agent_preferences_closed(self, result: None) -> None:
        if self._ui_preferences is not None:
            with suppress(Exception):
                self._agent_preferences = (
                    await self._ui_preferences.load_effective_agent_preferences(self._cwd)
                )
        await self.action_open_settings()

    def _apply_ui_theme(self, selected: UiTheme) -> None:
        if self.theme == selected.textual_name:
            return
        self.theme = selected.textual_name
        self.console.pop_theme()
        self.console.push_theme(markdown_theme(self))
        self._refresh_header()
        self._refresh_localized_interface()
        self._refresh_turn_activity()

    async def _theme_settings_selected(
        self,
        selected: UiTheme | None,
        *,
        original: UiTheme | None = None,
    ) -> None:
        if selected is None:
            if original is not None:
                self._apply_ui_theme(original)
            await self.action_open_settings()
            return
        self._apply_ui_theme(selected)
        if self._ui_preferences is not None:
            try:
                await self._ui_preferences.save_theme(selected)
            except Exception as error:
                self._write_ui_entry(
                    "error", "settings.theme.save_failed", error=f"{type(error).__name__}: {error}"
                )
        await self.action_open_settings()

    async def _provider_settings_selected(
        self,
        result: ProviderSettingsSubmission | None,
    ) -> None:
        if result is not None:
            self.exit(return_code=TUI_RELOAD_PROVIDER_SETTINGS)
            return
        await self.action_open_settings()

    async def _network_proxy_settings_selected(
        self,
        settings: ManagedProviderSettings | None,
    ) -> None:
        if settings is not None:
            self.exit(return_code=TUI_RELOAD_PROVIDER_SETTINGS)
            return
        await self.action_open_settings()

    async def _background_wake_settings_selected(
        self,
        settings: ManagedProviderSettings | None,
    ) -> None:
        if settings is not None:
            self.exit(return_code=TUI_RELOAD_PROVIDER_SETTINGS)
            return
        await self.action_open_settings()

    async def _select_reasoning_effort(
        self,
        requested: ReasoningEffort | None,
        *,
        return_to_settings: bool = False,
    ) -> None:
        if self._reasoning_controller is None:
            self._write_ui_entry("error", "effort.unavailable")
            if return_to_settings:
                await self.action_open_settings()
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "effort.switch_running")
            if return_to_settings:
                await self.action_open_settings()
            return
        if requested is None:
            self.push_screen(
                ReasoningEffortScreen(
                    self._reasoning_effort,
                    language=self._language,
                ),
                partial(self._reasoning_effort_selected, return_to_settings=return_to_settings),
            )
            return
        await self._apply_reasoning_effort(requested)

    async def _select_interaction_mode(
        self,
        requested: InteractionMode | None,
        *,
        return_to_settings: bool = False,
    ) -> None:
        if self._interaction_mode_controller is None:
            self._write_ui_entry("error", "mode.unavailable")
            if return_to_settings:
                await self.action_open_settings()
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "mode.switch_running")
            if return_to_settings:
                await self.action_open_settings()
            return
        if requested is None:
            self.push_screen(
                InteractionModeScreen(
                    self._interaction_mode,
                    language=self._language,
                ),
                partial(self._interaction_mode_selected, return_to_settings=return_to_settings),
            )
            return
        await self._apply_interaction_mode(requested)

    async def _select_permission_approval(self) -> None:
        if self._interaction_mode_controller is None:
            self._write_ui_entry("error", "mode.unavailable")
            await self.action_open_settings()
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "mode.switch_running")
            await self.action_open_settings()
            return
        self.push_screen(
            PermissionSettingsScreen(
                permission_level_key(
                    self._interaction_mode,
                    auto_unrestricted=self._auto_mode_unrestricted,
                ),
                language=self._language,
            ),
            self._permission_approval_selected,
        )

    async def _permission_approval_selected(
        self,
        selected: tuple[InteractionMode, bool] | None,
    ) -> None:
        if selected is None:
            await self.action_open_settings()
            return
        mode, unrestricted_auto = selected
        await self._apply_interaction_mode(mode, unrestricted_auto=unrestricted_auto)
        await self.action_open_settings()

    async def _reasoning_effort_selected(
        self,
        effort: ReasoningEffort | None,
        *,
        return_to_settings: bool = False,
    ) -> None:
        if effort is not None:
            await self._apply_reasoning_effort(effort)

        if return_to_settings:
            await self.action_open_settings()

    async def _interaction_mode_selected(
        self,
        mode: InteractionMode | None,
        *,
        return_to_settings: bool = False,
    ) -> None:
        if mode is not None:
            await self._apply_interaction_mode(mode)

        if return_to_settings:
            await self.action_open_settings()

    async def _apply_reasoning_effort(self, effort: ReasoningEffort) -> None:
        assert self._reasoning_controller is not None
        try:
            result = await self._reasoning_controller.set_reasoning_effort(effort)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._reasoning_effort = result.requested
        self._effective_reasoning_effort = result.effective
        if result.changed:
            self._ultracode_decision = None
        self._refresh_runtime_bar()
        if not result.changed:
            self._write_ui_entry(
                "status",
                (
                    "effort.already_selected_ultracode"
                    if result.requested is ReasoningEffort.ULTRACODE
                    else "effort.already_selected"
                ),
                glyph=result.requested.glyph,
                effort=result.requested.value,
                requested=result.requested.value,
            )
            return
        if result.requested is ReasoningEffort.ULTRACODE:
            self._write_ui_entry(
                "status",
                "effort.changed_ultracode",
                requested=result.requested.value,
            )
        else:
            self._write_ui_entry(
                "status",
                "effort.changed",
                glyph=result.requested.glyph,
                effort=result.requested.value,
            )
        if self._ui_preferences is not None:
            try:
                await self._ui_preferences.save_reasoning_effort(result.requested)
            except Exception as error:
                self._write_ui_entry(
                    "error",
                    "effort.save_failed",
                    error=f"{type(error).__name__}: {error}",
                )

    async def _apply_interaction_mode(
        self,
        mode: InteractionMode,
        *,
        unrestricted_auto: bool = False,
    ) -> None:
        if self._interaction_mode_controller is None:
            self._write_ui_entry("error", "mode.unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "mode.switch_running")
            return
        try:
            result = await self._interaction_mode_controller.set_interaction_mode(
                mode, unrestricted_auto=unrestricted_auto
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._interaction_mode = result.requested
        self._auto_mode_unrestricted = result.auto_unrestricted
        self._refresh_runtime_bar()
        if not result.changed:
            self._write_ui_entry(
                "status",
                "mode.already_selected",
                glyph=result.requested.glyph,
                mode=result.requested.value,
            )
            return
        if result.requested is InteractionMode.AUTO and result.auto_unrestricted:
            key = "mode.changed_full"
        elif result.limited_auto:
            key = "mode.changed_auto_limited"
        else:
            key = "mode.changed"
        self._write_ui_entry(
            "status",
            key,
            glyph=result.requested.glyph,
            mode=result.requested.value,
        )
        if self._ui_preferences is not None:
            try:
                await self._ui_preferences.save_interaction_mode(result.requested)
            except Exception as error:
                self._write_ui_entry(
                    "error",
                    "mode.save_failed",
                    error=f"{type(error).__name__}: {error}",
                )

    async def _language_settings_selected(
        self,
        result: UiLanguage | None,
    ) -> None:
        language = result
        if language is None:
            await self.action_open_settings()
            return
        if language is self._language:
            await self.action_open_settings()
            return
        self._language = language
        self._refresh_localized_interface()
        if self._ui_preferences is not None:
            try:
                await self._ui_preferences.save_language(language)
            except Exception as error:
                self._write_ui_entry(
                    "error",
                    "settings.save_failed",
                    error=f"{type(error).__name__}: {error}",
                )
                await self.action_open_settings()
                return
        self._write_ui_entry(
            "system",
            "settings.changed",
            language=language_name(language, in_language=language),
        )
        await self.action_open_settings()
