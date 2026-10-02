"""Capture the shipped widget; clock freezing is confined to this test harness."""

from __future__ import annotations

import os
from unittest.mock import patch

from tests.visual.showcases import make_app
from textual.widgets import Static

from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.empty_state import EmptyStateIdentity
from neuro_code.interfaces.tui.empty_state_reveal import TORSION_LOCK
from neuro_code.shared.ui_theme import UiTheme

KEY_FRAMES = {
    "resting": 0,
    "pre-torsion": 2,
    "max-torsion": 5,
    "lock-peak": 9,
    "rebound": 7,
    "returned-resting": 11,
}


def fixed_clock(app: NeuroCodeApp) -> None:
    app.query_one("#clock", Static).update("13:37")


def freeze_frame(symbol: EmptyStateIdentity, index: int, clock: list[float]) -> None:
    clock[0] = 100 + (sum(f.duration_ms for f in TORSION_LOCK[:index]) + 0.01) / 1000
    symbol._advance_animation(symbol._animation_generation)
    # Production uses one-shot timers; freeze only this capture, not the app.
    if symbol._timer is not None:
        symbol._timer.stop()
        symbol._timer = None


async def capture_frames(theme: UiTheme, viewport: tuple[int, int]) -> list[str]:
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
            captures = []
            for index in range(len(TORSION_LOCK)):
                freeze_frame(symbol, index, clock)
                await pilot.pause()
                captures.append(
                    app.export_screenshot(title="Neuro Code visual baseline", simplify=True)
                )
            return captures
