"""Offline artwork conversion, not imported by production or snapshot tests.

Run with a Python that has Pillow; CI consumes the checked-in terminal rows.
The PNG is a design reference only. Thresholds isolate its gold mark and green
border, never its filled grayscale badge background.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

BRAILLE_BITS = (
    (0, 0, 0),
    (0, 1, 1),
    (0, 2, 2),
    (1, 0, 3),
    (1, 1, 4),
    (1, 2, 5),
    (0, 3, 6),
    (1, 3, 7),
)
SIZES = {"large": (32, 16), "medium": (24, 12), "small": (16, 8)}


def encode(mask: Image.Image, columns: int, lines: int, *, sparse: bool = False) -> list[str]:
    target = (columns * 2, lines * 4)
    scaled = ImageOps.contain(mask, target, Image.Resampling.LANCZOS)
    sampled = Image.new("L", target)
    sampled.paste(scaled, ((target[0] - scaled.width) // 2, (target[1] - scaled.height) // 2))
    rows = []
    for row in range(lines):
        text = ""
        for column in range(columns):
            bits = 0
            for dx, dy, bit in BRAILLE_BITS:
                x, y = column * 2 + dx, row * 4 + dy
                if sampled.getpixel((x, y)) >= 80 and (not sparse or (x + y) % 3 == 0):
                    bits |= 1 << bit
            text += chr(0x2800 + bits) if bits else " "
        rows.append(text)
    return rows


def convert(source: Path, output: Path) -> None:
    raw = source.read_bytes()
    image = Image.open(source).convert("RGBA")
    core, border = (Image.new("L", image.size) for _ in range(2))
    for y in range(image.height):
        for x in range(image.width):
            red, green, blue, alpha = image.getpixel((x, y))
            if alpha >= 128 and red >= green and red - blue >= 16 and green - blue >= 8:
                core.putpixel((x, y), 255)
            if alpha >= 128 and green - red >= 6 and green - blue >= 2:
                border.putpixel((x, y), 255)
    badge_bounds = image.getchannel("A").point(lambda alpha: 255 if alpha >= 128 else 0).getbbox()
    core_bounds = core.getbbox()
    if badge_bounds is None or core_bounds is None or border.getbbox() is None:
        raise ValueError("Reference does not contain the verified badge/core color regions")
    full_core, outline = core.crop(badge_bounds), border.crop(badge_bounds)
    # Only foreground contours survive; the original solid badge never enters a mask.
    full_badge = ImageChops.lighter(full_core, outline)
    core_only = core.crop(core_bounds)
    result = {
        "source": source.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_size": image.size,
        "badge_bounds": badge_bounds,
        "core_bounds": core_bounds,
        "conversion": "gold core / green border segmentation; grayscale badge excluded; Braille 2x4 subpixels",
        "sizes": {},
    }
    for size, (columns, lines) in SIZES.items():
        result["sizes"][size] = {
            "A": {"core": encode(full_badge, columns, lines), "outline": [" " * columns] * lines},
            "B": {"core": encode(core_only, columns, lines), "outline": [" " * columns] * lines},
            "C": {
                "core": encode(full_core, columns, lines),
                "outline": encode(outline, columns, lines, sparse=True),
            },
        }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("representations.json")
    )
    args = parser.parse_args()
    convert(args.source, args.output)
