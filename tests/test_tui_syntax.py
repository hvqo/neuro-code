"""Syntax selection, readable code projection and UI state preservation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pygments.token import (
    Comment,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Text,
)
from rich.console import Console
from rich.syntax import Syntax
from textual.widgets import Select, Static

from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.infrastructure.persistence.ui_preferences import JsonUiPreferencesStore
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.screens import SettingsScreen, SyntaxThemeSettingsScreen
from neuro_code.interfaces.tui.syntax import SYNTAX_THEMES, contrast_ratio, resolve_syntax_theme
from neuro_code.interfaces.tui.terminal_palette import TerminalColorLevel, TerminalPalette
from neuro_code.interfaces.tui.theme import markdown_theme, syntax_theme
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, PromptInput
from neuro_code.shared.syntax_theme import SyntaxTheme
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui import TuiConversation, UiPreferencesFixture
from tests.visual.syntax_fixtures import SOURCES

TOKENS = (
    Keyword,
    Keyword.Type,
    String,
    Number,
    Comment,
    Name.Function,
    Name.Class,
    Name.Variable,
    Operator,
    Punctuation,
    Name.Builtin,
    Name.Constant,
    Name.Decorator,
    Generic.Inserted,
    Generic.Deleted,
    Generic.Subheading,
    Generic.Heading,
    Text,
)


@pytest.mark.parametrize("choice", list(SyntaxTheme))
@pytest.mark.parametrize(
    ("background", "foreground"),
    [("#202020", "#eeeeee"), ("#ffffff", "#262320"), ("#777777", "#000000")],
)
def test_curated_tokens_are_readable_and_never_own_surface(
    choice: SyntaxTheme, background: str, foreground: str
) -> None:
    theme = resolve_syntax_theme(choice, background, foreground)
    assert theme.get_background_style().bgcolor.get_truecolor().hex == background
    styles = [theme.get_style_for_token(token) for token in TOKENS]
    for style in styles:
        assert style.bgcolor is None
        assert not style.bold
        assert contrast_ratio(style.color.get_truecolor().hex, background) >= 4.5
    assert len({style.color for style in styles}) >= 4
    assert (
        theme.get_style_for_token(Generic.Inserted).color
        != theme.get_style_for_token(Generic.Deleted).color
    )
    assert (
        theme.get_style_for_token(Generic.Inserted).color != theme.get_style_for_token(Text).color
    )
    assert (
        theme.get_style_for_token(Generic.Subheading).color != theme.get_style_for_token(Text).color
    )
    assert resolve_syntax_theme(choice, background, foreground) is theme


def test_auto_uses_actual_surface_brightness_and_terminal_fallback() -> None:
    assert (
        resolve_syntax_theme(SyntaxTheme.AUTO, "#202020", "#eeeeee").resolved_choice
        is SyntaxTheme.GITHUB_DARK
    )
    assert (
        resolve_syntax_theme(SyntaxTheme.AUTO, "#ffffff", "#262320").resolved_choice
        is SyntaxTheme.FRIENDLY
    )
    theme = resolve_syntax_theme(SyntaxTheme.DRACULA, "default", "default")
    assert theme.terminal_fallback
    assert theme.get_background_style().bgcolor.is_default
    assert theme.get_style_for_token(Name.Variable).color.is_default
    assert theme.get_style_for_token(Comment).dim
    for token in TOKENS:
        style = theme.get_style_for_token(token)
        assert style.bgcolor is None
        assert style.color.type.name in {"DEFAULT", "STANDARD"}
    assert set(SYNTAX_THEMES) == set(SyntaxTheme)


@pytest.mark.parametrize("language", list(SOURCES))
def test_mature_lexers_preserve_source_and_unknown_is_plain(language: str) -> None:
    lexer = language if language != "unknown" else "neuro-unknown-language"
    source = SOURCES[language]
    theme = resolve_syntax_theme(SyntaxTheme.AUTO, "#202020", "#eeeeee")
    highlighted = Syntax(source, lexer, theme=theme).highlight(source)
    assert highlighted.plain.rstrip("\n") == source.rstrip("\n")
    if language == "unknown":
        assert len({span.style for span in highlighted.spans}) <= 1
    else:
        assert len({span.style.color for span in highlighted.spans}) >= 3


@pytest.mark.parametrize(
    "language", ["python", "rust,no_run", "json title=example", "shell", "diff", "unavailable"]
)
def test_fenced_markdown_metadata_and_unknown_render_without_exceptions(language: str) -> None:
    console = Console(width=80, force_terminal=True, color_system="truecolor")
    theme = resolve_syntax_theme(SyntaxTheme.MONOKAI, "#ffffff", "#262320")
    markdown = AssistantMarkdown(
        f'Before `AGENTS.md`.\n\n```{language}\nanswer = "审查", 42\n```\n\nAfter.',
        code_theme=theme,
    )
    rendered = list(console.render(markdown))
    plain = "".join(segment.text for segment in rendered)
    assert 'answer = "审查", 42' in plain
    assert "Before" in plain
    assert "After" in plain


@pytest.mark.asyncio
async def test_syntax_preference_migration_round_trip_and_independent_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preferences.json"
    store = JsonUiPreferencesStore(path)
    assert await store.load_syntax_theme() is SyntaxTheme.AUTO
    for raw in [None, "bad", {}, [], 3]:
        path.write_text(json.dumps({"version": 1, "syntax_theme": raw, "theme": "graphite"}))
        assert await store.load_syntax_theme() is SyntaxTheme.AUTO
    for invalid in ["not-json", json.dumps({"version": 99}), "[]"]:
        path.write_text(invalid)
        assert await store.load_syntax_theme() is SyntaxTheme.AUTO
    path.write_text(json.dumps({"version": 1, "custom": "preserve", "theme": "graphite"}))
    for choice in SyntaxTheme:
        await store.save_syntax_theme(choice)
        restored = JsonUiPreferencesStore(path)
        assert await restored.load_syntax_theme() is choice
        assert await restored.load_theme() is UiTheme.GRAPHITE
    await store.save_syntax_theme(SyntaxTheme.DRACULA)
    await store.save_theme(UiTheme.PORCELAIN)
    await store.save_language(UiLanguage.SIMPLIFIED_CHINESE)
    await store.save_reasoning_effort(ReasoningEffort.MAX)
    await store.save_agent_preferences(await store.load_agent_preferences())
    assert await JsonUiPreferencesStore(path).load_syntax_theme() is SyntaxTheme.DRACULA
    assert json.loads(path.read_text())["custom"] == "preserve"


@pytest.mark.parametrize("ui_theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
@pytest.mark.parametrize("viewport", [(120, 40), (100, 32), (80, 24)])
@pytest.mark.asyncio
async def test_live_preview_cancel_save_preserves_prose_draft_and_geometry(
    ui_theme: UiTheme, viewport: tuple[int, int]
) -> None:
    store = UiPreferencesFixture()
    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=store,
        ui_theme=ui_theme,
        provider_name="fixture",
        model_name="fixture",
        cwd=Path("/workspace"),
    )
    async with app.run_test(size=viewport) as pilot:

        async def wait_for_settings_entries() -> None:
            for _ in range(100):
                screen = app.screen
                if isinstance(screen, SettingsScreen):
                    entry = next(iter(screen.query("#settings-entry-language")), None)
                    if entry is not None and entry.is_mounted:
                        return
                await pilot.pause(0.05)
            pytest.fail("Settings entries were not mounted after the settings screen refresh")

        app._write_entry("user", "Review this.")
        app._write_entry("assistant", "Before `AGENTS.md`.\n\n```python\nreturn 42\n```")
        prompt = app.query_one("#prompt", PromptInput)
        prompt.text = "保留草稿\nsecond line"
        prompt.move_cursor((0, 2))
        await pilot.pause()
        entries, widgets = app.entries, tuple(app._entry_widgets)
        geometry = [widget.region for widget in widgets] + [app.query_one("#prompt-surface").region]
        prose = markdown_theme(app).styles
        await app._settings_category_selected("syntax-theme")
        await pilot.pause()
        assert isinstance(app.screen, SyntaxThemeSettingsScreen)
        app.screen.query_one("#syntax-choice", Select).value = SyntaxTheme.MONOKAI.value
        await pilot.pause()
        assert app._syntax_theme is SyntaxTheme.MONOKAI
        assert isinstance(app.screen.query_one("#syntax-preview", Static).renderable, Syntax)
        assert app.entries == entries
        assert tuple(app._entry_widgets) == widgets
        assert markdown_theme(app).styles == prose
        assert prompt.text == "保留草稿\nsecond line"
        assert prompt.cursor_location == (0, 2)
        assert [widget.region for widget in widgets] + [
            app.screen_stack[0].query_one("#prompt-surface").region
        ] == geometry
        await pilot.press("escape")
        await wait_for_settings_entries()
        assert app._syntax_theme is SyntaxTheme.AUTO
        assert not store.saved_syntax_themes
        assert isinstance(app.screen, SettingsScreen)
        await app._settings_category_selected("syntax-theme")
        await pilot.pause()
        app.screen.query_one("#syntax-choice", Select).value = SyntaxTheme.FRIENDLY.value
        await pilot.pause()
        app.screen.query_one("#syntax-settings-save").press()
        await wait_for_settings_entries()
        assert store.saved_syntax_themes == [SyntaxTheme.FRIENDLY]
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen.syntax_theme is SyntaxTheme.FRIENDLY
        assert app.theme == ui_theme.textual_name


@pytest.mark.asyncio
async def test_save_failure_is_visible_and_pending_prose_uses_preview() -> None:
    class FailingPreferences(UiPreferencesFixture):
        async def save_syntax_theme(self, theme: SyntaxTheme) -> None:
            raise OSError("fixture cannot write")

    app = NeuroCodeApp(
        TuiConversation(),
        ui_preferences=FailingPreferences(),
        provider_name="fixture",
        model_name="fixture",
        cwd=Path("/workspace"),
    )
    async with app.run_test() as pilot:
        app._assistant_parts = ["```python\nreturn 42\n```"]
        pending = Static()
        await app.screen.mount(pending)
        app._pending_assistant = pending
        await app._syntax_settings_selected(SyntaxTheme.DRACULA, original=SyntaxTheme.AUTO)
        await pilot.pause()
        assert app._syntax_theme is SyntaxTheme.DRACULA
        assert "could not save" in app.entries[-1].text
        assert pending.renderable.code_theme is syntax_theme(app)


@pytest.mark.asyncio
async def test_ui_switch_retains_explicit_syntax_choice_and_code_geometry() -> None:
    app = NeuroCodeApp(
        TuiConversation(),
        syntax_theme=SyntaxTheme.DRACULA,
        provider_name="fixture",
        model_name="fixture",
        cwd=Path("/workspace"),
    )
    async with app.run_test(size=(100, 32)) as pilot:
        app._write_entry("assistant", "Prose `AGENTS.md`.\n\n```python\nreturn 42\n```")
        await pilot.pause()
        initial_geometry = [widget.region for widget in app._entry_widgets]
        for ui_theme in (UiTheme.PORCELAIN, UiTheme.GRAPHITE, UiTheme.SYSTEM):
            app._apply_ui_theme(ui_theme)
            await pilot.pause()
            assert app._syntax_theme is SyntaxTheme.DRACULA
            resolved = syntax_theme(app)
            assert resolved.choice is SyntaxTheme.DRACULA
            assert app._entry_widgets[0].renderable.code_theme is resolved
            assert [widget.region for widget in app._entry_widgets] == initial_geometry
            assert markdown_theme(app).styles["markdown.code"].bgcolor is None


@pytest.mark.parametrize(
    "palette",
    [
        TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30)),
        TerminalPalette(TerminalColorLevel.ANSI256, (30, 30, 30), (246, 246, 246)),
        TerminalPalette(TerminalColorLevel.ANSI16, (232, 232, 232), (30, 30, 30)),
        TerminalPalette(),
    ],
)
def test_system_resolver_uses_existing_palette_capability(palette: TerminalPalette) -> None:
    owner = SimpleNamespace(
        app=SimpleNamespace(
            theme=UiTheme.SYSTEM.textual_name,
            terminal_palette=palette,
            _syntax_theme=SyntaxTheme.AUTO,
        )
    )
    theme = syntax_theme(owner)
    assert theme.terminal_fallback is (
        palette.color_level in {TerminalColorLevel.ANSI16, TerminalColorLevel.UNKNOWN}
    )
    assert markdown_theme(owner).styles["markdown.code"].bgcolor is None
