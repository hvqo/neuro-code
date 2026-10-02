"""Capture the shipped widget; clock freezing is confined to this test harness."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from unittest.mock import patch

from rich.color import blend_rgb
from rich.terminal_theme import SVG_EXPORT_THEME
from tests.visual.showcases import make_app
from textual.widgets import Static

from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.empty_state import EmptyStateIdentity
from neuro_code.interfaces.tui.empty_state_reveal import VORTEX_FRAMES
from neuro_code.shared.ui_theme import UiTheme

KEY_FRAMES = {
    "resting": 0,
    "lift": 32,
    "max-tilt": 72,
    "reconstruction": 111,
    "settle": 136,
    "returned-resting": 144,
}


def fixed_clock(app: NeuroCodeApp) -> None:
    app.query_one("#clock", Static).update("13:37")


def freeze_frame(symbol: EmptyStateIdentity, index: int, clock: list[float]) -> None:
    clock[0] = 100 + (sum(f.duration_ms for f in VORTEX_FRAMES[:index]) + 0.01) / 1000
    symbol._advance_animation(symbol._animation_generation)
    # Production uses one-shot timers; freeze only this capture, not the app.
    if symbol._timer is not None:
        symbol._timer.stop()
        symbol._timer = None


async def capture_frames(theme: UiTheme, viewport: tuple[int, int]) -> dict[int, str]:
    # Match the existing visual baseline policy, independent of a developer's
    # NO_COLOR setting. Production still honors that setting without alteration.
    environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
    with patch.dict(os.environ, environment, clear=True):
        app = make_app(theme, fixture="empty-conversation")
    clock = [100.0]
    with (
        patch.object(NeuroCodeApp, "_update_clock", fixed_clock),
        patch.object(EmptyStateIdentity, "_motion_allowed", return_value=True),
        patch("neuro_code.interfaces.tui.empty_state.monotonic", side_effect=lambda: clock[0]),
    ):
        async with app.run_test(size=viewport) as pilot:
            await pilot.pause()
            symbol = app.query_one(EmptyStateIdentity)
            await pilot.click(symbol)
            captures = {}
            for index in KEY_FRAMES.values():
                freeze_frame(symbol, index, clock)
                await pilot.pause()
                captures[index] = app.export_screenshot(
                    title="Neuro Code visual baseline", simplify=True
                )
            return captures


async def capture_player(theme: UiTheme, viewport: tuple[int, int]) -> dict[str, object]:
    """One real shell export plus production geometry/styles, avoiding full-screen video."""
    environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
    with patch.dict(os.environ, environment, clear=True):
        app = make_app(theme, fixture="empty-conversation")
    with patch.object(NeuroCodeApp, "_update_clock", fixed_clock):
        async with app.run_test(size=viewport) as pilot:
            await pilot.pause()
            symbol = app.query_one(EmptyStateIdentity)
            svg = app.export_screenshot(title="Neuro Code visual baseline", simplify=True)
            styles = symbol._resolve_frame_styles()
            palette = []
            for item in styles:
                style = symbol.visual_style.rich_style + item
                color = (
                    SVG_EXPORT_THEME.foreground_color
                    if style.color is None or style.color.is_default
                    else style.color.get_truecolor(SVG_EXPORT_THEME)
                )
                background = (
                    SVG_EXPORT_THEME.background_color
                    if style.bgcolor is None or style.bgcolor.is_default
                    else style.bgcolor.get_truecolor(SVG_EXPORT_THEME)
                )
                # Exactly Rich's export_svg convention, not a guessed ANSI palette.
                palette.append(blend_rgb(color, background, 0.4).hex if style.dim else color.hex)
            namespace = "http://www.w3.org/2000/svg"
            ET.register_namespace("", namespace)
            root = ET.fromstring(svg)
            glyphs = [
                (parent, child)
                for parent in root.iter()
                for child in parent
                if child.tag == f"{{{namespace}}}text"
                and any(0x2800 <= ord(char) <= 0x28FF for char in child.text or "")
            ]
            first = glyphs[0][1]
            origin = {"x": float(first.attrib["x"]), "y": float(first.attrib["y"])}
            origin["dx"] = float(first.attrib["textLength"]) / len(first.text or "")
            origin["dy"] = float(glyphs[1][1].attrib["y"]) - origin["y"]
            parent = glyphs[0][0]
            parent.set("id", "terminal-grid")
            for parent, child in glyphs:
                parent.remove(child)
            # Rich textLength causes Braille shaping errors in some browser fonts.
            for node in root.iter():
                node.attrib.pop("textLength", None)
            return {
                "shell": ET.tostring(root, encoding="unicode"),
                "size": symbol.asset_size,
                "origin": origin,
                "palette": palette,
            }
