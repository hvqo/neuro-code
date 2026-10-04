"""Message-local reuse of closed fenced-code render output.

Full Markdown parsing and Rich/Pygments retain ownership of syntax semantics.
缓存仅属于单条消息;完整 Markdown 解析与 Rich/Pygments 继续决定语义.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from sys import getsizeof

from markdown_it.token import Token
from rich.console import Console, ConsoleOptions
from rich.segment import Segment

from neuro_code.interfaces.tui.syntax import ResolvedSyntaxTheme


@dataclass(frozen=True)
class FenceRenderKey:
    code: str
    language: str
    theme: str | tuple[str, str, str]
    context: tuple[object, ...]


def closed_fences(markup: str, tokens: list[Token]) -> frozenset[int]:
    """Prove explicit top-level closure using the authoritative parser's map.

    Parent-autoclosed nested fences and indented code fail closed. Do not guess
    closure from EOF or from a blank line. Source normalization matches MarkdownIt.
    Nested containers still render normally; their deindentation is not reimplemented.
    """
    fences = [t for t in tokens if t.type == "fence" and t.level == 0 and t.map]
    if not fences:
        return frozenset()
    lines = markup.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd").split("\n")
    closed = set()
    for token in fences:
        assert token.map is not None
        start, end = token.map
        marker = token.markup
        if (
            len(marker) >= 3
            and marker[0] in "`~"
            and len(set(marker)) == 1
            and end > start + 1
            and end <= len(lines)
            and re.fullmatch(
                r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*",
                lines[end - 1],
            )
        ):
            closed.add(id(token))
    return frozenset(closed)


def fence_render_key(
    code: str, language: str, theme: object, console: Console, options: ConsoleOptions
) -> FenceRenderKey | None:
    # Our resolved themes are immutable selections. Unknown user-supplied Rich
    # theme objects may be mutable: render normally rather than guess identity.
    theme_key: str | tuple[str, str, str]
    if isinstance(theme, str):
        theme_key = theme
    elif type(theme) is ResolvedSyntaxTheme:
        theme_key = (theme.resolved_choice.value, theme.background, theme.foreground)
    else:
        return None
    # Cached values are Rich segments before viewport-height cropping. Height
    # budgets therefore must not split an otherwise identical syntax render
    # (Textual's measurement and paint passes often use different max heights).
    return FenceRenderKey(
        code,
        language,
        theme_key,
        (
            options.min_width,
            options.max_width,
            options.justify,
            options.overflow,
            options.no_wrap,
            options.highlight,
            options.markup,
            options.ascii_only,
            options.encoding,
            options.legacy_windows,
            options.is_terminal,
            console.color_system,
            console.get_style("markdown.code_block", default="none"),
            console.get_style("syntax", default="none"),
        ),
    )


class FenceRenderCache:
    """Bounded LRU owned by one AssistantMessage, without revision archives.

    Width/theme/style changes miss by key; returning to an unchanged presentation
    can reuse an entry. Disposal releases entries. Oversized output is not retained.
    Byte accounting is an estimate, complemented by a hard entry bound.
    """

    def __init__(self, *, max_entries: int = 32, max_bytes: int = 8 * 1024 * 1024) -> None:
        self.max_entries = max(0, max_entries)
        self.max_bytes = max(0, max_bytes)
        self._entries: OrderedDict[FenceRenderKey, tuple[tuple[Segment, ...], int]] = OrderedDict()
        self.estimated_bytes = 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: FenceRenderKey) -> tuple[Segment, ...] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: FenceRenderKey, segments: tuple[Segment, ...]) -> bool:
        styles = {segment.style for segment in segments if segment.style is not None}
        size = (
            getsizeof(key)
            + getsizeof(key.code)
            + getsizeof(key.language)
            + getsizeof(key.theme)
            + getsizeof(key.context)
            + getsizeof(segments)
            + sum(getsizeof(s) + getsizeof(s.text) for s in segments)
            + sum(getsizeof(s) + getsizeof(str(s)) + 256 for s in styles)
            + 512
        )
        previous = self._entries.pop(key, None)
        if previous is not None:
            self.estimated_bytes -= previous[1]
        if not self.max_entries or size > self.max_bytes:
            return False
        self._entries[key] = (segments, size)
        self.estimated_bytes += size
        while len(self._entries) > self.max_entries or self.estimated_bytes > self.max_bytes:
            self.estimated_bytes -= self._entries.popitem(last=False)[1][1]
        return key in self._entries

    def clear(self) -> None:
        self._entries.clear()
        self.estimated_bytes = 0
