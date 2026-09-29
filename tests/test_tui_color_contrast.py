"""V1A semantic color guards; geometry remains owned by the existing TUI."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.widgets import Button

from neuro_code.interfaces.tui.theme import (
    TEXT_SECONDARY,
    TEXTUAL_THEMES,
    markdown_theme,
    theme_style,
)
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app, populate_fixture, show_fixture_screen


def _luminance(hex_color: str) -> float:
    assert hex_color.startswith("#")
    assert len(hex_color) == 7
    channels = (int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5))
    linear = (
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    red, green, blue = linear
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("choice", [UiTheme.GRAPHITE, UiTheme.PORCELAIN])
def test_flagship_text_hierarchy_and_contrast(choice: UiTheme) -> None:
    theme = TEXTUAL_THEMES[choice]
    roles = theme.variables

    assert theme.background != theme.surface
    assert theme.surface != roles["surface-selected"]
    assert theme.surface != roles["surface-subtle"]
    assert roles["border"] != theme.surface
    assert roles["border-focus"] != roles["border"]
    assert roles["composer-surface"] != theme.background
    assert roles["composer-muted"] != roles["composer-surface"]
    assert roles["user-message-surface"] != roles["text-primary"]
    assert roles["border-dim"] == roles["border-subtle"]
    assert roles["bg-0"] == theme.background
    assert roles["bg-1"] == theme.surface

    for background in (
        theme.surface,
        roles["surface-selected"],
        roles["composer-surface"],
        roles["user-message-surface"],
    ):
        primary, secondary, muted = (
            _contrast(roles[name], background)
            for name in ("text-primary", "text-secondary", "text-muted")
        )
        assert primary > secondary > muted >= 4.5, (choice, background)

    # Structural edges and focus are visible without turning subtle dividers
    # into high-contrast decoration. WCAG ratios here are engineering guards.
    assert _contrast(roles["border"], theme.surface) >= 3
    assert _contrast(roles["border-focus"], theme.surface) >= 3


def test_system_uses_terminal_semantics_without_foreground_fill_collision() -> None:
    theme = TEXTUAL_THEMES[UiTheme.SYSTEM]
    roles = theme.variables

    assert theme.background == "ansi_default"
    assert theme.foreground == "ansi_default"
    assert theme.surface == theme.background
    assert theme.panel == theme.background
    assert theme.boost == theme.background
    assert roles["composer-surface"] == theme.surface
    assert roles["user-message-surface"] == theme.surface
    assert roles["surface-selected"] == theme.background
    broad_surfaces = (
        theme.background,
        theme.surface,
        theme.panel,
        theme.boost,
        roles["bg-0"],
        roles["bg-1"],
        roles["bg-2"],
        roles["bg-3"],
        roles["surface-hover"],
        roles["surface-subtle"],
        roles["surface-selected"],
        roles["composer-surface"],
        roles["user-message-surface"],
        roles["footer-background"],
        roles["scrollbar-background"],
    )
    assert set(broad_surfaces) == {"ansi_default"}
    assert "ansi_bright_black" not in {*roles.values(), *broad_surfaces}
    # ANSI default foreground and background are separate terminal channels.
    # Muted prose relies on dim intensity and has no surface fill of its own.
    assert roles["text-muted"] == "ansi_default"
    assert roles["text-muted-intensity"] == "dim"
    assert roles["border"] == "ansi_white"
    assert roles["border-subtle"] == "ansi_white"
    assert roles["border-focus"] == "ansi_bright_blue"
    assert roles["text-secondary-intensity"] == "dim"
    assert roles["text-muted-intensity"] == "dim"
    assert roles["composer-border"] == "ansi_white"
    assert roles["composer-focus-border"] == "ansi_bright_blue"
    assert roles["user-message-border"] == "ansi_white"
    assert roles["button-focus-text-style"] == "bold reverse"
    assert roles["selected-button-text-style"] == "bold reverse"
    assert roles["input-selection-background"] == "ansi_blue"
    owner = SimpleNamespace(app=SimpleNamespace(theme=UiTheme.SYSTEM.textual_name))
    assert theme_style(owner, TEXT_SECONDARY) == "dim default"
    assert markdown_theme(owner).styles["markdown.block_quote"].dim
    # Terminal ANSI values are unknown; an RGB contrast calculation here would
    # pretend to guarantee a luminance relationship that the app cannot know.


@pytest.mark.parametrize("choice", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
def test_markdown_inline_code_has_no_surface_background(choice: UiTheme) -> None:
    owner = SimpleNamespace(app=SimpleNamespace(theme=choice.textual_name))
    inline_code = markdown_theme(owner).styles["markdown.code"]

    assert inline_code.color is not None
    assert inline_code.bgcolor is None

    # Code blocks keep their independent background contract in the flagship
    # themes; removing the inline-code fill must not erase block treatment.
    if choice in (UiTheme.GRAPHITE, UiTheme.PORCELAIN):
        assert markdown_theme(owner).styles["markdown.code_block"].bgcolor is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["user-assistant", "settings"])
async def test_theme_switch_changes_palette_without_moving_widgets(fixture: str) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture=fixture)
    async with app.run_test(size=(100, 32)) as pilot:
        populate_fixture(app, fixture)
        show_fixture_screen(app, fixture, UiTheme.GRAPHITE)
        await pilot.pause()

        selectors = (
            (
                "#header",
                "#transcript",
                ".message-user",
                ".message-assistant",
                "#composer",
                "#prompt-surface",
                "#prompt",
            )
            if fixture == "user-assistant"
            else ("#settings-dialog", "#settings-navigation", "#settings-search")
        )

        def regions() -> tuple[tuple[int, int, int, int], ...]:
            return tuple(tuple(app.screen.query_one(selector).region) for selector in selectors)

        original = regions()
        original_surface = app.get_theme(app.theme).surface
        for choice in (UiTheme.PORCELAIN, UiTheme.SYSTEM):
            app.theme = choice.textual_name
            await pilot.pause()
            assert app.get_theme(app.theme).surface != original_surface
            assert regions() == original


@pytest.mark.asyncio
async def test_system_settings_selection_uses_reverse_without_a_filled_surface() -> None:
    app = make_app(UiTheme.SYSTEM, fixture="settings")
    async with app.run_test(size=(100, 32)) as pilot:
        populate_fixture(app, "settings")
        show_fixture_screen(app, "settings", UiTheme.SYSTEM)
        await pilot.pause()

        selected = app.screen.query_one("#settings-navigation Button.active", Button)
        selected.focus()
        await pilot.pause()

        assert selected.styles.text_style.bold
        assert selected.styles.text_style.reverse
        assert selected.styles.background.hex == "ansi_default"
