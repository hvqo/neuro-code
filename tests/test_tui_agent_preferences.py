"""Agent preferences settings-flow tests.

Agent 偏好设置流程测试.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Button, Select, Static

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
from neuro_code.application.ports.web_search import WebSearchExecutionPath
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.screens.agent_preferences import (
    AgentPreferencesOverview,
    AgentPreferencesScreen,
)
from neuro_code.shared.ui_language import UiLanguage
from tests.test_tui import TuiConversation, UiPreferencesFixture


def _search_inspection(
    *,
    providers: tuple[RuntimeSearchProviderOption, ...] = (),
) -> RuntimeWebCapabilityInspection:
    return RuntimeWebCapabilityInspection(
        WebSearchAvailability.UNAVAILABLE,
        WebSearchExecutionPath.UNAVAILABLE,
        WebSearchUnavailableReason.NO_COMPATIBLE_PROVIDER,
        search_providers=providers,
    )


async def test_settings_opens_the_agent_preferences_form_and_saves_values() -> None:
    store = UiPreferencesFixture()
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=Path("/tmp"),
    )
    async with app.run_test(size=(120, 44)) as pilot:
        await app.action_open_settings()
        await pilot.pause()
        await pilot.click("#settings-category-input")
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, AgentPreferencesScreen):
                break

        assert isinstance(app.screen, AgentPreferencesScreen)
        screen = app.screen
        screen.query_one("#preference-enter_behavior", Select).value = "newline"
        await pilot.click("#agent-preferences-save")
        for _ in range(20):
            await pilot.pause()
            if store.saved_agent_preferences:
                break

        assert len(store.saved_agent_preferences) == 1
        saved, workspace = store.saved_agent_preferences[0]
        assert saved.enter_behavior == "newline"
        # The default scope is the user level (no project workspace selected).
        assert workspace is None


async def test_agent_preferences_save_to_the_selected_project_scope() -> None:
    store = UiPreferencesFixture()
    workspace = Path("/tmp/neuro-code-settings-project")
    resolution = AgentPreferenceResolution(
        defaults=AgentPreferences(enter_behavior="send", prompt_soft_wrap=True)
    )
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        preference_resolution=resolution,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=workspace,
    )
    screen = AgentPreferencesScreen(
        "input",
        resolution.effective(),
        store,
        language=UiLanguage.ENGLISH,
        workspace=workspace,
        resolution=resolution,
    )

    async with app.run_test(size=(120, 44)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#agent-preferences-scope", Select).value = "project"
        await pilot.pause()
        screen.query_one("#preference-enter_behavior", Select).value = "newline"
        await pilot.click("#agent-preferences-save")
        await pilot.pause()

    saved, scope = store.saved_agent_preferences[0]
    assert scope == workspace
    assert saved.enter_behavior == "newline"


async def test_scope_switch_discards_an_unsaved_reset() -> None:
    store = UiPreferencesFixture()
    workspace = Path("/tmp/neuro-code-settings-project")
    store.agent_preferences = AgentPreferences(
        compaction_recent_items=8,
        compaction_summary_tokens=420,
    )
    store.project_agent_preferences = AgentPreferences(
        compaction_recent_items=14,
        compaction_summary_tokens=760,
    )
    resolution = AgentPreferenceResolution(
        defaults=AgentPreferences(
            compaction_recent_items=6,
            compaction_summary_tokens=300,
        ),
        user=store.agent_preferences,
        project=store.project_agent_preferences,
    )
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        preference_resolution=resolution,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=workspace,
    )
    screen = AgentPreferencesScreen(
        "context",
        resolution.effective(),
        store,
        language=UiLanguage.ENGLISH,
        workspace=workspace,
        resolution=resolution,
    )

    async with app.run_test(size=(120, 44)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        await pilot.click("#agent-preferences-reset")
        screen.query_one("#agent-preferences-scope", Select).value = "project"
        await pilot.pause()
        await pilot.click("#agent-preferences-save")
        await pilot.pause()

    saved, scope = store.saved_agent_preferences[0]
    assert scope == workspace
    assert saved.compaction_recent_items == 14
    assert saved.compaction_summary_tokens == 760


async def test_settings_opens_the_preferences_overview() -> None:
    store = UiPreferencesFixture()
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=Path("/tmp"),
    )
    async with app.run_test(size=(120, 44)) as pilot:
        await app.action_open_settings()
        await pilot.pause()
        await pilot.click("#settings-nav-advanced")
        await pilot.pause()
        category = app.screen.query_one("#settings-category-preferences-overview", Button)
        category.focus()
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, AgentPreferencesOverview):
                break

        assert isinstance(app.screen, AgentPreferencesOverview)
        rows = app.screen.query(".agent-preference-help")
        assert rows
        await pilot.click("#agent-preferences-back")
        for _ in range(20):
            await pilot.pause()
            if not isinstance(app.screen, AgentPreferencesOverview):
                break

        assert not isinstance(app.screen, AgentPreferencesOverview)


async def test_runtime_settings_reload_retains_the_active_session_identity() -> None:
    runner = TuiConversation()
    runner._session_id = "session-fixture"
    app = NeuroCodeApp(
        runner,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=Path("/tmp"),
    )

    async with app.run_test(size=(120, 44)):
        app._request_runtime_configuration_reload()

        assert app.runtime_reload_session_id == "session-fixture"
        assert app.return_code == 75


def test_agent_preference_resolution_keeps_scope_and_cli_source() -> None:
    resolution = AgentPreferenceResolution(
        defaults=AgentPreferences(web_search_mode="auto", failover=True),
        user=AgentPreferences(
            web_search_mode="custom", web_search_profile="search", failover=False
        ),
        project=AgentPreferences(web_search_mode="auto"),
        cli=AgentPreferences(failover=True),
    )

    project = resolution.effective(scope="project")
    all_projects = resolution.effective(scope="user")

    assert project.web_search_mode == "auto"
    assert project.web_search_profile is None
    assert project.failover is True
    assert resolution.source("web_search_mode") is AgentPreferenceSource.PROJECT
    assert resolution.source("failover") is AgentPreferenceSource.CLI
    assert all_projects.web_search_mode == "custom"
    assert all_projects.web_search_profile == "search"
    assert resolution.source("web_search_mode", scope="user") is AgentPreferenceSource.USER

    explicit_profile = AgentPreferenceResolution(
        defaults=AgentPreferences(execution_profile="normal"),
        user=AgentPreferences(execution_profile="deep", max_steps=50),
        cli=AgentPreferences(execution_profile="deep"),
    )
    explicit_max_steps = AgentPreferenceResolution(
        defaults=AgentPreferences(execution_profile="normal"),
        user=AgentPreferences(execution_profile="deep"),
        cli=AgentPreferences(max_steps=12),
    )

    assert explicit_profile.effective().execution_profile == "deep"
    assert explicit_profile.effective().max_steps is None
    assert explicit_profile.source("max_steps") is AgentPreferenceSource.CLI
    assert explicit_max_steps.effective().execution_profile == "normal"
    assert explicit_max_steps.effective().max_steps == 12
    assert explicit_max_steps.source("execution_profile") is AgentPreferenceSource.CLI


async def test_simple_web_settings_show_effective_values_and_executable_custom_profile() -> None:
    store = UiPreferencesFixture()
    resolution = AgentPreferenceResolution(
        defaults=AgentPreferences(web_search_mode="auto", web_fetch_mode="disabled")
    )
    capability = _search_inspection(
        providers=(RuntimeSearchProviderOption("search", "gemini-3.6-flash"),)
    )
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=Path("/tmp"),
        preference_resolution=resolution,
    )
    screen = AgentPreferencesScreen(
        "web-tools",
        resolution.effective(),
        store,
        language=UiLanguage.ENGLISH,
        resolution=resolution,
        web_capabilities=capability,
    )

    async with app.run_test(size=(120, 44)) as pilot:
        app.push_screen(screen)
        await pilot.pause()

        mode = screen.query_one("#preference-web_search_mode", Select)
        assert mode.value == "auto"
        assert "inherit" not in {
            value for _, value in screen._choice_options("web_search_mode", "auto")
        }
        assert "source: Default" in str(
            screen.query_one("#preference-source-web_search_mode", Static).renderable
        )
        assert screen.query_one("#preference-web_search_profile", Select).display is False


async def test_custom_search_profile_save_requests_runtime_reload() -> None:
    store = UiPreferencesFixture()
    resolution = AgentPreferenceResolution(
        defaults=AgentPreferences(web_search_mode="auto", web_fetch_mode="disabled")
    )
    capability = RuntimeWebCapabilityInspection(
        WebSearchAvailability.DISABLED,
        WebSearchExecutionPath.DISABLED,
        search_providers=(RuntimeSearchProviderOption("search", "gemini-3.6-flash"),),
    )
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=Path("/tmp"),
        preference_resolution=resolution,
    )
    result: list[object] = []
    screen = AgentPreferencesScreen(
        "web-tools",
        resolution.effective(),
        store,
        language=UiLanguage.ENGLISH,
        resolution=resolution,
        web_capabilities=capability,
    )

    async with app.run_test(size=(120, 44)) as pilot:
        app.push_screen(screen, result.append)
        await pilot.pause()
        screen.query_one("#preference-web_search_mode", Select).value = "custom"
        screen.query_one("#preference-web_search_profile", Select).value = "search"
        screen._dirty_fields.update({"web_search_mode", "web_search_profile"})
        await pilot.click("#agent-preferences-save")
        await pilot.pause()

    saved, scope = store.saved_agent_preferences[0]
    assert scope is None
    assert saved.web_search_mode == "custom"
    assert saved.web_search_profile == "search"
    assert result[0].reload_required is True


async def test_unavailable_search_shows_fail_closed_status_and_provider_entry() -> None:
    store = UiPreferencesFixture()
    resolution = AgentPreferenceResolution(
        defaults=AgentPreferences(web_search_mode="auto", web_fetch_mode="disabled")
    )
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=Path("/tmp"),
    )
    screen = AgentPreferencesScreen(
        "web-tools",
        resolution.effective(),
        store,
        language=UiLanguage.ENGLISH,
        resolution=resolution,
        web_capabilities=_search_inspection(),
        provider_settings_available=True,
    )

    async with app.run_test(size=(120, 44)) as pilot:
        app.push_screen(screen)
        await pilot.pause()

        assert "unavailable" in str(
            screen.query_one("#agent-preferences-web-status", Static).renderable
        )
        assert "No executable Search provider" in str(
            screen.query_one("#agent-preferences-no-search-provider", Static).renderable
        )
        assert screen.query_one("#agent-preferences-manage-providers", Button)
        assert [value for _, value in screen._choice_options("web_search_mode", "auto")] == [
            "off",
            "auto",
            "custom",
        ]


async def test_search_permission_blocker_does_not_suggest_provider_changes() -> None:
    store = UiPreferencesFixture()
    resolution = AgentPreferenceResolution(
        defaults=AgentPreferences(web_search_mode="auto", web_fetch_mode="disabled")
    )
    inspection = RuntimeWebCapabilityInspection(
        WebSearchAvailability.UNAVAILABLE,
        WebSearchExecutionPath.UNAVAILABLE,
        WebSearchUnavailableReason.TOOL_NOT_ALLOWED,
    )
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        provider_name="fixture",
        model_name="fixture-model",
        cwd=Path("/tmp"),
    )
    screen = AgentPreferencesScreen(
        "web-tools",
        resolution.effective(),
        store,
        language=UiLanguage.ENGLISH,
        resolution=resolution,
        web_capabilities=inspection,
        provider_settings_available=True,
    )

    async with app.run_test(size=(120, 44)) as pilot:
        app.push_screen(screen)
        await pilot.pause()

        blocker = str(screen.query_one("#agent-preferences-no-search-provider", Static).renderable)
        assert "tool policy" in blocker
        assert not list(screen.query("#agent-preferences-manage-providers"))
