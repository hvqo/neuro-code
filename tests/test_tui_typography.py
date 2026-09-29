"""V1B reading roles are shared across themes without changing TUI geometry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pygments.token import Keyword, Name
from rich.console import Console
from rich.style import Style

from neuro_code.interfaces.tui.theme import (
    TEXT_MUTED,
    TOOL_DETAIL_STYLE,
    TOOL_META_STYLE,
    markdown_theme,
    syntax_theme,
    theme_style,
)
from neuro_code.interfaces.tui.widgets import AssistantMarkdown
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app


@pytest.mark.parametrize("choice", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
def test_markdown_reading_hierarchy_is_regular_except_explicit_emphasis(
    choice: UiTheme,
) -> None:
    owner = SimpleNamespace(app=SimpleNamespace(theme=choice.textual_name))
    styles = markdown_theme(owner).styles

    for role in ("paragraph", "text", "h2", "h3", "h4", "h5", "h6", "block_quote"):
        assert not styles[f"markdown.{role}"].bold, (choice, role)
    for role in ("item.bullet", "item.number", "table.header", "code"):
        assert not styles[f"markdown.{role}"].bold, (choice, role)
    assert styles["markdown.h1"].bold
    assert styles["markdown.strong"].bold
    assert not styles["markdown.block_quote"].italic
    assert styles["markdown.code"].bgcolor is None
    assert styles["markdown.code_block"].bgcolor is not None

    metadata = Style.parse(theme_style(owner, TOOL_META_STYLE))
    detail = Style.parse(theme_style(owner, TOOL_DETAIL_STYLE))
    assert TOOL_META_STYLE == TEXT_MUTED
    assert not metadata.bold
    assert not detail.bold
    if choice is UiTheme.SYSTEM:
        # Unknown ANSI palettes can resolve both roles to terminal-default dim;
        # V1B keeps V1A's fail-soft palette rather than inventing RGB contrast.
        assert metadata.dim
    else:
        assert metadata != detail

    code_theme = syntax_theme(owner)
    assert not code_theme.get_style_for_token(Keyword).bold
    assert not code_theme.get_style_for_token(Name.Function).bold


def test_markdown_h1_stays_on_the_left_reading_axis() -> None:
    console = Console(width=40, record=True, force_terminal=False)
    console.print(AssistantMarkdown("# Review result"))
    heading = next(line for line in console.export_text().splitlines() if "Review result" in line)
    assert heading.startswith("Review result")


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
async def test_user_and_error_messages_are_regular_while_borders_keep_state(
    choice: UiTheme,
) -> None:
    app = make_app(choice, fixture="error")
    async with app.run_test(size=(100, 32)) as pilot:
        app._write_entry("user", "请检查 `theme.py` 与测试结果。")
        app._write_entry("error", "Provider unavailable; retry after checking settings.")
        await pilot.pause()

        user = app.query_one(".message-user")
        error = app.query_one(".message-error")
        assert not user.styles.text_style.bold
        assert not error.styles.text_style.bold
        assert user.styles.border_left is not None
        assert error.styles.border_left is not None
        for name, value in (
            ("provider", "fixture-provider"),
            ("path", "src/neuro_code/interfaces/tui/theme.py"),
            ("duration", "120ms"),
            ("steps", 3),
            ("status", "completed"),
        ):
            style = app._semantic_value_style(name, value)
            assert style is not None
            assert not Style.parse(style).bold, (choice, name)
