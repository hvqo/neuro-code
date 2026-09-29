from __future__ import annotations

import argparse
import base64
import html
from pathlib import Path
from tempfile import gettempdir

SNAPSHOT_ROOT = Path(__file__).parent / "snapshots"


def render_gallery(output: Path) -> Path:
    snapshots = sorted(SNAPSHOT_ROOT.glob("*.svg"))
    if not snapshots:
        raise SystemExit("No SVG baselines found; run the visual snapshot tests first.")

    cards: list[str] = []
    for path in snapshots:
        label = html.escape(path.stem.replace("__", " · "))
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        cards.append(
            f"<figure><figcaption>{label}</figcaption>"
            f'<img alt="{label}" src="data:image/svg+xml;base64,{encoded}"></figure>'
        )

    document = (
        """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neuro Code TUI visual baselines</title>
<style>
body { margin: 1.5rem; color: #eee; background: #181818; font: 14px system-ui, sans-serif; }
h1 { font-size: 1.2rem; font-weight: 600; }
main { display: grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap: 1rem; }
figure { margin: 0; padding: .6rem; background: #242424; border: 1px solid #555; }
figcaption { padding: 0 0 .5rem; color: #ddd; font: 12px ui-monospace, monospace; }
img { display: block; width: 100%; height: auto; }
</style>
<h1>Neuro Code TUI visual baselines</h1>
<main>
"""
        + "\n".join(cards)
        + "\n</main>\n</html>\n"
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as html_file:
        html_file.write(document)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a standalone TUI snapshot gallery.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(gettempdir()) / "neuro-code-tui-visual-gallery.html",
    )
    args = parser.parse_args()
    print(render_gallery(args.output.resolve()))


if __name__ == "__main__":
    main()
