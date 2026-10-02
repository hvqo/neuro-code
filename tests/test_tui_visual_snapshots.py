from __future__ import annotations

import os
from pathlib import Path
from tempfile import gettempdir
from unittest.mock import patch

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.terminal_palette import (
    TerminalColorLevel,
    TerminalPalette,
)
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.syntax_theme import SyntaxTheme
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import (
    VISUAL_FIXTURES,
    make_app,
    populate_fixture,
    show_fixture_screen,
)

VIEWPORTS = ((120, 40), (100, 32), (80, 24))
THEMES = (UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM)
SNAPSHOT_ROOT = Path(__file__).parent / "visual" / "snapshots"
_TITLE = "Neuro Code visual baseline"
SYSTEM_PALETTE_FIXTURES = (
    (
        "dark",
        TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30)),
    ),
    (
        "light",
        TerminalPalette(TerminalColorLevel.TRUECOLOR, (30, 30, 30), (246, 246, 246)),
    ),
    ("unknown", TerminalPalette()),
)


def snapshot_name(fixture: str, theme: UiTheme, viewport: tuple[int, int]) -> str:
    width, height = viewport
    return f"{fixture}__{theme.value}__{width}x{height}.svg"


async def capture_snapshot(
    fixture: str,
    theme: UiTheme,
    viewport: tuple[int, int],
    *,
    terminal_palette: TerminalPalette | None = None,
    syntax_choice: SyntaxTheme = SyntaxTheme.AUTO,
) -> str:
    # Textual selects its output filter at construction. Pin color output for
    # visual review rather than inheriting the caller's NO_COLOR policy. Real
    # application startup still honors that policy without any modification.
    environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
    with patch.dict(os.environ, environment, clear=True):
        app = make_app(
            theme, fixture=fixture, terminal_palette=terminal_palette, syntax_choice=syntax_choice
        )

    def fixed_clock(instance: NeuroCodeApp) -> None:
        instance.query_one("#clock", Static).update("13:37")

    with patch.object(NeuroCodeApp, "_update_clock", fixed_clock):
        async with app.run_test(size=viewport) as pilot:
            populate_fixture(app, fixture)
            if fixture in {"single-line-composer", "multiline-composer", "long-composer"}:
                app.query_one("#prompt", PromptInput).focus()
            elif fixture == "idle-composer":
                app.query_one("#prompt", PromptInput).blur()
            show_fixture_screen(app, fixture, theme)
            # Let Textual complete a layout pass. No timers, provider calls, or
            # animation-driven states are part of these fixtures.
            await pilot.pause()
            if (
                fixture == "mixed-language-long-answer"
                or fixture.startswith("markdown-")
                or (fixture.startswith("syntax-") and fixture != "syntax-settings")
            ):
                # Review the opening reading hierarchy and user/assistant axis.
                # Normal long responses may auto-follow their bottom edge.
                app.query_one("#transcript", VerticalScroll).scroll_home(
                    animate=False, immediate=True
                )
                await pilot.pause()
            return app.export_screenshot(title=_TITLE, simplify=True)


def canonicalize_svg(svg: str) -> str:
    """Drop exporter-only line-end padding without changing rendered SVG."""

    return "\n".join(line.rstrip() for line in svg.splitlines()).rstrip() + "\n"


@pytest.mark.parametrize("fixture", VISUAL_FIXTURES)
@pytest.mark.parametrize("theme", THEMES, ids=lambda theme: theme.value)
@pytest.mark.parametrize("viewport", VIEWPORTS, ids=lambda size: f"{size[0]}x{size[1]}")
@pytest.mark.asyncio
async def test_tui_visual_snapshot(
    fixture: str,
    theme: UiTheme,
    viewport: tuple[int, int],
) -> None:
    actual = canonicalize_svg(await capture_snapshot(fixture, theme, viewport))
    path = SNAPSHOT_ROOT / snapshot_name(fixture, theme, viewport)

    if os.environ.get("NEURO_TUI_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as snapshot_file:
            snapshot_file.write(actual)
        return

    received = Path(gettempdir()) / "neuro-code-tui-visual-received" / path.name
    received.parent.mkdir(parents=True, exist_ok=True)
    with received.open("w", encoding="utf-8", newline="\n") as received_file:
        received_file.write(actual)

    expected = path.read_text(encoding="utf-8") if path.exists() else "<missing snapshot>"
    assert actual == expected, (
        f"Visual baseline differs: {path}\n"
        f"Rendered output saved to: {received}\n"
        "Inspect the SVG or render the visual gallery, then update intentionally with:\n"
        "NEURO_TUI_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_visual_snapshots.py -q"
    )


@pytest.mark.parametrize(("palette_name", "terminal_palette"), SYSTEM_PALETTE_FIXTURES)
@pytest.mark.parametrize(
    "fixture",
    [
        "empty-conversation",
        "user-assistant",
        "settings",
        "syntax-python",
        "syntax-diff",
        "syntax-settings",
    ],
)
@pytest.mark.asyncio
async def test_tui_system_terminal_palette_snapshot(
    palette_name: str,
    terminal_palette: TerminalPalette,
    fixture: str,
) -> None:
    actual = canonicalize_svg(
        await capture_snapshot(
            fixture,
            UiTheme.SYSTEM,
            (100, 32),
            terminal_palette=terminal_palette,
        )
    )
    path = SNAPSHOT_ROOT / f"system-palette-{palette_name}__{fixture}__100x32.svg"
    if os.environ.get("NEURO_TUI_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as snapshot_file:
            snapshot_file.write(actual)
        return

    received = Path(gettempdir()) / "neuro-code-tui-visual-received" / path.name
    received.parent.mkdir(parents=True, exist_ok=True)
    received.write_text(actual, encoding="utf-8", newline="\n")
    expected = path.read_text(encoding="utf-8") if path.exists() else "<missing snapshot>"
    assert actual == expected, (
        f"Visual baseline differs: {path}\n"
        f"Rendered output saved to: {received}\n"
        "Update intentionally with NEURO_TUI_UPDATE_SNAPSHOTS=1 and the visual snapshot test."
    )


@pytest.mark.parametrize("choice", list(SyntaxTheme), ids=lambda choice: choice.value)
@pytest.mark.parametrize("theme", THEMES, ids=lambda theme: theme.value)
@pytest.mark.asyncio
async def test_tui_syntax_selection_snapshot(choice: SyntaxTheme, theme: UiTheme) -> None:
    # Explicit previews include a deterministic detected dark System palette;
    # unknown/light System have their own separate palette fixtures above.
    palette = SYSTEM_PALETTE_FIXTURES[0][1] if theme is UiTheme.SYSTEM else None
    actual = canonicalize_svg(
        await capture_snapshot(
            "syntax-settings", theme, (100, 32), terminal_palette=palette, syntax_choice=choice
        )
    )
    path = SNAPSHOT_ROOT / f"syntax-choice-{choice.value}__{theme.value}__100x32.svg"
    if os.environ.get("NEURO_TUI_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        return
    received = Path(gettempdir()) / "neuro-code-tui-visual-received" / path.name
    received.parent.mkdir(parents=True, exist_ok=True)
    received.write_text(actual, encoding="utf-8", newline="\n")
    assert actual == path.read_text(encoding="utf-8"), (
        f"Visual syntax baseline differs: {path}; actual: {received}"
    )


@pytest.mark.asyncio
async def test_visual_color_output_is_independent_of_callers_no_color_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    with_policy = await capture_snapshot("syntax-python", UiTheme.GRAPHITE, (100, 32))
    assert os.environ["NO_COLOR"] == "1"
    monkeypatch.delenv("NO_COLOR")
    without_policy = await capture_snapshot("syntax-python", UiTheme.GRAPHITE, (100, 32))
    assert with_policy == without_policy
    # Protect the final model of rendered colors, not merely resolver objects.
    assert "fill: #ff7b72" in with_policy
    assert "fill: #a5d6ff" in with_policy
