"""Bounded terminal palette probing and deterministic System palette resolution.

The probe is presentation-only and best-effort. It runs before Textual starts
reading terminal input, has a strict deadline, and treats unsupported replies as
an ordinary unknown-palette result.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TextIO

from rich.console import Console

RGB = tuple[int, int, int]
_MAX_PROBE_BYTES = 512
_DEFAULT_PROBE_TIMEOUT_SECONDS = 0.06
_OSC_COLOR = re.compile(
    rb"\x1b\](10|11);rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})(?:\x07|\x1b\\)"
)


class TerminalColorLevel(StrEnum):
    """Color output capabilities relevant to derived System surfaces."""

    TRUECOLOR = "truecolor"
    ANSI256 = "ansi256"
    ANSI16 = "ansi16"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TerminalPalette:
    """Terminal defaults and color capability; missing RGB means unknown."""

    color_level: TerminalColorLevel = TerminalColorLevel.UNKNOWN
    foreground: RGB | None = None
    background: RGB | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSystemPalette:
    """Semantic colors consumed by Textual and Rich for the System theme."""

    mode: str
    adaptive: bool
    canvas: str
    surface: str
    surface_subtle: str
    surface_selected: str
    composer_surface: str
    user_message_surface: str
    border_subtle: str
    border_normal: str
    border_focus: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent: str


def resolve_system_palette(palette: TerminalPalette) -> ResolvedSystemPalette:
    """Resolve stable RGB surfaces when available, otherwise use terminal roles.

    ANSI16 and unknown terminals keep their own foreground/background semantics;
    the visible ANSI-white rule and ANSI-blue focus remain reliable without
    guessing what a user's bright-black slot looks like.
    """

    foreground, background = palette.foreground, palette.background
    adaptive = (
        palette.color_level in (TerminalColorLevel.TRUECOLOR, TerminalColorLevel.ANSI256)
        and foreground is not None
        and background is not None
        and _contrast(foreground, background) >= 4.5
    )
    if not adaptive:
        return ResolvedSystemPalette(
            mode="unknown",
            adaptive=False,
            canvas="ansi_default",
            surface="ansi_default",
            surface_subtle="ansi_default",
            surface_selected="ansi_default",
            composer_surface="ansi_default",
            user_message_surface="ansi_default",
            border_subtle="ansi_white",
            border_normal="ansi_white",
            border_focus="ansi_bright_blue",
            text_primary="ansi_default",
            text_secondary="ansi_default",
            text_muted="ansi_default",
            accent="ansi_bright_blue",
        )

    assert foreground is not None
    assert background is not None
    light = _luminance(background) >= 0.48
    mode = "light" if light else "dark"
    direction = (0, 0, 0) if light else (255, 255, 255)
    canvas = _hex(background)
    surface = _hex(_mix(background, direction, 0.04))
    surface_subtle = _hex(_mix(background, direction, 0.065))
    surface_selected = _hex(_mix(background, direction, 0.16))
    composer_surface = _hex(_mix(background, direction, 0.09))
    user_message_surface = _hex(_mix(background, direction, 0.052))

    border_subtle = _hex(_mix(background, foreground, 0.28))
    border_normal = _hex(_mix(background, foreground, 0.44))
    text_surfaces = tuple(
        _parse_hex(value)
        for value in (
            canvas,
            surface,
            surface_subtle,
            surface_selected,
            composer_surface,
            user_message_surface,
        )
    )
    text_secondary = _hex(_readable_foreground(foreground, background, text_surfaces, 0.80, 6.0))
    text_muted = _hex(_readable_foreground(foreground, background, text_surfaces, 0.62, 4.5))
    accent_rgb = (0, 95, 135) if light else (105, 174, 229)
    return ResolvedSystemPalette(
        mode=mode,
        adaptive=True,
        canvas=canvas,
        surface=surface,
        surface_subtle=surface_subtle,
        surface_selected=surface_selected,
        composer_surface=composer_surface,
        user_message_surface=user_message_surface,
        border_subtle=border_subtle,
        border_normal=border_normal,
        border_focus=_hex(accent_rgb),
        text_primary=_hex(foreground),
        text_secondary=text_secondary,
        text_muted=text_muted,
        accent=_hex(accent_rgb),
    )


def probe_terminal_palette(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> TerminalPalette:
    """Probe the current terminal before Textual owns its input stream.

    A missing TTY, unsupported OSC query, malformed response, or I/O failure
    returns a capability-only/unknown palette and never blocks beyond the
    fixed deadline.
    """

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    color_level = _color_level(output_stream)
    if (
        os.name != "posix"
        or not input_stream.isatty()
        or not output_stream.isatty()
        or color_level not in (TerminalColorLevel.TRUECOLOR, TerminalColorLevel.ANSI256)
        or os.environ.get("NO_COLOR") is not None
    ):
        return TerminalPalette(color_level=color_level)

    try:
        input_fd = input_stream.fileno()
        output_fd = output_stream.fileno()
        if os.ttyname(input_fd) != os.ttyname(output_fd):
            return TerminalPalette(color_level=color_level)
        foreground, background = _probe_default_colors(
            input_fd, output_fd, timeout_seconds=max(0.0, min(timeout_seconds, 0.1))
        )
    except (OSError, ValueError, TypeError):
        foreground, background = None, None
    return TerminalPalette(color_level, foreground, background)


def parse_osc_default_colors(data: bytes) -> tuple[RGB, RGB] | None:
    """Parse one OSC 10 foreground and OSC 11 background reply pair."""

    colors: dict[int, RGB] = {}
    for match in _OSC_COLOR.finditer(data[:_MAX_PROBE_BYTES]):
        slot = int(match.group(1))
        channels = tuple(_scale_channel(match.group(index)) for index in (2, 3, 4))
        colors[slot] = (channels[0], channels[1], channels[2])
    if 10 not in colors or 11 not in colors:
        return None
    return colors[10], colors[11]


def _probe_default_colors(
    input_fd: int, output_fd: int, *, timeout_seconds: float
) -> tuple[RGB, RGB] | tuple[None, None]:
    """Read the two OSC replies in a bounded pre-driver input window."""

    import termios
    import tty

    # Do not take ownership of an input buffer that already contains user keys.
    if select.select([input_fd], [], [], 0)[0]:
        return None, None
    original = termios.tcgetattr(input_fd)
    buffer = bytearray()
    try:
        tty.setraw(input_fd, termios.TCSANOW)
        os.write(output_fd, b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\")
        deadline = time.monotonic() + timeout_seconds
        while len(buffer) < _MAX_PROBE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([input_fd], [], [], remaining)
            if not readable:
                break
            chunk = os.read(input_fd, min(64, _MAX_PROBE_BYTES - len(buffer)))
            if not chunk:
                break
            buffer.extend(chunk)
            colors = parse_osc_default_colors(bytes(buffer))
            if colors is not None:
                return colors
        return None, None
    finally:
        termios.tcsetattr(input_fd, termios.TCSANOW, original)


def _color_level(stream: TextIO) -> TerminalColorLevel:
    if os.environ.get("NO_COLOR") is not None:
        return TerminalColorLevel.UNKNOWN
    try:
        system = Console(file=stream).color_system
    except (OSError, ValueError):
        return TerminalColorLevel.UNKNOWN
    if system == "truecolor":
        return TerminalColorLevel.TRUECOLOR
    if system == "256":
        return TerminalColorLevel.ANSI256
    if system in ("standard", "windows"):
        return TerminalColorLevel.ANSI16
    return TerminalColorLevel.UNKNOWN


def _scale_channel(value: bytes) -> int:
    integer = int(value, 16)
    maximum = (1 << (4 * len(value))) - 1
    return round(integer * 255 / maximum)


def _mix(start: RGB, end: RGB, end_weight: float) -> RGB:
    return tuple(
        round(start[index] * (1.0 - end_weight) + end[index] * end_weight) for index in range(3)
    )  # type: ignore[return-value]


def _hex(rgb: RGB) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in rgb)


def _parse_hex(value: str) -> RGB:
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


def _readable_foreground(
    foreground: RGB,
    background: RGB,
    surfaces: tuple[RGB, ...],
    initial_weight: float,
    minimum_contrast: float,
) -> RGB:
    weight = initial_weight
    while weight < 1.0:
        candidate = _mix(background, foreground, weight)
        if min(_contrast(candidate, surface) for surface in surfaces) >= minimum_contrast:
            return candidate
        weight = min(1.0, weight + 0.025)
    return foreground


def _luminance(rgb: RGB) -> float:
    channels = (
        channel / 255 / 12.92
        if channel / 255 <= 0.04045
        else ((channel / 255 + 0.055) / 1.055) ** 2.4
        for channel in rgb
    )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: RGB, second: RGB) -> float:
    first_luminance, second_luminance = _luminance(first), _luminance(second)
    lighter, darker = max(first_luminance, second_luminance), min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


__all__ = [
    "RGB",
    "ResolvedSystemPalette",
    "TerminalColorLevel",
    "TerminalPalette",
    "parse_osc_default_colors",
    "probe_terminal_palette",
    "resolve_system_palette",
]
