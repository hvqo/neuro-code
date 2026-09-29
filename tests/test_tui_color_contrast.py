"""V1A semantic color guards; geometry remains owned by the existing TUI."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
    assert theme.surface != theme.background
    assert roles["surface-selected"] != theme.surface
    assert roles["composer-surface"] == theme.surface
    assert roles["user-message-surface"] == theme.surface
    for foreground, background in (
        (roles["text-primary"], theme.surface),
        (roles["text-secondary"], theme.surface),
        (roles["text-muted"], theme.surface),
        (roles["composer-muted"], roles["composer-surface"]),
        (roles["composer-border"], roles["composer-surface"]),
        (roles["user-message-border"], roles["user-message-surface"]),
        (roles["border"], theme.surface),
        (roles["border-subtle"], theme.surface),
    ):
        assert foreground != background
    assert roles["border-focus"] != roles["border"]
    assert roles["text-secondary-intensity"] == "dim"
    assert roles["text-muted-intensity"] == "dim"
    owner = SimpleNamespace(app=SimpleNamespace(theme=UiTheme.SYSTEM.textual_name))
    assert theme_style(owner, TEXT_SECONDARY) == "dim default"
    assert markdown_theme(owner).styles["markdown.block_quote"].dim
    # Terminal ANSI values are unknown; an RGB contrast calculation here would
    # pretend to guarantee a luminance relationship that the app cannot know.


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
