"""Generation-local, monotonic presentation of parser-proven fenced code.

Token identities bind only the current parse. Lifecycles use normalized source
anchors within an AssistantMessage generation, never parser object identities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from markdown_it.token import Token

from neuro_code.interfaces.tui.fence_cache import closed_fences


class FencePhase(StrEnum):
    ACTIVE = "ACTIVE"
    FINALIZED = "FINALIZED"
    CACHED = "CACHED"


@dataclass(frozen=True)
class FenceIdentity:
    generation: int
    source_start: int


@dataclass
class FencePresentationRecord:
    identity: FenceIdentity
    phase: FencePhase = FencePhase.ACTIVE
    finalizations: int = 0

    @property
    def foreground_mode(self) -> str:
        return "PLAIN" if self.phase is FencePhase.ACTIVE else "SYNTAX"

    def finalize(self) -> None:
        if self.phase is FencePhase.ACTIVE:
            self.phase = FencePhase.FINALIZED
            self.finalizations += 1


class FencePresentation:
    """Message-owned lifecycle metadata, without render results or timers."""

    def __init__(self) -> None:
        self.generation = 0
        self.records: dict[FenceIdentity, FencePresentationRecord] = {}

    def reset(self) -> None:
        self.records.clear()
        self.generation += 1

    def resolve(
        self, markup: str, tokens: list[Token], *, complete: bool
    ) -> dict[int, FencePresentationRecord]:
        normalized = markup.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")
        parts = normalized.split("\n")
        lines = [line + "\n" for line in parts[:-1]]
        if parts[-1]:
            lines.append(parts[-1])
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        closed = closed_fences(markup, tokens)
        current: dict[int, FencePresentationRecord] = {}
        for token in tokens:
            if token.type != "fence" or token.level != 0 or token.map is None:
                continue
            start, end = token.map
            if not 0 <= start < end <= len(lines):
                continue
            opener = re.match(r" {0,3}([`~]{3,})", lines[start])
            if opener is None or opener.group(1) != token.markup:
                continue
            explicitly_closed = id(token) in closed
            if not explicitly_closed and end != len(lines):
                continue  # An unproven parser/container boundary stays on Rich's full path.
            identity = FenceIdentity(self.generation, offsets[start])
            record = self.records.setdefault(identity, FencePresentationRecord(identity))
            # At streaming EOF, a bare closing candidate can still receive text
            # and cease to be a closing fence. Only a newline seals that line.
            stable_close = explicitly_closed and lines[end - 1].endswith("\n")
            if complete or stable_close:
                record.finalize()
            current[id(token)] = record  # This binding is valid only for this parse.
        return current

    def mark_cached(self, identity: FenceIdentity) -> None:
        record = self.records.get(identity)
        if record is not None and record.phase is not FencePhase.ACTIVE:
            record.phase = FencePhase.CACHED
