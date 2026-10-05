"""Measured Ink presentation data; Unicode source offsets are never cells.

One bounded timeline per streamed view. No Runtime events, timers or text queues
are owned here. Grapheme boundaries come from regex's UAX #29 implementation.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from math import ceil, floor

import regex
from markdown_it.token import Token
from rich.color import Color, ColorSystem, ColorType
from rich.style import Style
from rich.text import Text

FRAME_RATE = 24
FRAME_SECONDS = 1 / FRAME_RATE
DURATION_SECONDS = 0.160
DELTA_LSTAR = 22.0
MIN_CONTRAST_RATIO = 4.5
MAX_ACTIVE_GLYPHS = 8
SOURCE_TAG_LIMIT = 12
SOURCE_WINDOW = 384
ARRIVAL_META = "neuro_arrival_source"
RGB = tuple[int, int, int]
_CLUSTER = regex.compile(r"\X")
_SAFE = regex.compile(r"(?:[\p{Latin}\p{Han}\p{Common}]\p{M}*)\Z")
_COMPLEX = regex.compile(r"[\p{Extended_Pictographic}\p{Regional_Indicator}\p{Cf}\p{Cc}]")


def graphemes(text: str) -> Iterator[tuple[int, int, str]]:
    for match in _CLUSTER.finditer(text):
        yield match.start(), match.end(), match.group()


def safe_glyph(glyph: str) -> bool:
    """Only unambiguous Latin/CJK cells; shaping/emoji clusters stay canonical."""
    return bool(_SAFE.fullmatch(glyph)) and not bool(_COMPLEX.search(glyph))


def paragraph_sources(markup: str, parsed: list[Token]) -> dict[int, tuple[int, str]]:
    """Prove literal top-level paragraph provenance, otherwise fail closed.

    No substring searching or guessed inline-token locations. Source lines must
    exactly equal the parser's inline content and its text/softbreak children.
    Formatting, entities, escapes, HTML, links and nested blocks remain static.
    """
    offsets = [0]
    for line in markup.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    sources: dict[int, tuple[int, str]] = {}
    for opening, inline in pairwise(parsed):
        if opening.type != "paragraph_open" or opening.level != 0 or inline.type != "inline":
            continue
        if not inline.map or not inline.children:
            continue
        if any(child.type not in {"text", "softbreak"} for child in inline.children):
            continue
        literal = "".join("\n" if t.type == "softbreak" else t.content for t in inline.children)
        first, last = inline.map
        if last >= len(offsets):
            continue
        start, end = offsets[first], offsets[last]
        if markup[start:end].removesuffix("\n") != inline.content or literal != inline.content:
            continue
        sources[id(opening)] = (start, literal.replace("\n", " "))
    return sources


def tag_paragraph(
    text: Text,
    source: tuple[int, str] | None,
    floor: int,
    preserve_starts: frozenset[int] = frozenset(),
) -> None:
    if source is None or text.plain != source[1]:
        return
    offset = source[0]
    tail: deque[tuple[int, int]] = deque(maxlen=SOURCE_TAG_LIMIT)
    preserved: list[tuple[int, int]] = []
    for start, end, glyph in graphemes(text.plain):
        source_start = offset + start
        if (source_start >= floor or source_start in preserve_starts) and (
            not glyph.isspace() and safe_glyph(glyph)
        ):
            if source_start in preserve_starts:
                preserved.append((start, end))
            else:
                tail.append((start, end))
    for start, end in {*tail, *preserved}:
        text.stylize(Style(meta={ARRIVAL_META: (offset + start, offset + end)}), start, end)


@dataclass(frozen=True, slots=True)
class Arrival:
    start: int
    end: int
    received_at: float


class ArrivalTimeline:
    def __init__(self) -> None:
        self.arrivals: deque[Arrival] = deque(maxlen=256)
        # First presentation, not delta receipt. Keep settled identities within
        # the source window: rebuild/reflow/combining marks cannot restart them.
        self.births: dict[int, float] = {}
        # Visible arrivals denied a slot or a reliable color plan stay static;
        # they are never promoted later after another glyph settles.
        self.skipped: set[int] = set()
        self._floor = 0

    def receive(self, start: int, end: int, now: float) -> None:
        if end > start:
            self.arrivals.append(Arrival(start, end, now))
            self._floor = max(self._floor, end - SOURCE_WINDOW)
        self.prune(now)

    def prune(self, now: float) -> None:
        # Pending ranges have no visual age. Bound by source progress/count,
        # never by receive-time TTL, even if parsing takes longer than motion.
        while self.arrivals and self.arrivals[0].end <= self._floor:
            self.arrivals.popleft()
        self.births = {
            s: at
            for s, at in self.births.items()
            if s >= self._floor or 0 <= now - at < DURATION_SECONDS
        }
        self.skipped = {s for s in self.skipped if s >= self._floor}

    def registered(self, source: tuple[int, int]) -> bool:
        start, end = source
        if end <= start or start in self.skipped:
            return False
        return start in self.births or (
            start >= self._floor and any(start < a.end and end > a.start for a in self.arrivals)
        )

    def eligible(self, source: tuple[int, int], now: float) -> bool:
        return self.registered(source) and (
            source[0] not in self.births or self.age(source, now) is not None
        )

    def start_visual(self, source: tuple[int, int], now: float) -> float | None:
        """Called only for a proven, uncropped glyph in the visible strip crop."""
        if not self.eligible(source, now):
            return None
        if source[0] not in self.births and self.active_count(now) >= MAX_ACTIVE_GLYPHS:
            self.skip_visual(source)
            return None
        self.births.setdefault(source[0], now)
        return self.age(source, now)

    def skip_visual(self, source: tuple[int, int]) -> None:
        if source[0] not in self.births and source[0] >= self._floor:
            self.skipped.add(source[0])

    def age(self, source: tuple[int, int], now: float) -> float | None:
        at = self.births.get(source[0])
        if at is None:
            return None
        age = now - at
        return age if 0 <= age < DURATION_SECONDS else None

    def has_active(self, now: float) -> bool:
        return self.active_count(now) > 0

    def active_count(self, now: float) -> int:
        return sum(0 <= now - at < DURATION_SECONDS for at in self.births.values())

    def active_starts(self, now: float) -> frozenset[int]:
        return frozenset(
            start for start, at in self.births.items() if 0 <= now - at < DURATION_SECONDS
        )

    def clear(self) -> None:
        self.arrivals.clear()
        self.births.clear()
        self.skipped.clear()
        self._floor = 0


@dataclass(frozen=True, slots=True)
class MaterializePlan:
    """A bounded, capability-aware foreground path for one canonical color."""

    frames: tuple[Color | None, ...]
    delta_lstar: float
    duration: float
    frame_rate: int

    def color_at(self, age: float) -> Color | None:
        if age < 0 or age >= self.duration or not self.frames:
            return None
        frame = min(floor(age * self.frame_rate), len(self.frames) - 1)
        return self.frames[frame]


def color_system_name(color_system: object) -> str | None:
    """Return only capabilities whose output semantics are known here."""
    if color_system == "truecolor" or color_system is ColorSystem.TRUECOLOR:
        return "truecolor"
    if color_system in {"256", "eight_bit"} or color_system is ColorSystem.EIGHT_BIT:
        return "256"
    return None


def concrete_rgb(color: Color | None) -> RGB | None:
    """Resolve concrete RGB only; terminal-default/ANSI16 colors fail closed."""
    if color is None or color.type not in {ColorType.TRUECOLOR, ColorType.EIGHT_BIT}:
        return None
    triplet = color.get_truecolor()
    return triplet.red, triplet.green, triplet.blue


def quantized_rgb(rgb: RGB, color_system: object) -> RGB | None:
    mode = color_system_name(color_system)
    if mode == "truecolor":
        return rgb
    if mode == "256":
        color = Color.from_rgb(*rgb).downgrade(ColorSystem.EIGHT_BIT)
        return concrete_rgb(color)
    return None


def _linear(channel: int) -> float:
    value = channel / 255
    return ((value + 0.055) / 1.055) ** 2.4 if value > 0.04045 else value / 12.92


def _lab_curve(value: float) -> float:
    delta = 6 / 29
    return value ** (1 / 3) if value > delta**3 else value / (3 * delta**2) + 4 / 29


def _lab_inverse(value: float) -> float:
    delta = 6 / 29
    return value**3 if value > delta else 3 * delta**2 * (value - 4 / 29)


def _rgb_to_lab(rgb: RGB) -> tuple[float, float, float]:
    red, green, blue = (_linear(channel) for channel in rgb)
    x = (0.4124564 * red + 0.3575761 * green + 0.1804375 * blue) / 0.95047
    y = 0.2126729 * red + 0.7151522 * green + 0.0721750 * blue
    z = (0.0193339 * red + 0.1191920 * green + 0.9503041 * blue) / 1.08883
    fx, fy, fz = _lab_curve(x), _lab_curve(y), _lab_curve(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _lab_to_rgb(lightness: float, a: float, b: float) -> RGB:
    fy = (lightness + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    x = 0.95047 * _lab_inverse(fx)
    y = _lab_inverse(fy)
    z = 1.08883 * _lab_inverse(fz)
    linear_rgb = (
        3.2404542 * x - 1.5371385 * y - 0.4985314 * z,
        -0.9692660 * x + 1.8760108 * y + 0.0415560 * z,
        0.0556434 * x - 0.2040259 * y + 1.0572252 * z,
    )

    def encode(channel: float) -> int:
        value = 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055
        return round(max(0.0, min(1.0, value)) * 255)

    return tuple(encode(channel) for channel in linear_rgb)  # type: ignore[return-value]


def _luminance(rgb: RGB) -> float:
    red, green, blue = (_linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: RGB, background: RGB) -> float:
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _display_color(color: Color, mode: str) -> Color:
    return color.downgrade(ColorSystem.EIGHT_BIT) if mode == "256" else color


def _same_output(first: Color, second: Color, mode: str) -> bool:
    if mode == "256":
        return (
            first.downgrade(ColorSystem.EIGHT_BIT).number
            == second.downgrade(ColorSystem.EIGHT_BIT).number
        )
    return concrete_rgb(first) == concrete_rgb(second)


def _rgb_path(
    background: RGB,
    canonical_display: Color,
    mode: str,
    delta: float,
    duration: float,
    frame_rate: int,
    minimum_contrast: float,
) -> tuple[Color, ...] | None:
    canonical_rgb = concrete_rgb(canonical_display)
    if canonical_rgb is None or contrast_ratio(canonical_rgb, background) < minimum_contrast:
        return None

    # The path starts from the color users actually see. This matters when a
    # TrueColor source style is rendered through an ANSI256 console.
    foreground_l, a, b = _rgb_to_lab(canonical_rgb)
    background_l, _, _ = _rgb_to_lab(background)
    direction = 1.0 if background_l > foreground_l else -1.0
    count = max(1, ceil(duration * frame_rate))
    frames: list[Color] = []
    previous_distance = float("inf")
    visible_colors: set[int] = set()
    canonical_l = _rgb_to_lab(canonical_rgb)[0]
    for frame in range(count):
        age = frame / frame_rate
        remaining = max(0.0, 1.0 - age / duration)
        candidate_rgb = _lab_to_rgb(foreground_l + direction * delta * remaining, a, b)
        candidate = _display_color(Color.from_rgb(*candidate_rgb), mode)
        displayed_rgb = concrete_rgb(candidate)
        if displayed_rgb is None or contrast_ratio(displayed_rgb, background) < minimum_contrast:
            return None
        distance = abs(_rgb_to_lab(displayed_rgb)[0] - canonical_l)
        if distance > previous_distance + 1e-6 or distance > DELTA_LSTAR + 1e-6:
            return None
        previous_distance = distance
        frames.append(candidate)
        if not _same_output(candidate, canonical_display, mode):
            if mode == "256":
                number = candidate.downgrade(ColorSystem.EIGHT_BIT).number
                if number is not None:
                    visible_colors.add(number)
        elif visible_colors:
            # A quantized path may settle early, but it may not disappear and
            # then return to a changed color.
            return None

    if _same_output(frames[0], canonical_display, mode):
        return None
    if mode == "256" and len(visible_colors) < 2:
        return None
    return tuple(frames)


@lru_cache(maxsize=128)
def _cached_materialize_plan(
    foreground_color: Color,
    background: RGB,
    mode: str,
    duration: float,
    frame_rate: int,
    requested_delta: float,
    minimum_contrast: float,
) -> MaterializePlan | None:
    foreground = concrete_rgb(foreground_color)
    if foreground is None:
        return None
    canonical_display = _display_color(foreground_color, mode)
    canonical_rgb = concrete_rgb(canonical_display)
    if canonical_rgb is None:
        return None
    canonical_l = _rgb_to_lab(canonical_rgb)[0]

    # Preserve the requested A22 path whenever it satisfies contrast and
    # quantized stability; otherwise find the largest safe lower L* movement.
    for tenths in range(round(requested_delta * 10), 0, -1):
        delta = tenths / 10
        frames = _rgb_path(
            background,
            canonical_display,
            mode,
            delta,
            duration,
            frame_rate,
            minimum_contrast,
        )
        if frames is None:
            continue
        first_rgb = concrete_rgb(frames[0])
        if first_rgb is None:
            continue
        actual_delta = abs(_rgb_to_lab(first_rgb)[0] - canonical_l)
        if actual_delta <= requested_delta + 1e-6:
            return MaterializePlan(frames, actual_delta, duration, frame_rate)
    return None


def materialize_plan(
    foreground: Color | None,
    background: RGB | None,
    color_system: object,
) -> MaterializePlan | None:
    """Build a cached plan from concrete rendered colors and known capability."""
    mode = color_system_name(color_system)
    if foreground is None or background is None or mode is None:
        return None
    if foreground.type not in {ColorType.TRUECOLOR, ColorType.EIGHT_BIT}:
        return None
    return _cached_materialize_plan(
        foreground,
        background,
        mode,
        DURATION_SECONDS,
        FRAME_RATE,
        DELTA_LSTAR,
        MIN_CONTRAST_RATIO,
    )


def ink_style(style: Style, age: float, plan: MaterializePlan) -> Style:
    """Change only foreground while keeping all Rich style semantics intact."""
    if style.bold or style.italic or style.underline or style.link:
        return style
    color = plan.color_at(age)
    return style + Style(color=color) if color is not None else style
