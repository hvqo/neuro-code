"""Semantic palettes for the Textual terminal interface.

This module deliberately contains presentation-only values.  It must not own
application state, controller decisions, or terminal interaction behavior.

Textual 终端界面使用的暖瓷白与曜石黑视觉令牌.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import ClassVar, Protocol

from pygments.style import Style as PygmentsStyle
from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    String,
    Text,
    Whitespace,
    _TokenType,
)
from rich.style import Style
from rich.syntax import PygmentsSyntaxTheme, SyntaxTheme, TokenType
from rich.theme import Theme as RichTheme
from textual.theme import Theme

from neuro_code.shared.ui_theme import UiTheme

BG_0 = "#F6F5F2"
BG_1 = "#FFFFFF"
BG_2 = "#F1EFEB"
BG_3 = "#E5E1DA"
BORDER_SUBTLE = "#CFC9C1"
BORDER = "#92887E"
FG_DIM = "#827C75"
FG_MUTED = "#68625C"
FG_SECONDARY = "#58534E"
FG_EMPHASIS = "#49443F"
FG_BODY = "#2E2B28"
FG_PRIMARY = "#262320"

# Focus stays restrained; text roles use independent, theme-aware accents.
ACCENT = "#76618F"
SUCCESS = "#526F56"
WARNING = "#8A632E"
ERROR = "#A34650"

# Compatibility aliases keep render call sites explicit while all values still
# originate from the compact semantic token set above.
BACKGROUND = BG_0
SURFACE = BG_1
SURFACE_HOVER = BG_2
SURFACE_SUBTLE = BG_2
SURFACE_SELECTED = BG_3
BORDER_DIM = BORDER_SUBTLE
BORDER_NORMAL = BORDER
BORDER_FOCUS = ACCENT
TEXT_DIM = FG_DIM
TEXT_PLACEHOLDER = FG_SECONDARY
TEXT_DISABLED = FG_DIM
TEXT_MUTED = FG_MUTED
TEXT_SECONDARY = FG_SECONDARY
TEXT_EMPHASIS = FG_EMPHASIS
TEXT_BODY = FG_BODY
TEXT_PRIMARY = FG_PRIMARY
BRAND_TEXT = FG_PRIMARY

# Semantic accents remain quiet, with enough contrast on both porcelain surfaces.
ACCENT_BLUE = "#285F9B"
ACCENT_CYAN = "#246E73"
ACCENT_ORANGE = "#995328"
ACCENT_VIOLET = "#79519C"
ACCENT_CODE = ACCENT_CYAN
ACCENT_LINK = ACCENT_BLUE
ACCENT_NUMBER = ACCENT_ORANGE
ACCENT_SUCCESS = SUCCESS
ACCENT_WARNING = WARNING
ACCENT_ERROR = ERROR
SYNTAX_OPERATOR = FG_SECONDARY

# Historical export name retained for callers; these are now warm neutrals.
MONO_COLORS = (
    BACKGROUND,
    SURFACE,
    SURFACE_HOVER,
    SURFACE_SUBTLE,
    SURFACE_SELECTED,
    BORDER_DIM,
    BORDER,
    TEXT_DIM,
    TEXT_PLACEHOLDER,
    TEXT_DISABLED,
    TEXT_MUTED,
    TEXT_SECONDARY,
    TEXT_EMPHASIS,
    TEXT_BODY,
    TEXT_PRIMARY,
    BRAND_TEXT,
)

TEXTUAL_THEME = Theme(
    name="neuro-code-porcelain",
    dark=False,
    primary=BORDER_FOCUS,
    secondary=TEXT_SECONDARY,
    accent=TEXT_EMPHASIS,
    warning=WARNING,
    error=ERROR,
    success=SUCCESS,
    foreground=TEXT_PRIMARY,
    background=BACKGROUND,
    surface=SURFACE,
    panel=SURFACE,
    boost=SURFACE_HOVER,
    luminosity_spread=0.08,
    text_alpha=1.0,
    variables={
        "modal-overlay": "#29252E",
        "border": BORDER,
        "border-normal": BORDER_NORMAL,
        "border-subtle": BORDER_SUBTLE,
        "border-dim": BORDER_DIM,
        "border-focus": BORDER_FOCUS,
        "bg-0": BG_0,
        "bg-1": BG_1,
        "bg-2": BG_2,
        "bg-3": BG_3,
        "fg-primary": FG_PRIMARY,
        "fg-secondary": FG_SECONDARY,
        "fg-muted": FG_MUTED,
        "space-0": "0",
        "space-1": "1",
        "space-2": "2",
        "space-3": "3",
        "space-4": "4",
        "space-6": "6",
        "space-8": "8",
        "surface-hover": SURFACE_HOVER,
        "surface-subtle": SURFACE_SUBTLE,
        "surface-selected": SURFACE_SELECTED,
        "text-primary": TEXT_PRIMARY,
        "text-body": TEXT_BODY,
        "text-secondary": TEXT_SECONDARY,
        "text-muted": TEXT_MUTED,
        "text-secondary-intensity": "none",
        "text-muted-intensity": "none",
        "text-dim": TEXT_DIM,
        "text-placeholder": TEXT_PLACEHOLDER,
        "text-disabled": TEXT_DISABLED,
        "text-emphasis": TEXT_EMPHASIS,
        "brand-text": BRAND_TEXT,
        "block-cursor-background": BORDER_FOCUS,
        "block-cursor-foreground": BACKGROUND,
        "block-hover-background": SURFACE_HOVER,
        "button-color-foreground": BACKGROUND,
        # Keep the current neutral focus treatment for graphical themes. System
        # overrides this with terminal-native reverse video.
        "button-focus-text-style": "none",
        "selected-button-text-style": "bold",
        "footer-background": BACKGROUND,
        "footer-description-background": BACKGROUND,
        "footer-description-foreground": TEXT_MUTED,
        "footer-item-background": BACKGROUND,
        "footer-key-background": BACKGROUND,
        "footer-key-foreground": TEXT_EMPHASIS,
        "input-cursor-background": TEXT_PRIMARY,
        "input-cursor-foreground": BACKGROUND,
        "input-selection-background": SURFACE_SELECTED,
        "composer-surface": SURFACE,
        "composer-border": BORDER_NORMAL,
        "composer-focus-border": ACCENT,
        "composer-muted": TEXT_MUTED,
        "composer-selection": ACCENT,
        "composer-selection-text": BG_0,
        "user-message-surface": SURFACE_SUBTLE,
        "user-message-border": BORDER_NORMAL,
        "scrollbar": BORDER,
        "scrollbar-active": TEXT_SECONDARY,
        "scrollbar-background": BACKGROUND,
        "scrollbar-hover": TEXT_MUTED,
    },
)

MARKDOWN_THEME = RichTheme(
    {
        "markdown.paragraph": TEXT_BODY,
        "markdown.text": TEXT_BODY,
        "markdown.em": f"italic {TEXT_EMPHASIS}",
        "markdown.strong": f"bold {TEXT_PRIMARY}",
        # Inline code is part of a prose line, not a panel or chip. Keep the
        # accent on the foreground only; fenced code retains its own surface.
        "markdown.code": ACCENT_CODE,
        "markdown.code_block": f"{TEXT_BODY} on {SURFACE}",
        "markdown.block_quote": f"italic {TEXT_SECONDARY}",
        "markdown.list": TEXT_BODY,
        "markdown.item": TEXT_BODY,
        "markdown.item.bullet": f"bold {TEXT_SECONDARY}",
        "markdown.item.number": f"bold {TEXT_SECONDARY}",
        "markdown.hr": BORDER_SUBTLE,
        "markdown.h1": f"bold {TEXT_PRIMARY}",
        "markdown.h2": f"bold {TEXT_PRIMARY}",
        "markdown.h3": f"bold {TEXT_EMPHASIS}",
        "markdown.h4": f"bold {TEXT_BODY}",
        "markdown.h5": f"bold {TEXT_EMPHASIS}",
        "markdown.h6": f"bold {TEXT_SECONDARY}",
        "markdown.link": f"underline {ACCENT}",
        "markdown.link_url": f"underline {ACCENT}",
        "markdown.table.border": BORDER_SUBTLE,
        "markdown.table.header": f"bold {TEXT_EMPHASIS}",
        "markdown.kbd": f"bold {TEXT_EMPHASIS} on {SURFACE_SELECTED}",
    }
)


class _MonochromePygmentsStyle(PygmentsStyle):
    """Pygments token styles for fenced Markdown code blocks.

    用于 Markdown 围栏代码块的 Pygments 令牌样式."""

    background_color: ClassVar[str] = SURFACE
    styles: ClassVar[Mapping[_TokenType, str]] = {
        Text: TEXT_BODY,
        Whitespace: TEXT_BODY,
        Comment: f"italic {TEXT_MUTED}",
        Keyword: f"bold {ACCENT_VIOLET}",
        Keyword.Type: f"bold {ACCENT_CODE}",
        Operator: SYNTAX_OPERATOR,
        Operator.Word: f"bold {SYNTAX_OPERATOR}",
        Name: TEXT_BODY,
        Name.Builtin: ACCENT_CODE,
        Name.Function: f"bold {ACCENT_BLUE}",
        Name.Class: f"bold {ACCENT_WARNING}",
        Name.Decorator: ACCENT_ORANGE,
        String: ACCENT_SUCCESS,
        Number: ACCENT_NUMBER,
        Generic.Deleted: ACCENT_ERROR,
        Generic.Inserted: f"bold {ACCENT_SUCCESS}",
        Generic.Heading: f"bold {TEXT_PRIMARY}",
        Generic.Subheading: f"bold {ACCENT_CODE}",
        Error: f"bold {ACCENT_ERROR}",
    }


MONO_SYNTAX_THEME = PygmentsSyntaxTheme(_MonochromePygmentsStyle)

# Shared semantic pairs: CSS, Rich text and Pygments resolve through the same
# palette. Resolution is app-local; no global palette is mutated on a switch.
_DARK_COLORS = {
    BG_0: "#171717",
    BG_1: "#202020",
    BG_2: "#292824",
    BG_3: "#353330",
    BORDER_SUBTLE: "#4B4946",
    BORDER: "#77736D",
    FG_DIM: "#96938D",
    FG_MUTED: "#A39E95",
    FG_SECONDARY: "#C8C3B9",
    FG_EMPHASIS: "#DDD8D0",
    FG_BODY: "#ECE9E3",
    FG_PRIMARY: "#F3F0EA",
    ACCENT: "#CBB898",
    SUCCESS: "#ADC0A4",
    WARNING: "#D1B37F",
    ERROR: "#D9A29C",
}


def _blend_hex(start: str, end: str) -> str:
    """Create a quiet intermediate role for existing RGB theme variants."""

    channels = (
        (int(start[index : index + 2], 16) + int(end[index : index + 2], 16)) // 2
        for index in (1, 3, 5)
    )
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def _palette(
    background: str,
    surface: str,
    selected: str,
    border: str,
    muted: str,
    secondary: str,
    foreground: str,
    accent: str,
    success: str,
    warning: str,
    error: str,
) -> dict[str, str]:
    return dict(
        zip(
            (
                BG_0,
                BG_1,
                BG_2,
                BG_3,
                BORDER_SUBTLE,
                BORDER,
                FG_DIM,
                FG_MUTED,
                FG_SECONDARY,
                FG_EMPHASIS,
                FG_BODY,
                FG_PRIMARY,
                ACCENT,
                SUCCESS,
                WARNING,
                ERROR,
            ),
            (
                background,
                surface,
                _blend_hex(surface, selected),
                selected,
                _blend_hex(surface, border),
                border,
                muted,
                muted,
                secondary,
                foreground,
                foreground,
                foreground,
                accent,
                success,
                warning,
                error,
            ),
            strict=True,
        )
    )


# Independently mapped semantic roles, using upstream palette values. Sources and
# variant names are documented in docs/{en,zh-CN}/tui-themes.md.
_PALETTES = {
    UiTheme.PORCELAIN: {},
    UiTheme.GRAPHITE: _DARK_COLORS,
    # System is terminal-native: broad surfaces share the terminal default
    # background. Borders, dim text, ANSI state accents, and selection semantics
    # provide hierarchy without assuming any particular ANSI palette luminance.
    UiTheme.SYSTEM: {
        BG_0: "default",
        BG_1: "default",
        BG_2: "default",
        BG_3: "default",
        BORDER_SUBTLE: "white",
        BORDER: "default",
        FG_DIM: "default",
        FG_MUTED: "default",
        FG_SECONDARY: "default",
        FG_EMPHASIS: "default",
        FG_BODY: "default",
        FG_PRIMARY: "default",
        ACCENT: "bright_blue",
        SUCCESS: "green",
        WARNING: "yellow",
        ERROR: "red",
    },
    UiTheme.TOKYONIGHT: _palette(
        "#1A1B26",
        "#16161E",
        "#292E42",
        "#414868",
        "#737AA2",
        "#A9B1D6",
        "#C0CAF5",
        "#7AA2F7",
        "#9ECE6A",
        "#E0AF68",
        "#F7768E",
    ),
    UiTheme.EVERFOREST: _palette(
        "#2D353B",
        "#343F44",
        "#3D484D",
        "#475258",
        "#9DA9A0",
        "#D3C6AA",
        "#D3C6AA",
        "#A7C080",
        "#83C092",
        "#DBBC7F",
        "#E67E80",
    ),
    UiTheme.AYU: _palette(
        "#0D1017",
        "#141821",
        "#1B1F29",
        "#475266",
        "#8A9199",
        "#BFBDB6",
        "#BFBDB6",
        "#E6B450",
        "#AAD94C",
        "#FFB454",
        "#F07178",
    ),
    UiTheme.CATPPUCCIN: _palette(
        "#1E1E2E",
        "#181825",
        "#313244",
        "#45475A",
        "#9399B2",
        "#BAC2DE",
        "#CDD6F4",
        "#CBA6F7",
        "#A6E3A1",
        "#F9E2AF",
        "#F38BA8",
    ),
    UiTheme.CATPPUCCIN_MACCHIATO: _palette(
        "#24273A",
        "#1E2030",
        "#363A4F",
        "#494D64",
        "#939AB7",
        "#B8C0E0",
        "#CAD3F5",
        "#C6A0F6",
        "#A6DA95",
        "#EED49F",
        "#ED8796",
    ),
    UiTheme.GRUVBOX: _palette(
        "#282828",
        "#32302F",
        "#3C3836",
        "#504945",
        "#A89984",
        "#D5C4A1",
        "#EBDBB2",
        "#D79921",
        "#B8BB26",
        "#FABD2F",
        "#FB4934",
    ),
    UiTheme.KANAGAWA: _palette(
        "#1F1F28",
        "#2A2A37",
        "#363646",
        "#54546D",
        "#9A978B",
        "#C8C093",
        "#DCD7BA",
        "#7E9CD8",
        "#98BB6C",
        "#E6C384",
        "#E46876",
    ),
    UiTheme.NORD: _palette(
        "#2E3440",
        "#3B4252",
        "#434C5E",
        "#4C566A",
        "#A5B0C2",
        "#D8DEE9",
        "#E5E9F0",
        "#88C0D0",
        "#A3BE8C",
        "#EBCB8B",
        "#BF616A",
    ),
    UiTheme.MATRIX: _palette(
        "#090D0A",
        "#101A13",
        "#1C2D20",
        "#304D37",
        "#82A58B",
        "#A0CCAA",
        "#B7E6C1",
        "#68D391",
        "#8DE0A6",
        "#D7CE84",
        "#EF9B91",
    ),
    UiTheme.ONE_DARK: _palette(
        "#282C34",
        "#333841",
        "#3E4452",
        "#4B5263",
        "#828997",
        "#ABB2BF",
        "#DCDFE4",
        "#61AFEF",
        "#98C379",
        "#E5C07B",
        "#E06C75",
    ),
}


# Text-only accents. Custom light colors are darker for legibility; dark themes
# use gentler hues. System delegates colors to the terminal's ANSI palette.
_TEXT_ACCENTS: dict[UiTheme, tuple[str, str, str, str]] = {
    UiTheme.PORCELAIN: (ACCENT_BLUE, ACCENT_CYAN, ACCENT_ORANGE, ACCENT_VIOLET),
    UiTheme.GRAPHITE: ("#9CBDE0", "#91C4BF", "#D6AB83", "#BDAAD7"),
    UiTheme.SYSTEM: ("blue", "cyan", "yellow", "magenta"),
    UiTheme.TOKYONIGHT: ("#7AA2F7", "#7DCFFF", "#FF9E64", "#BB9AF7"),
    UiTheme.EVERFOREST: ("#7FBBB3", "#83C092", "#E69875", "#D699B6"),
    UiTheme.AYU: ("#73B8FF", "#95E6CB", "#FFAD66", "#D2A6FF"),
    UiTheme.CATPPUCCIN: ("#89B4FA", "#94E2D5", "#FAB387", "#CBA6F7"),
    UiTheme.CATPPUCCIN_MACCHIATO: ("#8AADF4", "#8BD5CA", "#F5A97F", "#C6A0F6"),
    UiTheme.GRUVBOX: ("#83A598", "#8EC07C", "#FE8019", "#D3869B"),
    UiTheme.KANAGAWA: ("#7E9CD8", "#7FB4CA", "#FFA066", "#957FB8"),
    UiTheme.NORD: ("#81A1C1", "#8FBCBB", "#D08770", "#B48EAD"),
    UiTheme.MATRIX: ("#80C8B0", "#79D6C5", "#D7C17A", "#A6CE8E"),
    UiTheme.ONE_DARK: ("#61AFEF", "#56B6C2", "#D19A66", "#C678DD"),
}
for _choice, _accents in _TEXT_ACCENTS.items():
    _PALETTES[_choice].update(
        zip((ACCENT_BLUE, ACCENT_CYAN, ACCENT_ORANGE, ACCENT_VIOLET), _accents, strict=True)
    )

# Dedicated conversation surfaces retain each palette's hue while separating
# editable input and previous user turns from the canvas. These are deliberately
# independent of code blocks, menus, and other surfaces.
# Order: composer fill, user-message fill, unfocused composer edge.
_CONVERSATION_SURFACES: dict[UiTheme, tuple[str, str, str]] = {
    UiTheme.TOKYONIGHT: ("#303449", "#25293B", "#737AA2"),
    UiTheme.EVERFOREST: ("#414E50", "#374447", "#9DA9A0"),
    UiTheme.AYU: ("#242C39", "#1D2430", "#8A9199"),
    UiTheme.CATPPUCCIN: ("#36374D", "#2A2B3D", "#9399B2"),
    UiTheme.CATPPUCCIN_MACCHIATO: ("#3A3F59", "#30344B", "#939AB7"),
    UiTheme.GRUVBOX: ("#444039", "#36332D", "#A89984"),
    UiTheme.KANAGAWA: ("#363644", "#2C2C38", "#9A978B"),
    UiTheme.NORD: ("#465268", "#3A4558", "#A5B0C2"),
    UiTheme.MATRIX: ("#23392B", "#1B2B21", "#82A58B"),
    UiTheme.ONE_DARK: ("#3E4654", "#343B47", "#828997"),
}


def _translate(style: str, colors: Mapping[str, str]) -> str:
    # One pass avoids translating a target that is also a source color.
    return re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: colors.get(m[0].upper(), m[0]), style)


def _semantic_rich_style(style: str, colors: Mapping[str, str], choice: UiTheme) -> str:
    translated = _translate(style, colors)
    if choice is UiTheme.SYSTEM and any(
        token in style.upper() for token in (FG_DIM, FG_MUTED, FG_SECONDARY)
    ):
        return f"dim {translated}"
    return translated


def _textual_theme(choice: UiTheme) -> Theme:
    if choice is UiTheme.PORCELAIN:
        return TEXTUAL_THEME
    colors = _PALETTES[choice]

    def css(value: str) -> str:
        translated = _translate(value, colors)
        return f"ansi_{translated}" if choice is UiTheme.SYSTEM else translated

    variables = {
        key: css(value) if value.startswith("#") else value
        for key, value in TEXTUAL_THEME.variables.items()
    }
    variables["modal-overlay"] = "ansi_default" if choice is UiTheme.SYSTEM else "#000000"
    if choice is UiTheme.SYSTEM:
        variables["input-selection-background"] = "ansi_blue"
        variables["button-focus-text-style"] = "bold reverse"
        variables["text-secondary-intensity"] = "dim"
        variables["text-muted-intensity"] = "dim"
        variables.update(
            {
                "composer-surface": "ansi_default",
                "composer-border": "ansi_default",
                "composer-focus-border": "ansi_bright_blue",
                "composer-muted": "ansi_default",
                "composer-selection": "ansi_blue",
                "composer-selection-text": "ansi_bright_white",
                "selected-button-text-style": "bold reverse",
                "user-message-surface": "ansi_default",
                "user-message-border": "ansi_default",
            }
        )
    elif choice is UiTheme.GRAPHITE:
        variables.update(
            {
                "composer-surface": css(SURFACE_SUBTLE),
                "composer-border": css(BORDER_NORMAL),
                "composer-focus-border": css(BORDER_FOCUS),
                "composer-muted": css(TEXT_MUTED),
                "composer-selection": css(ACCENT),
                "composer-selection-text": css(BG_0),
                "user-message-surface": css(SURFACE_SUBTLE),
                "user-message-border": css(BORDER_NORMAL),
            }
        )
    else:
        composer, user_message, edge = _CONVERSATION_SURFACES[choice]
        variables.update(
            {
                "composer-surface": composer,
                "composer-border": edge,
                "composer-focus-border": css(ACCENT),
                "composer-muted": css(FG_SECONDARY),
                "composer-selection": css(ACCENT),
                "composer-selection-text": css(BG_0),
                "user-message-surface": user_message,
                "user-message-border": css(ACCENT),
            }
        )
    return replace(
        TEXTUAL_THEME,
        name=choice.textual_name,
        dark=choice is not UiTheme.SYSTEM,
        primary=css(ACCENT),
        secondary=css(TEXT_SECONDARY),
        accent=css(ACCENT),
        warning=css(WARNING),
        error=css(ERROR),
        success=css(SUCCESS),
        foreground=css(TEXT_PRIMARY),
        background=css(BACKGROUND),
        surface=css(SURFACE),
        panel=css(SURFACE),
        boost=css(SURFACE_HOVER),
        variables=variables,
    )


class _PaletteSyntaxTheme(SyntaxTheme):
    def __init__(self, colors: Mapping[str, str]) -> None:
        self.colors = colors

    def get_style_for_token(self, token_type: TokenType) -> Style:
        return Style.parse(
            _translate(str(MONO_SYNTAX_THEME.get_style_for_token(token_type)), self.colors)
        )

    def get_background_style(self) -> Style:
        return Style(bgcolor=self.colors[SURFACE])


TEXTUAL_THEMES = {choice: _textual_theme(choice) for choice in UiTheme}
_MARKDOWN_THEMES = {
    choice: MARKDOWN_THEME
    if choice is UiTheme.PORCELAIN
    else RichTheme(
        {
            name: _semantic_rich_style(str(style), colors, choice)
            for name, style in MARKDOWN_THEME.styles.items()
        }
    )
    for choice, colors in _PALETTES.items()
}
_SYNTAX_THEMES: dict[UiTheme, SyntaxTheme] = {
    choice: MONO_SYNTAX_THEME if choice is UiTheme.PORCELAIN else _PaletteSyntaxTheme(colors)
    for choice, colors in _PALETTES.items()
}
# Preserve public aliases and the saved Graphite identifier.
GRAPHITE_THEME = TEXTUAL_THEMES[UiTheme.GRAPHITE]
GRAPHITE_MARKDOWN_THEME = _MARKDOWN_THEMES[UiTheme.GRAPHITE]
GRAPHITE_SYNTAX_THEME = _SYNTAX_THEMES[UiTheme.GRAPHITE]


class _ThemeHost(Protocol):
    @property
    def theme(self) -> str: ...


class _ThemeOwner(Protocol):
    @property
    def app(self) -> _ThemeHost: ...


def theme_style(owner: _ThemeOwner, style: str) -> str:
    """Resolve colors without modifying model text or other app instances."""
    choice = UiTheme.from_textual_name(owner.app.theme)
    return _semantic_rich_style(style, _PALETTES[choice], choice)


def markdown_theme(owner: _ThemeOwner) -> RichTheme:
    return _MARKDOWN_THEMES[UiTheme.from_textual_name(owner.app.theme)]


def syntax_theme(owner: _ThemeOwner) -> SyntaxTheme:
    return _SYNTAX_THEMES[UiTheme.from_textual_name(owner.app.theme)]


EFFORT_STYLES = {
    "low": TEXT_EMPHASIS,
    "medium": TEXT_EMPHASIS,
    "high": TEXT_EMPHASIS,
    "xhigh": TEXT_EMPHASIS,
    "max": TEXT_EMPHASIS,
    "ultracode": TEXT_EMPHASIS,
}

MODE_STYLES = {
    "normal": TEXT_EMPHASIS,
    "accept-edits": TEXT_EMPHASIS,
    "plan": TEXT_EMPHASIS,
    "auto": TEXT_EMPHASIS,
}

USER_TEXT_STYLE = TEXT_PRIMARY
ASSISTANT_TEXT_STYLE = TEXT_BODY
SYSTEM_LABEL_STYLE = f"bold {TEXT_EMPHASIS}"
SYSTEM_TEXT_STYLE = TEXT_BODY
STATUS_LABEL_STYLE = f"bold {TEXT_SECONDARY}"
STATUS_TEXT_STYLE = TEXT_SECONDARY
RECOVERABLE_LABEL_STYLE = f"bold {ACCENT_WARNING}"
RECOVERABLE_TEXT_STYLE = TEXT_EMPHASIS
TOOL_LABEL_STYLE = f"bold {TEXT_SECONDARY}"
TOOL_TEXT_STYLE = TEXT_SECONDARY
TOOL_TITLE_STYLE = TEXT_SECONDARY
TOOL_ACTIVE_STYLE = ACCENT
TOOL_COMPLETE_STYLE = f"bold {ACCENT_SUCCESS}"
TOOL_META_STYLE = TEXT_SECONDARY
TOOL_DETAIL_STYLE = TEXT_SECONDARY
TOOL_GUIDE_STYLE = TEXT_MUTED
ERROR_LABEL_STYLE = f"bold {ACCENT_ERROR}"
ERROR_TEXT_STYLE = f"bold {ACCENT_ERROR}"
ERROR_DETAIL_STYLE = ACCENT_ERROR
WAITING_STYLE = ACCENT_WARNING

DIFF_HUNK_STYLE = f"bold {ACCENT_VIOLET} on {SURFACE_SELECTED}"
DIFF_FILE_STYLE = f"bold {ACCENT_BLUE}"
DIFF_ADDITION_STYLE = f"{ACCENT_SUCCESS} on {SURFACE_SELECTED}"
DIFF_DELETION_STYLE = f"{ACCENT_ERROR} on {SURFACE_HOVER}"
DIFF_CONTEXT_STYLE = TEXT_BODY
DIFF_SUMMARY_ADDITION_STYLE = f"bold {ACCENT_SUCCESS} on {SURFACE_SELECTED}"
DIFF_SUMMARY_DELETION_STYLE = f"bold {ACCENT_ERROR} on {SURFACE_HOVER}"

LOADING_LEVEL_STYLES = (
    TEXT_DIM,
    TEXT_DISABLED,
    TEXT_MUTED,
    TEXT_SECONDARY,
    TEXT_EMPHASIS,
    TEXT_EMPHASIS,
    TEXT_EMPHASIS,
    TEXT_EMPHASIS,
)

CONNECTION_STATUS_STYLES = {
    "success": TOOL_COMPLETE_STYLE,
    "warning": f"bold {ACCENT_WARNING}",
    "error": ERROR_TEXT_STYLE,
}


def loading_style(level: int) -> str:
    """Return a bounded monochrome style for one loading-wave column.

    返回一个用于加载波列的有界单色样式."""

    safe_level = max(0, min(len(LOADING_LEVEL_STYLES) - 1, level))
    style = LOADING_LEVEL_STYLES[safe_level]
    return f"bold {style}" if safe_level == len(LOADING_LEVEL_STYLES) - 1 else style


__all__ = [
    "ACCENT",
    "ACCENT_CODE",
    "ACCENT_ERROR",
    "ACCENT_LINK",
    "ACCENT_NUMBER",
    "ACCENT_SUCCESS",
    "ACCENT_WARNING",
    "ASSISTANT_TEXT_STYLE",
    "BACKGROUND",
    "BG_0",
    "BG_1",
    "BG_2",
    "BG_3",
    "BORDER",
    "BORDER_DIM",
    "BORDER_FOCUS",
    "BORDER_NORMAL",
    "BORDER_SUBTLE",
    "BRAND_TEXT",
    "CONNECTION_STATUS_STYLES",
    "DIFF_ADDITION_STYLE",
    "DIFF_CONTEXT_STYLE",
    "DIFF_DELETION_STYLE",
    "DIFF_FILE_STYLE",
    "DIFF_HUNK_STYLE",
    "DIFF_SUMMARY_ADDITION_STYLE",
    "DIFF_SUMMARY_DELETION_STYLE",
    "EFFORT_STYLES",
    "ERROR",
    "ERROR_DETAIL_STYLE",
    "ERROR_LABEL_STYLE",
    "ERROR_TEXT_STYLE",
    "GRAPHITE_MARKDOWN_THEME",
    "GRAPHITE_SYNTAX_THEME",
    "GRAPHITE_THEME",
    "LOADING_LEVEL_STYLES",
    "MARKDOWN_THEME",
    "MODE_STYLES",
    "MONO_COLORS",
    "MONO_SYNTAX_THEME",
    "RECOVERABLE_LABEL_STYLE",
    "RECOVERABLE_TEXT_STYLE",
    "STATUS_LABEL_STYLE",
    "STATUS_TEXT_STYLE",
    "SUCCESS",
    "SURFACE",
    "SURFACE_HOVER",
    "SURFACE_SELECTED",
    "SURFACE_SUBTLE",
    "SYNTAX_OPERATOR",
    "SYSTEM_LABEL_STYLE",
    "SYSTEM_TEXT_STYLE",
    "TEXTUAL_THEME",
    "TEXTUAL_THEMES",
    "TEXT_BODY",
    "TEXT_DIM",
    "TEXT_DISABLED",
    "TEXT_EMPHASIS",
    "TEXT_MUTED",
    "TEXT_PLACEHOLDER",
    "TEXT_PRIMARY",
    "TEXT_SECONDARY",
    "TOOL_ACTIVE_STYLE",
    "TOOL_COMPLETE_STYLE",
    "TOOL_DETAIL_STYLE",
    "TOOL_GUIDE_STYLE",
    "TOOL_LABEL_STYLE",
    "TOOL_META_STYLE",
    "TOOL_TEXT_STYLE",
    "TOOL_TITLE_STYLE",
    "USER_TEXT_STYLE",
    "WAITING_STYLE",
    "WARNING",
    "loading_style",
    "markdown_theme",
    "syntax_theme",
    "theme_style",
]
