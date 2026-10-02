"""Local HTML playback from actual production Textual frames, or native TUI.

uv run python -m tests.visual.empty_reveal.render_preview --output /path/preview.html
uv run python -m tests.visual.empty_reveal.render_preview --terminal --theme system
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
from pathlib import Path
from tempfile import gettempdir

from tests.visual.empty_reveal.preview import KEY_FRAMES, capture_frames, capture_player
from tests.visual.showcases import make_app

from neuro_code.interfaces.tui.empty_state_reveal import VORTEX_FRAMES
from neuro_code.interfaces.tui.terminal_palette import probe_terminal_palette
from neuro_code.shared.ui_theme import UiTheme


async def render(output: Path) -> None:
    captures: dict[str, object] = {}
    shells: dict[str, object] = {}
    for theme in (UiTheme.SYSTEM, UiTheme.GRAPHITE, UiTheme.PORCELAIN):
        for viewport in ((120, 40), (100, 32), (80, 24)):
            key = f"{theme.value}-{viewport[0]}x{viewport[1]}"
            shells[key] = await capture_player(theme, viewport)
            captures[key] = {
                index: "data:image/svg+xml;base64,"
                + base64.b64encode(re.sub(rb' textLength="[0-9.]+"', b"", svg.encode())).decode()
                for index, svg in (await capture_frames(theme, viewport)).items()
            }
    data = json.dumps(
        {
            "captures": captures,
            "shells": shells,
            "frames": [
                {size: [f.rows[size], f.levels[size]] for size in f.rows} for f in VORTEX_FRAMES
            ],
            "durations": [f.duration_ms for f in VORTEX_FRAMES],
            "keyframes": KEY_FRAMES,
        },
        ensure_ascii=False,
    )
    template = Path(__file__).with_name("player.html").read_text(encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        output.write_text, template.replace("__FRAME_DATA__", data), encoding="utf-8"
    )
    print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(gettempdir()) / "neuro-empty-reveal-preview.html"
    )
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("--theme", choices=["system", "graphite", "porcelain"], default="system")
    args = parser.parse_args()
    if args.terminal:
        make_app(
            UiTheme(args.theme),
            fixture="empty-conversation",
            terminal_palette=probe_terminal_palette(),
        ).run()
    else:
        asyncio.run(render(args.output))


if __name__ == "__main__":
    main()
