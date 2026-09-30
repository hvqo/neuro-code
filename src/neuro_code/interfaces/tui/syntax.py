"""Curated Pygments styles adapted to the UI-owned code surface.

Only code token foregrounds are resolved here. Lexing remains owned by
Rich/Pygments; no language grammar, application state or terminal probe lives here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import cast

from pygments.styles import get_style_by_name
from pygments.token import Comment, Generic, Keyword, Name, Number, String, Text, _TokenType
from rich.style import Style
from rich.syntax import SyntaxTheme as RichSyntaxTheme
from rich.syntax import TokenType

from neuro_code.shared.syntax_theme import SyntaxTheme


@dataclass(frozen=True, slots=True)
class SyntaxThemeDefinition:
    label: str
    pygments_style: str | None


SYNTAX_THEMES: Mapping[SyntaxTheme, SyntaxThemeDefinition] = MappingProxyType(
    {
        SyntaxTheme.AUTO: SyntaxThemeDefinition("Auto", None),
        SyntaxTheme.GITHUB_DARK: SyntaxThemeDefinition("GitHub Dark", "github-dark"),
        SyntaxTheme.ONE_DARK: SyntaxThemeDefinition("One Dark", "one-dark"),
        SyntaxTheme.MONOKAI: SyntaxThemeDefinition("Monokai", "monokai"),
        SyntaxTheme.DRACULA: SyntaxThemeDefinition("Dracula", "dracula"),
        SyntaxTheme.FRIENDLY: SyntaxThemeDefinition("Friendly Light", "friendly"),
        SyntaxTheme.SOLARIZED_LIGHT: SyntaxThemeDefinition("Solarized Light", "solarized-light"),
    }
)


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _luminance(color: str) -> float:
    channels = (channel / 255 for channel in _rgb(color))
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def contrast_ratio(foreground: str, background: str) -> float:
    """RGB engineering guard; never pretend an unknown ANSI palette has RGB."""
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@lru_cache(maxsize=2048)
def readable_token_color(color: str, background: str) -> str:
    """Keep mature palette hues; adjust only when the actual surface needs it."""
    if contrast_ratio(color, background) >= 4.5:
        return color
    target = 0 if _luminance(background) >= 0.179 else 255
    channels = _rgb(color)
    for amount in range(1, 101):
        mixed = tuple(round(c + (target - c) * amount / 100) for c in channels)
        adjusted = "#" + "".join(f"{c:02x}" for c in mixed)
        if contrast_ratio(adjusted, background) >= 4.5:
            return adjusted
    return f"#{target:02x}{target:02x}{target:02x}"


class ResolvedSyntaxTheme(RichSyntaxTheme):
    """Foreground-only token styles with a background supplied by the UI."""

    def __init__(self, choice: SyntaxTheme, background: str, foreground: str) -> None:
        self.choice = choice
        self.background = background
        self.foreground = foreground
        self.terminal_fallback = not background.startswith("#")
        self.resolved_choice = (
            SyntaxTheme.GITHUB_DARK
            if choice is SyntaxTheme.AUTO
            and not self.terminal_fallback
            and _luminance(background) < 0.179
            else SyntaxTheme.FRIENDLY
            if choice is SyntaxTheme.AUTO
            else choice
        )
        self._pygments_style = get_style_by_name(self.resolved_choice.value)
        self._styles: dict[TokenType, Style] = {}

    def get_background_style(self) -> Style:
        return Style(bgcolor=self.background)

    def get_style_for_token(self, token_type: TokenType) -> Style:
        if token_type not in self._styles:
            self._styles[token_type] = self._resolve_token(cast(_TokenType, token_type))
        return self._styles[token_type]

    def _resolve_token(self, token_type: _TokenType) -> Style:
        if self.terminal_fallback:
            return _ansi_token_style(token_type)
        token = token_type
        while token not in self._pygments_style.styles and token.parent is not None:
            token = token.parent
        raw = self._pygments_style.style_for_token(token)
        # Some curated styles leave diff insertion/hunk roles identical to
        # context. Reuse the style's existing semantic foregrounds in that
        # case, rather than introduce a second hand-maintained palette.
        neutral = self._pygments_style.style_for_token(Text)["color"]
        for category, fallback in (
            (Generic.Inserted, Name.Function),
            (Generic.Subheading, Keyword),
            (Generic.Heading, Name.Class),
        ):
            if category in token_type.split() and raw["color"] == neutral:
                raw = self._pygments_style.style_for_token(fallback)
                break
        color = "#" + raw["color"] if raw["color"] else self.foreground
        return Style(
            color=readable_token_color(color, self.background),
            # Code uses the same regular-weight baseline as V1B; comments may
            # retain the curated style's italic distinction. No token fills.
            bold=False,
            italic=bool(raw["italic"]),
        )


def _ansi_token_style(token: _TokenType) -> Style:
    if Comment in token.split():
        return Style(color="default", dim=True, italic=True, bold=False)
    for category, color in (
        (Generic.Deleted, "red"),
        (Generic.Inserted, "green"),
        (Generic.Subheading, "cyan"),
        (Generic.Heading, "cyan"),
        (Keyword.Type, "cyan"),
        (Name.Class, "cyan"),
        (Name.Builtin, "cyan"),
        (Name.Decorator, "magenta"),
        (Keyword, "magenta"),
        (String, "green"),
        (Number, "yellow"),
        (Name.Function, "blue"),
    ):
        if category in token.split():
            return Style(color=color, bold=False)
    return Style(color="default", bold=False)


@lru_cache(maxsize=128)
def resolve_syntax_theme(
    choice: SyntaxTheme, background: str, foreground: str
) -> ResolvedSyntaxTheme:
    """Cache app-independent, immutable selections without mutating palettes."""
    return ResolvedSyntaxTheme(choice, background, foreground)
