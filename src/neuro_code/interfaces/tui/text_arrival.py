"""Measured Ink presentation data; Unicode source offsets are never cells.

One bounded timeline per streamed view. No Runtime events, timers or text queues
are owned here. Grapheme boundaries come from regex's UAX #29 implementation.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise

import regex
from markdown_it.token import Token
from rich.style import Style
from rich.text import Text

FRAME_SECONDS = 1 / 20
DURATION_SECONDS = 0.180
GLYPH_LIMIT = 12
SOURCE_WINDOW = 384
ARRIVAL_META = "neuro_arrival_source"
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


def tag_paragraph(text: Text, source: tuple[int, str] | None, floor: int) -> None:
    if source is None or text.plain != source[1]:
        return
    offset = source[0]
    tail: deque[tuple[int, int]] = deque(maxlen=GLYPH_LIMIT)
    for start, end, glyph in graphemes(text.plain):
        if offset + start >= floor and not glyph.isspace() and safe_glyph(glyph):
            tail.append((start, end))
    for start, end in tail:
        text.stylize(Style(meta={ARRIVAL_META: (offset + start, offset + end)}), start, end)


@dataclass(frozen=True, slots=True)
class Arrival:
    start: int
    end: int
    at: float


class ArrivalTimeline:
    def __init__(self) -> None:
        self.arrivals: deque[Arrival] = deque(maxlen=256)
        # Retain birth times within the bounded tail even after expiry. Appending
        # combining marks must not repeatedly restart an existing glyph's life.
        self.births: dict[int, float] = {}

    def receive(self, start: int, end: int, now: float) -> None:
        if end > start:
            self.arrivals.append(Arrival(start, end, now))
        self.prune(now)
        self.births = {s: at for s, at in self.births.items() if s >= end - SOURCE_WINDOW}

    def prune(self, now: float) -> None:
        while self.arrivals and now - self.arrivals[0].at >= DURATION_SECONDS:
            self.arrivals.popleft()

    def age(self, source: tuple[int, int], now: float) -> float | None:
        start, end = source
        at = self.births.get(start)
        if at is None:
            # Earliest overlapping arrival: an expanded grapheme cannot extend
            # the animation's lifetime. Provider chunks are never split.
            at = next((a.at for a in self.arrivals if start < a.end and end > a.start), None)
            if at is None:
                return None
            self.births[start] = at
        age = now - at
        return age if 0 <= age < DURATION_SECONDS else None

    def clear(self) -> None:
        self.arrivals.clear()
        self.births.clear()


def ink_style(style: Style, age: float, rank: int, primary: str, secondary: str) -> Style:
    # Defensive guard in addition to paragraph eligibility. No semantic token
    # foreground, background, link or emphasis is ever replaced.
    if rank >= GLYPH_LIMIT or style.bold or style.italic or style.underline or style.link:
        return style
    phase = max(age / DURATION_SECONDS, rank / 16)
    level = primary if phase < 0.35 else secondary if phase < 0.65 else None
    return style + Style.parse(level) if level is not None else style
