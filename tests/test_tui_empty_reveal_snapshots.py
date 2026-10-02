"""Six production motion states in every accepted theme and viewport."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import gettempdir

import pytest

from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui_visual_snapshots import SNAPSHOT_ROOT, canonicalize_svg
from tests.visual.empty_reveal.preview import KEY_FRAMES, capture_frames


@pytest.mark.parametrize("theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
@pytest.mark.parametrize("viewport", [(120, 40), (100, 32), (80, 24)])
async def test_production_reveal_key_frame_snapshots(
    theme: UiTheme, viewport: tuple[int, int]
) -> None:
    frames = await capture_frames(theme, viewport)
    assert frames[0] == frames[-1]
    # Match the intentionally quieter canonical resting baseline, rather than
    # merely comparing two new frames. It also guards inherited NO_COLOR filters.
    static_baseline = SNAPSHOT_ROOT / (
        f"empty-conversation__{theme.value}__{viewport[0]}x{viewport[1]}.svg"
    )
    assert canonicalize_svg(frames[0]) == static_baseline.read_text(encoding="utf-8")
    for state, index in KEY_FRAMES.items():
        actual = canonicalize_svg(frames[index])
        name = f"empty-reveal-{state}__{theme.value}__{viewport[0]}x{viewport[1]}.svg"
        path = SNAPSHOT_ROOT / name
        if os.environ.get("NEURO_TUI_UPDATE_SNAPSHOTS") == "1":
            path.write_text(actual, encoding="utf-8", newline="\n")
            continue
        received = Path(gettempdir()) / "neuro-code-tui-visual-received" / name
        received.parent.mkdir(parents=True, exist_ok=True)
        received.write_text(actual, encoding="utf-8", newline="\n")
        assert path.exists(), f"Missing explicit baseline: {path}"
        assert path.read_text(encoding="utf-8") == actual, (
            f"Inspect {received} before updating {path}"
        )
