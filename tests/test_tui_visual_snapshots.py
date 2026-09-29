from __future__ import annotations

import os
from pathlib import Path
from tempfile import gettempdir
from unittest.mock import patch

import pytest
from textual.widgets import Static

from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.widgets import PromptInput
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


def snapshot_name(fixture: str, theme: UiTheme, viewport: tuple[int, int]) -> str:
    width, height = viewport
    return f"{fixture}__{theme.value}__{width}x{height}.svg"


async def capture_snapshot(
    fixture: str,
    theme: UiTheme,
    viewport: tuple[int, int],
) -> str:
    app = make_app(theme, fixture=fixture)

    def fixed_clock(instance: NeuroCodeApp) -> None:
        instance.query_one("#clock", Static).update("13:37")

    with patch.object(NeuroCodeApp, "_update_clock", fixed_clock):
        async with app.run_test(size=viewport) as pilot:
            populate_fixture(app, fixture)
            if fixture == "multiline-composer":
                app.query_one("#prompt", PromptInput).focus()
            show_fixture_screen(app, fixture, theme)
            # Let Textual complete a layout pass. No timers, provider calls, or
            # animation-driven states are part of these fixtures.
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
