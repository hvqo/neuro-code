"""Agent preferences settings-flow tests.

Agent 偏好设置流程测试.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Select

from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.screens.agent_preferences import (
    AgentPreferencesOverview,
    AgentPreferencesScreen,
)
from tests.test_tui import TuiConversation, UiPreferencesFixture


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
        await pilot.click("#settings-category-preferences-overview")
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
