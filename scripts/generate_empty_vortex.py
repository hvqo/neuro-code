"""Offline motion sampled from canonical dots, inspired by the user's neuro_vortex.py.

Keep the accepted resting artwork, but use its particles in counter-moving radial
layers, tilted ribbon projection, short trails and a single outward impulse.
No terminal I/O, reference-file execution, PNG, random state or runtime math.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from neuro_code.interfaces.tui.empty_state_logo import LOGO_ROWS, SOURCE_SHA256

FPS = 24
DURATION_MS = 6000
STEPS = FPS * DURATION_MS // 1000
BITS = ((1, 8), (2, 16), (4, 32), (64, 128))
LEVELS = "0123456789abcdef"
Particle = tuple[float, float, float, float, int]


def smooth(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def smoother(value: float) -> float:
    return value**3 * (value * (value * 6 - 15) + 10)


def particles(rows: tuple[str, ...]) -> tuple[Particle, ...]:
    width, height = len(rows[0]) * 2, len(rows) * 4
    scale = min(width, height) * 0.40
    points: list[Particle] = []
    for row, line in enumerate(rows):
        for col, char in enumerate(line):
            bits = ord(char) - 0x2800 if char != " " else 0
            for sy, masks in enumerate(BITS):
                for sx, mask in enumerate(masks):
                    if bits & mask:
                        x = (col * 2 + sx - (width - 1) / 2) / scale
                        y = (row * 4 + sy - (height - 1) / 2) / scale
                        radius = math.hypot(x, y)
                        layer = 0 if radius > 0.83 else (1 if radius > 0.65 else 2)
                        points.append((x, y, radius, math.atan2(y, x), layer))
    return tuple(points)


def project(point: Particle, progress: float) -> tuple[float, float, float]:
    x, y, radius, angle, layer = point
    if progress <= 0 or progress >= 1:
        return x, y, 0.0
    envelope = math.sin(math.pi * progress) ** 2
    phase = smoother(progress)
    angle += math.tau * (0.85, -0.65, 1.10)[layer] * phase * envelope
    radius *= 1 + 0.065 * envelope * math.sin(3 * point[3] - math.tau * phase)
    x, y = radius * math.cos(angle), radius * math.sin(angle)
    z = 0.30 * envelope * math.sin(3 * angle + layer * 1.4 + phase * math.tau)
    tilt_x = 1.10 * envelope
    tilt_y = 0.48 * envelope * math.sin(math.tau * progress)
    y, z = y * math.cos(tilt_x) - z * math.sin(tilt_x), y * math.sin(tilt_x) + z * math.cos(tilt_x)
    x, z = x * math.cos(tilt_y) + z * math.sin(tilt_y), -x * math.sin(tilt_y) + z * math.cos(tilt_y)
    perspective = 2.8 / (2.8 - z)
    return x * perspective, y * perspective, z


def sample(rows: tuple[str, ...], progress: float) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Fixed-size cells and abstract neutral intensity, never source RGB colors."""
    width, height = len(rows[0]), len(rows)
    if progress <= 0 or progress >= 1:
        return rows, ("0" * width,) * height
    pw, ph = width * 2, height * 4
    scale = min(pw, ph) * 0.40
    envelope = math.sin(math.pi * progress) ** 2
    phase = smoother(progress)
    dots: dict[tuple[int, int], float] = {}

    def paint(x: float, y: float, strength: float) -> None:
        dx, dy = round((pw - 1) / 2 + x * scale), round((ph - 1) / 2 + y * scale)
        if 0 <= dx < pw and 0 <= dy < ph:
            dots[dx, dy] = max(dots.get((dx, dy), 0), strength)

    for point in particles(rows):
        x, y, depth = project(point, progress)
        sweep = math.exp(
            -(
                (
                    ((math.atan2(y, x) - phase * math.tau * 2 + 1.2 + math.pi) % math.tau - math.pi)
                    / 0.35
                )
                ** 2
            )
        )
        strength = min(1.0, envelope * (0.65 + 0.20 * depth + 0.25 * sweep))
        if envelope > 0.03:
            for lag, weight in ((0.020, 0.30), (0.040, 0.13)):
                tx, ty, _ = project(point, max(0, progress - lag))
                paint(tx, ty, strength * weight * envelope)
        paint(x, y, strength)
    impulse = math.exp(-(((progress - 0.77) / 0.065) ** 2)) * envelope
    if impulse > 0.02:
        radius = 0.94 + 0.24 * smooth((progress - 0.65) / 0.24)
        for index in range(128):
            angle = index * math.tau / 128
            paint(radius * math.cos(angle), radius * math.sin(angle), impulse * 0.60)
    geometry, intensities = [], []
    data_gate = smooth((progress - 0.20) / 0.12) * (1 - smooth((progress - 0.67) / 0.13))
    for row in range(height):
        chars, levels = [], []
        for col in range(width):
            bits, strengths = 0, []
            for sy, masks in enumerate(BITS):
                for sx, mask in enumerate(masks):
                    value = dots.get((col * 2 + sx, row * 4 + sy))
                    if value is not None:
                        bits |= mask
                        strengths.append(value)
            level = round(max(strengths, default=0) * 15)
            char = chr(0x2800 + bits) if bits else " "
            # Sparse deterministic data glints from the reference, not random glitch.
            if bits and level >= 9 and data_gate > 0.35:
                marker = (col * 17 + row * 31 + int(progress * 28)) % 61
                if marker < 2:
                    char = ("0", "1")[marker]
            chars.append(char)
            levels.append(LEVELS[level])
        geometry.append("".join(chars))
        intensities.append("".join(levels))
    return tuple(geometry), tuple(intensities)


def generate() -> dict[str, object]:
    durations = [
        ((i + 1) * DURATION_MS // STEPS) - i * DURATION_MS // STEPS for i in range(STEPS)
    ] + [0]
    return {
        "source_sha256": SOURCE_SHA256,
        "fps": FPS,
        "duration_ms": DURATION_MS,
        "durations": durations,
        "sizes": {
            size: [sample(rows, index / STEPS) for index in range(STEPS + 1)]
            for size, rows in LOGO_ROWS.items()
        },
    }


if __name__ == "__main__":
    output = (
        Path(__file__).resolve().parents[1]
        / "src/neuro_code/interfaces/tui/empty_state_vortex.json"
    )
    output.write_text(
        json.dumps(generate(), ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(output)
