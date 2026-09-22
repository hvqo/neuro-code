"""Appearance changes preserve conversation state and are restored on launch."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pygments.token import Keyword, String
from textual.widgets import Button

from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.infrastructure.persistence.ui_preferences import JsonUiPreferencesStore
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.screens import SettingsScreen, ThemeSettingsScreen
from neuro_code.interfaces.tui.theme import (
    GRAPHITE_SYNTAX_THEME,
    GRAPHITE_THEME,
    MONO_SYNTAX_THEME,
    TEXTUAL_THEME,
    TEXTUAL_THEMES,
    syntax_theme,
)
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, PromptInput
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui import TuiConversation, UiPreferencesFixture


class ThemePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_theme_round_trip_preserves_other_preferences_and_vice_versa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = JsonUiPreferencesStore(path)
            await store.save_theme(UiTheme.GRAPHITE)
            await store.save_language(UiLanguage.SIMPLIFIED_CHINESE)
            await store.save_reasoning_effort(ReasoningEffort.MAX)
            await store.save_interaction_mode(InteractionMode.PLAN)
            restored = JsonUiPreferencesStore(path)
            self.assertEqual(await restored.load_theme(), UiTheme.GRAPHITE)
            await restored.save_theme(UiTheme.PORCELAIN)
            self.assertEqual(await store.load_language(), UiLanguage.SIMPLIFIED_CHINESE)
            self.assertEqual(await store.load_reasoning_effort(), ReasoningEffort.MAX)
            self.assertEqual(await store.load_interaction_mode(), InteractionMode.PLAN)
            self.assertEqual(await store.load_theme(), UiTheme.PORCELAIN)

    async def test_old_missing_and_invalid_theme_preferences_use_porcelain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            store = JsonUiPreferencesStore(path)
            self.assertEqual(await store.load_theme(), UiTheme.PORCELAIN)
            for payload in ["broken", [], {"version": 99}, {"version": 1}] + [
                {"version": 1, "theme": value} for value in [None, "unknown", [], {}, 1]
            ]:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(await store.load_theme(), UiTheme.PORCELAIN)


class TuiThemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_switches_both_ways_without_losing_draft_or_messages(self) -> None:
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            ui_preferences=preferences,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with app.run_test(size=(110, 40)) as pilot:
            app._write_entry("assistant", '**Result**\n\n```python\nreturn "ok"\n```')
            await pilot.pause()
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "保留草稿\nsecond line"
            prompt.cursor_location = (0, 2)
            entries = app.entries
            widgets = tuple(app._entry_widgets)
            for choice, palette, syntax in [
                (UiTheme.GRAPHITE, GRAPHITE_THEME, GRAPHITE_SYNTAX_THEME),
                (UiTheme.PORCELAIN, TEXTUAL_THEME, MONO_SYNTAX_THEME),
            ]:
                if not isinstance(app.screen, SettingsScreen):
                    await app.action_open_settings()
                    await pilot.pause()
                self.assertTrue(await pilot.click("#settings-category-theme"))
                await pilot.pause()
                self.assertIsInstance(app.screen, ThemeSettingsScreen)
                app.screen.query_one(f"#settings-theme-{choice.value}", Button).focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                self.assertEqual(app.theme, choice.textual_name)
                self.assertEqual(app.screen.styles.background.a, 0.25)
                self.assertEqual(app.entries, entries)
                self.assertEqual(tuple(app._entry_widgets), widgets)
                self.assertEqual(prompt.value, "保留草稿\nsecond line")
                self.assertEqual(prompt.cursor_location, (0, 2))
                markdown = app._entry_widgets[-1].renderable
                self.assertIsInstance(markdown, AssistantMarkdown)
                self.assertIs(markdown.code_theme, syntax)
                self.assertEqual(
                    app.console.get_style("markdown.text").color.get_truecolor().hex,
                    palette.variables["text-body"].lower(),
                )
                colors = {
                    segment.style.color.get_truecolor().hex
                    for segment in app.console.render(markdown)
                    if segment.style is not None and segment.style.color is not None
                }
                # The re-render must use the selected palette: the success
                # accent and the foreground of strong text both come from it.
                self.assertIn(palette.success.lower(), colors)
                self.assertIn(palette.variables["fg-primary"].lower(), colors)
            self.assertEqual(preferences.saved_themes, [UiTheme.GRAPHITE, UiTheme.PORCELAIN])
            await pilot.press("escape")
            await pilot.pause()
            self.assertIs(app.focused, prompt)

    async def test_dark_startup_does_not_change_next_apps_light_palette(self) -> None:
        for selected in [UiTheme.GRAPHITE, UiTheme.PORCELAIN]:
            app = NeuroCodeApp(
                TuiConversation(),
                ui_theme=selected,
                provider_name="fixture",
                model_name="fixture-model",
                cwd=Path("/workspace"),
            )
            async with app.run_test(size=(54, 24)) as pilot:
                self.assertEqual(app.theme, selected.textual_name)
                await app.action_open_settings()
                await pilot.pause()
                await pilot.click("#settings-category-theme")
                await pilot.pause()
                self.assertIsInstance(app.screen, ThemeSettingsScreen)
                self.assertEqual(app.focused.id, f"settings-theme-{selected.value}")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                self.assertEqual(app.theme, selected.textual_name)
        self.assertNotEqual(
            GRAPHITE_SYNTAX_THEME.get_style_for_token(Keyword),
            MONO_SYNTAX_THEME.get_style_for_token(Keyword),
        )
        self.assertNotEqual(
            GRAPHITE_SYNTAX_THEME.get_style_for_token(String),
            MONO_SYNTAX_THEME.get_style_for_token(String),
        )

    async def test_failed_save_keeps_applied_theme_and_reports_failure(self) -> None:
        class FailingPreferences(UiPreferencesFixture):
            async def save_theme(self, theme: UiTheme) -> None:
                raise OSError("fixture cannot write")

        app = NeuroCodeApp(
            TuiConversation(),
            ui_preferences=FailingPreferences(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )
        async with app.run_test(size=(110, 40)) as pilot:
            await app._theme_settings_selected(UiTheme.GRAPHITE)
            await pilot.pause()
            self.assertEqual(app.theme, UiTheme.GRAPHITE.textual_name)
            self.assertIn("could not save", app.entries[-1].text)
            self.assertEqual(app.entries[-1].category, "error")

    async def test_every_palette_renders_and_restores_after_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonUiPreferencesStore(Path(directory) / "preferences.json")
            for choice in UiTheme:
                with self.subTest(theme=choice):
                    await store.save_theme(choice)
                    app = NeuroCodeApp(
                        TuiConversation(),
                        ui_theme=await store.load_theme(),
                        provider_name="fixture",
                        model_name="fixture",
                        cwd=Path("/workspace"),
                    )
                    async with app.run_test(size=(80, 24)) as pilot:
                        app._write_entry("assistant", '**Result**\n\n```python\nreturn "ok"\n```')
                        await pilot.pause()
                        self.assertEqual(UiTheme.from_textual_name(app.theme), choice)
                        self.assertEqual(app.ansi_color, choice is UiTheme.SYSTEM)
                        self.assertTrue(list(app.console.render(app._entry_widgets[-1].renderable)))
                        if choice is UiTheme.SYSTEM:
                            self.assertTrue(app.console.get_style("markdown.text").color.is_default)
                            background = syntax_theme(app).get_background_style().bgcolor
                            assert background is not None
                            # ANSI bright black keeps code blocks off the canvas.
                            self.assertFalse(background.is_default)
                            self.assertEqual(background.number, 8)
                            self.assertEqual(app.get_theme(app.theme).background, "ansi_default")
                        else:
                            self.assertEqual(
                                app.screen.styles.background.hex.lower(),
                                TEXTUAL_THEMES[choice].background.lower(),
                            )

    def test_system_theme_separates_surfaces_from_the_terminal_canvas(self) -> None:
        theme = TEXTUAL_THEMES[UiTheme.SYSTEM]

        self.assertEqual(theme.background, "ansi_default")
        self.assertEqual(theme.surface, "ansi_bright_black")
        self.assertEqual(theme.variables["border"], "ansi_bright_black")
        self.assertEqual(theme.variables["composer-surface"], "ansi_bright_black")
        self.assertEqual(theme.variables["user-message-surface"], "ansi_bright_black")
        self.assertNotEqual(theme.surface, theme.background)

    async def test_preview_scroll_and_escape_restore_original_without_saving(self) -> None:
        preferences = UiPreferencesFixture()
        app = NeuroCodeApp(
            TuiConversation(),
            ui_preferences=preferences,
            provider_name="fixture",
            model_name="fixture",
            cwd=Path("/workspace"),
        )
        async with app.run_test(size=(54, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "中文草稿\nkeep this"
            prompt.cursor_location = (1, 3)
            await app._settings_category_selected("theme")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(app.theme, UiTheme.ONE_DARK.textual_name)
            focused = app.screen.query_one("#settings-theme-one-dark", Button)
            viewport = app.screen.query_one("#settings-themes")
            self.assertTrue(viewport.region.contains_region(focused.region))
            self.assertEqual(preferences.saved_themes, [])
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.theme, UiTheme.PORCELAIN.textual_name)
            self.assertEqual(preferences.saved_themes, [])
            self.assertEqual(prompt.value, "中文草稿\nkeep this")
            self.assertEqual(prompt.cursor_location, (1, 3))
            await pilot.click("#settings-category-theme")
            await pilot.pause()
            app.screen.query_one("#settings-theme-system", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.theme, UiTheme.SYSTEM.textual_name)
            self.assertEqual(preferences.saved_themes, [UiTheme.SYSTEM])
            await pilot.click("#settings-category-theme")
            await pilot.pause()
            self.assertEqual(app.focused.id, "settings-theme-system")

    async def test_send_button_uses_existing_submission_and_command_pipeline(self) -> None:
        runner = TuiConversation()
        app = NeuroCodeApp(
            runner,
            language=UiLanguage.SIMPLIFIED_CHINESE,
            provider_name="fixture",
            model_name="fixture",
            cwd=Path("/workspace"),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            self.assertEqual(len(list(app.query("#prompt-caption"))), 0)
            await pilot.pause(0.25)  # Settle layout and the preceding click animation.
            self.assertTrue(await pilot.click("#prompt-send"))
            self.assertEqual(runner.prompts, [])
            prompt.value = "帮我检查代码"
            prompt.disabled = True
            await pilot.pause(0.25)  # Settle layout and the preceding click animation.
            self.assertTrue(await pilot.click("#prompt-send"))
            self.assertEqual(runner.prompts, [])
            prompt.disabled = False
            await pilot.pause(0.25)  # Settle layout and the preceding click animation.
            self.assertTrue(await pilot.click("#prompt-send"))
            await pilot.pause()
            self.assertEqual(runner.prompts, ["帮我检查代码"])
            self.assertEqual(prompt.value, "")
            prompt.value = "/settings"
            await pilot.pause(0.25)  # Settle layout and the preceding click animation.
            self.assertTrue(await pilot.click("#prompt-send"))
            await pilot.pause()
            self.assertIsInstance(app.screen, SettingsScreen)
            self.assertEqual(runner.prompts, ["帮我检查代码"])
