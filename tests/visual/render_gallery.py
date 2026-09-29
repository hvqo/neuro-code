from __future__ import annotations

import argparse
import base64
import html
import subprocess
from pathlib import Path
from tempfile import gettempdir

SNAPSHOT_ROOT = Path(__file__).parent / "snapshots"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _embedded_svg(content: bytes, label: str) -> str:
    encoded = base64.b64encode(content).decode("ascii")
    return f'<img alt="{label}" src="data:image/svg+xml;base64,{encoded}">'


def _snapshot_at_ref(ref: str, path: Path) -> bytes | None:
    relative = path.relative_to(REPOSITORY_ROOT).as_posix()
    try:
        return subprocess.run(
            ["git", "show", f"{ref}:{relative}"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        if (
            b"does not exist" in error.stderr
            or b"Path '" in error.stderr
            or b"but not in" in error.stderr
        ):
            return None
        raise SystemExit(
            f"Cannot read {relative} at {ref}: {error.stderr.decode(errors='replace')}"
        ) from error


def render_gallery(output: Path, *, before_ref: str | None = None) -> Path:
    snapshots = sorted(SNAPSHOT_ROOT.glob("*.svg"))
    if not snapshots:
        raise SystemExit("No SVG baselines found; run the visual snapshot tests first.")

    cards: list[str] = []
    for path in snapshots:
        label = html.escape(path.stem.replace("__", " · "))
        current = _embedded_svg(path.read_bytes(), label)
        if before_ref is None:
            cards.append(f"<figure><figcaption>{label}</figcaption>{current}</figure>")
        else:
            before_svg = _snapshot_at_ref(before_ref, path)
            before = (
                _embedded_svg(before_svg, f"{label} before")
                if before_svg is not None
                else "<span class=missing>Not present in the V0 baseline</span>"
            )
            cards.append(
                f"<figure><figcaption>{label}</figcaption><div class=pair>"
                f"<div><strong>Before ({html.escape(before_ref)})</strong>{before}</div>"
                f"<div><strong>V1A</strong>{current}</div></div></figure>"
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
.missing { display: block; padding: 1rem; color: #aaa; border: 1px dashed #555; }
.pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .5rem; }
.pair strong { display: block; margin-bottom: .4rem; font: 12px ui-monospace, monospace; }
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
    parser.add_argument(
        "--before-ref",
        help="Show each committed snapshot at this Git ref beside the current snapshot.",
    )
    args = parser.parse_args()
    print(render_gallery(args.output.resolve(), before_ref=args.before_ref))


if __name__ == "__main__":
    main()
