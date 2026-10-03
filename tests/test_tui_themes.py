"""Appearance changes preserve conversation state and are restored on launch."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pygments.token import Keyword, String
from textual.widgets import Button, TextArea

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
    def test_detached_prompt_change_event_is_ignored(self) -> None:
        app = NeuroCodeApp(
            TuiConversation(),
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/workspace"),
        )

        # Textual may deliver a queued Changed message after a test/app screen
        # has been detached. The stale event must not make the TUI handler ask
        # the detached prompt for its screen.
        app.on_text_area_changed(TextArea.Changed(PromptInput()))

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

            async def wait_for_theme_focus(choice: UiTheme) -> None:
                for _ in range(100):
                    screen = app.screen
                    if isinstance(screen, ThemeSettingsScreen):
                        target = next(iter(screen.query(f"#settings-theme-{choice.value}")), None)
                        if target is not None and target.is_mounted and app.focused is target:
                            return
                    await pilot.pause(0.05)
                self.fail(f"theme choice {choice.value} was not mounted and focused")

            async def wait_for_theme_applied(choice: UiTheme) -> None:
                for _ in range(100):
                    if (
                        app.theme == choice.textual_name
                        and preferences.saved_themes[-1:] == [choice]
                        and isinstance(app.screen, SettingsScreen)
                    ):
                        return
                    await pilot.pause(0.05)
                self.fail(f"theme choice {choice.value} was not applied and persisted")

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
                initial_choice = app.screen.selected
                await wait_for_theme_focus(initial_choice)
                target = app.screen.query_one(f"#settings-theme-{choice.value}", Button)
                self.assertTrue(target.is_mounted)
                target.focus()
                await wait_for_theme_focus(choice)
                await pilot.press("enter")
                await wait_for_theme_applied(choice)
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
                # Code uses the independently resolved syntax foreground;
                # strong prose still uses the selected UI foreground.
                self.assertIn(syntax.get_style_for_token(String).color.get_truecolor().hex, colors)
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
                for _ in range(100):
                    if isinstance(app.screen, ThemeSettingsScreen):
                        target = next(
                            iter(app.screen.query(f"#settings-theme-{selected.value}")), None
                        )
                        if target is not None and target.is_mounted and app.focused is target:
                            break
                    await pilot.pause(0.05)
                else:
                    self.fail("selected theme button was not mounted and focused")
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
                            # System does not assume any ANSI color is a dark fill.
                            self.assertTrue(background.is_default)
                            self.assertEqual(app.get_theme(app.theme).background, "ansi_default")
                        else:
                            self.assertEqual(
                                app.screen.styles.background.hex.lower(),
                                TEXTUAL_THEMES[choice].background.lower(),
                            )

    def test_system_theme_separates_surfaces_from_the_terminal_canvas(self) -> None:
        theme = TEXTUAL_THEMES[UiTheme.SYSTEM]

        self.assertEqual(theme.background, "ansi_default")
        self.assertEqual(theme.surface, "ansi_default")
        self.assertEqual(theme.variables["border"], "ansi_white")
        self.assertEqual(theme.variables["border-subtle"], "ansi_white")
        self.assertEqual(theme.variables["composer-surface"], "ansi_default")
        self.assertEqual(theme.variables["composer-border"], "ansi_white")
        self.assertEqual(theme.variables["composer-muted"], "ansi_default")
        self.assertEqual(theme.variables["user-message-surface"], "ansi_default")
        self.assertEqual(theme.variables["user-message-border"], "ansi_white")
        self.assertEqual(theme.variables["text-muted-intensity"], "dim")
        self.assertEqual(theme.surface, theme.background)
        self.assertEqual(theme.variables["surface-selected"], "ansi_default")
        self.assertEqual(theme.variables["button-focus-text-style"], "reverse")

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

            async def wait_for_theme_applied(selected: UiTheme) -> None:
                for _ in range(100):
                    if app.theme == selected.textual_name and preferences.saved_themes[-1:] == [
                        selected
                    ]:
                        return
                    # The modal dismissal callback applies and persists the
                    # choice asynchronously after the key event is dispatched.
                    await pilot.pause(0.05)
                self.fail("selected theme was not applied and persisted")

            async def wait_for_settings_screen() -> None:
                for _ in range(100):
                    screen = app.screen
                    if isinstance(screen, SettingsScreen):
                        button = next(iter(screen.query("#settings-category-theme")), None)
                        hit_test_ready = False
                        if (
                            button is not None
                            and button.region.width > 0
                            and button.region.height > 0
                        ):
                            center_x = button.region.x + button.region.width // 2
                            center_y = button.region.y + button.region.height // 2
                            if screen.region.contains(center_x, center_y):
                                # A restored screen can report mounted/focused before a
                                # closing modal or screen transition stops covering the
                                # button. Pilot.click returns False until hit testing
                                # actually resolves the button at its click point.
                                hit_test_ready = app.get_widget_at(center_x, center_y)[0] is button
                        if (
                            screen._settings_view_ready
                            and button is not None
                            and button.is_mounted
                            and button.visible
                            and button.region.width > 0
                            and button.region.height > 0
                            and app.focused is button
                            and hit_test_ready
                        ):
                            return
                    await pilot.pause(0.05)
                self.fail("settings theme entry was not interactive after modal restoration")

            async def wait_for_theme_screen(
                selected: UiTheme, *, require_focus: bool = False
            ) -> None:
                for _ in range(100):
                    screen = app.screen
                    if isinstance(screen, ThemeSettingsScreen):
                        target = next(iter(screen.query(f"#settings-theme-{selected.value}")), None)
                        if (
                            target is not None
                            and target.is_mounted
                            and (not require_focus or app.focused is target)
                        ):
                            return
                    # The Windows runner can have a delayed Textual message pump
                    # and screen children under the full suite; yield a bounded
                    # interval until the selected button is actually mounted.
                    await pilot.pause(0.05)
                self.fail("selected theme button was not mounted and focused")

            async def wait_for_visible_theme(choice: UiTheme) -> None:
                for _ in range(100):
                    screen = app.screen
                    if isinstance(screen, ThemeSettingsScreen):
                        target = next(iter(screen.query(f"#settings-theme-{choice.value}")), None)
                        viewport = next(iter(screen.query("#settings-themes")), None)
                        if (
                            target is not None
                            and viewport is not None
                            and app.focused is target
                            and app.theme == choice.textual_name
                            and viewport.region.contains_region(target.region)
                        ):
                            return
                    # Textual schedules focus-driven scrolling after the key event;
                    # wait for the viewport geometry rather than assuming one
                    # event-loop turn is enough on every platform.
                    await pilot.pause(0.05)
                self.fail(f"focused theme choice {choice.value} did not become visible")

            prompt = app.query_one("#prompt", PromptInput)
            prompt.value = "中文草稿\nkeep this"
            prompt.cursor_location = (1, 3)
            await app._settings_category_selected("theme")
            await wait_for_theme_screen(UiTheme.PORCELAIN, require_focus=True)
            await pilot.press("up")
            await wait_for_visible_theme(UiTheme.ONE_DARK)
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
            await wait_for_settings_screen()
            self.assertTrue(await pilot.click("#settings-category-theme"))
            await wait_for_theme_screen(UiTheme.PORCELAIN, require_focus=True)
            app.screen.query_one("#settings-theme-system", Button).focus()
            await pilot.press("enter")
            await wait_for_theme_applied(UiTheme.SYSTEM)
            await wait_for_settings_screen()
            self.assertEqual(app.theme, UiTheme.SYSTEM.textual_name)
            self.assertEqual(preferences.saved_themes, [UiTheme.SYSTEM])
            self.assertTrue(await pilot.click("#settings-category-theme"))
            await wait_for_theme_screen(UiTheme.SYSTEM, require_focus=True)
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
