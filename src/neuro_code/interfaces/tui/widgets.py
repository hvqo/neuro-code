"""Textual widgets owned by the TUI interface.

TUI 界面拥有的 Textual 组件.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import ClassVar, Literal

from markdown_it.token import Token
from rich.console import Console, ConsoleOptions, JustifyMethod, RenderableType
from rich.console import RenderResult as RichRenderResult
from rich.markdown import CodeBlock, Heading, Markdown, MarkdownElement, Paragraph
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult, RenderResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Region, Size
from textual.message import Message as TextualMessage
from textual.strip import Strip
from textual.timer import Timer
from textual.visual import SupportsVisual, visualize
from textual.widget import Widget
from textual.widgets import Button, Input, Static, TextArea

from neuro_code.interfaces.tui.state import (
    _PROMPT_MARK,
    _PROMPT_MAX_VISIBLE_LINES,
    _SUCCESS_MARK,
)
from neuro_code.interfaces.tui.terminal_keyboard import (
    TerminalInputAction,
    TerminalInputNormalizer,
    TerminalKeyboardCapability,
)
from neuro_code.interfaces.tui.text_arrival import (
    ARRIVAL_META,
    FRAME_SECONDS,
    GLYPH_LIMIT,
    SOURCE_WINDOW,
    ArrivalTimeline,
    ink_style,
    paragraph_sources,
    tag_paragraph,
)
from neuro_code.interfaces.tui.theme import (
    ACCENT_CODE,
    ASSISTANT_TEXT_STYLE,
    TEXT_DISABLED,
    TEXT_PLACEHOLDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TOOL_COMPLETE_STYLE,
    theme_style,
)

# Content presentation cadence is independent of Measured Ink's 20fps clock.
# Fixed production budget; animation-off uses the same commit clock.
VIEW_COMMIT_SECONDS = 1 / 40


class WorkloadStatus(Static):
    """Repaint the existing fixed-height status slot without transcript reflow.

    Its one-row geometry remains TCSS-owned. Show/hide, resizing and stylesheet
    changes still use Textual's normal layout invalidation; content updates only
    invalidate the visual, with the same rendering as Static.update.

    状态文字只重绘既有单行槽位; 显隐、尺寸和样式变化仍由 Textual 处理布局.
    """

    def update(self, content: RenderableType | SupportsVisual = "") -> None:
        self._content = content
        self._visual = visualize(self, content)
        self.refresh()


class MenuOptionButton(Button):
    """Sparse modal row with independent focus and selected-state signals.

    使用独立焦点与已选择信号的克制模态列表行。
    """

    def __init__(
        self,
        primary: str,
        *,
        secondary: str = "",
        selected: bool = False,
        muted: bool = False,
        primary_width: int | None = None,
        secondary_justify: Literal["left", "right"] = "right",
        id: str | None = None,
        disabled: bool = False,
    ) -> None:
        accessible_label = " · ".join(part for part in (primary, secondary) if part)
        super().__init__(accessible_label, id=id, disabled=disabled)
        self._primary = primary
        self._secondary = secondary
        self._selected = selected
        self._muted = muted
        self._primary_width = primary_width
        self._secondary_justify = secondary_justify

    def render(self) -> RenderResult:
        primary_style = theme_style(
            self, TEXT_DISABLED if self.disabled or self._muted else TEXT_PRIMARY
        )
        secondary_style = theme_style(
            self, TEXT_DISABLED if self.disabled or self._muted else TEXT_SECONDARY
        )
        table = Table.grid(expand=True, padding=(0, 1))
        if self.has_focus and self.app.ansi_color:
            table.style = "reverse"
        table.add_column(width=1, no_wrap=True)
        if self._primary_width is None:
            table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        else:
            table.add_column(width=self._primary_width, overflow="ellipsis", no_wrap=True)
        table.add_column(
            ratio=1,
            justify=self._secondary_justify,
            overflow="ellipsis",
            no_wrap=True,
        )
        table.add_column(width=1, no_wrap=True)
        table.add_row(
            Text(_PROMPT_MARK if self.has_focus else " ", style=theme_style(self, ACCENT_CODE)),
            Text(self._primary, style=primary_style),
            Text(self._secondary, style=secondary_style),
            Text(
                _SUCCESS_MARK if self._selected else " ",
                style=theme_style(self, TOOL_COMPLETE_STYLE),
            ),
        )
        return table


class _ReadingHeading(Heading):
    """Keep document headings on the conversation reading axis."""

    LEVEL_ALIGN: ClassVar[dict[str, JustifyMethod]] = {**Heading.LEVEL_ALIGN, "h1": "left"}


class _FencedCodeBlock(CodeBlock):
    """Pass only the language identifier to Rich's existing lexer resolver."""

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> _FencedCodeBlock:
        language = re.split(r"[\s,]+", (token.info or "").strip(), maxsplit=1)[0]
        return cls(language.lower() or "text", markdown.code_theme)


class _ReadingParagraph(Paragraph):
    """Add one semantic block gap, never wrapped-line or nested-list spacing."""

    extra_gap: bool = False
    source: tuple[int, str] | None = None
    source_floor: int = 0

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> _ReadingParagraph:
        element = cls(markdown.justify or "left")
        element.extra_gap = isinstance(markdown, AssistantMarkdown) and markdown.has_paragraph_gap(
            token
        )
        if isinstance(markdown, AssistantMarkdown):
            element.source = markdown._arrival_sources.get(id(token))
            element.source_floor = markdown._arrival_floor
        return element

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RichRenderResult:
        if self.extra_gap:
            yield Segment.line()
        text = self.text.copy() if self.source is not None else self.text
        text.justify = self.justify
        tag_paragraph(text, self.source, self.source_floor)
        yield text


class _MarkdownBody:
    """Render the existing parser once, without recursing through its cache."""

    def __init__(self, markdown: AssistantMarkdown) -> None:
        self.markdown = markdown

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RichRenderResult:
        yield from super(AssistantMarkdown, self.markdown).__rich_console__(console, options)


class AssistantMarkdown(Markdown):
    """Safe model Markdown whose string form remains useful in diagnostics.

    安全的模型 Markdown,其字符串形式仍适合诊断."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": _ReadingHeading,
        "paragraph_open": _ReadingParagraph,
        "fence": _FencedCodeBlock,
        "code_block": _FencedCodeBlock,
    }

    def __init__(
        self,
        markup: str,
        code_theme: str = "monokai",
        justify: JustifyMethod | None = None,
        style: str | Style = "none",
        hyperlinks: bool = True,
        inline_code_lexer: str | None = None,
        inline_code_theme: str | None = None,
        *,
        compact: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            markup, code_theme, justify, style, hyperlinks, inline_code_lexer, inline_code_theme
        )
        self._compact = compact
        previous: Token | None = None
        gaps: set[int] = set()
        for token in self.parsed:
            if token.level != 0:
                continue
            if (
                token.type == "paragraph_open"
                and previous is not None
                and previous.type == "paragraph_close"
            ):
                gaps.add(id(token))
            previous = token
        self._paragraph_gaps = frozenset(gaps)
        self._arrival_sources: dict[int, tuple[int, str]] = {}
        self._arrival_floor = 0
        self._view_cached = False
        self._view_key: tuple[object, ...] | None = None
        self.cached_lines: list[list[Segment]] = []
        self.cached_sources: tuple[tuple[int, int], ...] = ()

    def cache_stream_view(self, *, animate: bool) -> None:
        self._view_cached = True
        self._arrival_floor = max(0, len(self.markup) - SOURCE_WINDOW)
        self._arrival_sources = paragraph_sources(self.markup, self.parsed) if animate else {}

    def invalidate_view(self) -> None:
        self._view_key = None
        self.cached_lines = []
        self.cached_sources = ()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RichRenderResult:
        if not self._view_cached:
            yield from super().__rich_console__(console, options)
            return
        # Content/revision is immutable per renderable. One cache entry, never a
        # history of replies. Width, compact policy and resolved theme/syntax
        # styles determine line layout and colors; motion age is NOT a key.
        key = (
            options.max_width,
            bool(self._compact and self._compact()),
            console.color_system,
            self.style,
            self.code_theme,
            tuple(console.get_style(name, default="none") for name in MARKDOWN_STYLE_KEYS),
        )
        if self._view_key != key:
            self.cached_lines = console.render_lines(
                _MarkdownBody(self), options.update(height=None), pad=False
            )
            self.cached_sources = tuple(
                sorted(
                    {
                        tuple(segment.style.meta[ARRIVAL_META])
                        for line in self.cached_lines
                        for segment in line
                        if segment.style and ARRIVAL_META in segment.style.meta
                    },
                    reverse=True,
                )
            )
            self._view_key = key
        for line in self.cached_lines:
            yield from line
            yield Segment.line()

    def has_paragraph_gap(self, token: Token) -> bool:
        """Resolve current shell policy at render time; no retained spacers."""
        return id(token) in self._paragraph_gaps and not (self._compact and self._compact())

    def __str__(self) -> str:
        return self.markup


MARKDOWN_STYLE_KEYS = (
    "markdown.paragraph",
    "markdown.h1",
    "markdown.h2",
    "markdown.h3",
    "markdown.h4",
    "markdown.h5",
    "markdown.h6",
    "markdown.code",
    "markdown.code_block",
    "markdown.link",
    "markdown.link_url",
    "markdown.strong",
    "markdown.em",
    "markdown.block_quote",
    "markdown.item",
    "markdown.bullet",
    "markdown.hr",
)


class AttachedTerminalPanel(Vertical):
    """Conversation-local viewport for one binding's attached terminals.

    The panel renders bounded terminal output as plain ``Text`` and sends
    input/focus intent back to its controller.  Terminal sessions and their
    lifecycle remain outside the widget.

    当前会话绑定的附加终端 viewport.输出始终以普通 Text 安全渲染,输入和焦点意图交给
    controller;终端会话及其生命周期不由 widget 持有.
    """

    class InputSubmitted(TextualMessage):
        def __init__(self, panel: AttachedTerminalPanel, value: str) -> None:
            self.panel = panel
            self.value = value
            super().__init__()

    class FocusChatRequested(TextualMessage):
        def __init__(self, panel: AttachedTerminalPanel) -> None:
            self.panel = panel
            super().__init__()

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static(id="attached-terminal-summary")
        yield Static(id="attached-terminal-output")
        yield Input(id="attached-terminal-input")
        yield Static(id="attached-terminal-help")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value
        event.input.value = ""
        self.post_message(self.InputSubmitted(self, value))

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.post_message(self.FocusChatRequested(self))

    def update_sessions(self, text: str) -> None:
        self.query_one("#attached-terminal-summary", Static).update(Text(text))

    def update_output(self, text: str) -> None:
        self.query_one("#attached-terminal-output", Static).update(Text(text))

    def update_help(self, text: str) -> None:
        self.query_one("#attached-terminal-help", Static).update(Text(text))

    def set_input_placeholder(self, text: str) -> None:
        self.query_one("#attached-terminal-input", Input).placeholder = text

    def focus_input(self) -> None:
        self.query_one("#attached-terminal-input", Input).focus()

    def clear_input(self) -> None:
        self.query_one("#attached-terminal-input", Input).value = ""

    def blur_input(self) -> None:
        self.query_one("#attached-terminal-input", Input).blur()


class TranscriptScroll(VerticalScroll):
    """Conversation scroll container with an idle-hidden scrollbar.

    The scrollbar remains a Textual-owned layout gutter while its visual
    widget is hidden.  Scrolling still uses the inherited scroll actions and
    the existing transcript follow logic.

    带有空闲自动隐藏滚动条的会话滚动容器. 滚动条始终保留 Textual 所有的布局槽位,
    只隐藏视觉组件; 滚动动作与现有会话跟随逻辑保持不变.
    """

    class ViewportChanged(TextualMessage):
        """Notify presentation overlays after conversation space changes."""

    def on_resize(self, event: events.Resize) -> None:
        if self._stream_follow_y is not None:
            self.follow_stream_growth()
        self.post_message(self.ViewportChanged())

    SCROLLBAR_HIDE_DELAY_SECONDS = 0.8

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
        can_focus: bool | None = None,
        can_focus_children: bool | None = None,
        can_maximize: bool | None = None,
    ) -> None:
        self._scrollbar_hide_timer: Timer | None = None
        self._scrollbar_visible = False
        self._stream_follow_paused = False
        self._stream_follow_pending = False
        self._stream_follow_anchor = 0.0
        self._stream_follow_y: float | None = None
        self._stream_follow_generation = 0
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
            can_focus=can_focus,
            can_focus_children=can_focus_children,
            can_maximize=can_maximize,
        )

    def on_unmount(self) -> None:
        self.cancel_stream_follow()
        timer = self._scrollbar_hide_timer
        self._scrollbar_hide_timer = None
        if timer is not None:
            timer.stop()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value != old_value and self.is_vertical_scroll_end and new_value >= old_value:
            self._stream_follow_paused = False
        elif new_value < old_value and not self.is_vertical_scroll_end:
            self.pause_stream_follow()
        if old_value != new_value:
            self._show_scrollbar_temporarily()

    def watch_virtual_size(self, old_value: Size, new_value: Size) -> None:
        if old_value != new_value and self._stream_follow_y is not None:
            self.follow_stream_growth()

    def cancel_stream_follow(self) -> None:
        self._stream_follow_generation += 1
        self._stream_follow_pending = False
        self._stream_follow_y = None
        self._stream_follow_paused = False

    def pause_stream_follow(self) -> None:
        self.cancel_stream_follow()
        self._stream_follow_paused = True

    def follow_stream_growth(self) -> None:
        """One post-layout follow, preserving intent across consecutive commits.

        An unfinished layout can temporarily make an end-following viewport look
        off-end. That is not user scroll intent. A real upward scroll cancels
        the pending follow; ordinary history browsing remains untouched.
        """
        if (
            self._stream_follow_paused
            or self.is_vertical_scrollbar_grabbed
            or self.scroll_target_y < self.scroll_y
            or self._stream_follow_pending
            or (not self.is_vertical_scroll_end and self.scroll_y != self._stream_follow_y)
        ):
            return
        self._stream_follow_pending = True
        self._stream_follow_anchor = self.scroll_y
        self._stream_follow_y = self.scroll_y
        generation = self._stream_follow_generation
        self.call_after_refresh(lambda: self._finish_stream_follow(generation))

    def _finish_stream_follow(self, generation: int) -> None:
        if generation != self._stream_follow_generation or not self._stream_follow_pending:
            return
        self._stream_follow_pending = False
        if not self.is_mounted:
            return
        if self.scroll_y < self._stream_follow_anchor and not self.is_vertical_scroll_end:
            return
        self.scroll_end(animate=False, immediate=True)
        self._stream_follow_y = self.scroll_y

    def _refresh_scrollbars(self) -> None:
        super()._refresh_scrollbars()
        if not self.show_vertical_scrollbar:
            self._scrollbar_visible = False
        if self._vertical_scrollbar is not None:
            self._vertical_scrollbar.display = (
                self._scrollbar_visible and self.show_vertical_scrollbar
            )

    def _show_scrollbar_temporarily(self) -> None:
        if not self.show_vertical_scrollbar:
            self._scrollbar_visible = False
            return
        self._scrollbar_visible = True
        self.vertical_scrollbar.display = True
        timer = self._scrollbar_hide_timer
        if timer is not None:
            timer.stop()
        self._scrollbar_hide_timer = self.set_timer(
            self.SCROLLBAR_HIDE_DELAY_SECONDS,
            self._hide_scrollbar,
            name="transcript-scrollbar-hide",
        )

    def _hide_scrollbar(self) -> None:
        self._scrollbar_hide_timer = None
        self._scrollbar_visible = False
        if self._vertical_scrollbar is not None:
            self._vertical_scrollbar.display = False

    def action_scroll_up(self) -> None:
        self.pause_stream_follow()
        self._show_scrollbar_temporarily()
        super().action_scroll_up()

    def action_scroll_down(self) -> None:
        self._show_scrollbar_temporarily()
        super().action_scroll_down()

    def action_page_up(self) -> None:
        self.pause_stream_follow()
        self._show_scrollbar_temporarily()
        super().action_page_up()

    def action_page_down(self) -> None:
        self._show_scrollbar_temporarily()
        super().action_page_down()


class ConversationMessage(Static):
    """One stable message node in the scrollable conversation.

    可滚动会话中的一个稳定消息节点."""

    def __init__(
        self,
        category: str,
        rendered: RenderableType,
        *,
        pending: bool = False,
    ) -> None:
        classes = f"conversation-message message-{category}"
        if pending:
            classes += " message-pending"
        super().__init__(rendered, markup=False, classes=classes)
        self.category = category

    def set_pending(self, pending: bool) -> None:
        self.set_class(pending, "message-pending")


class AssistantMessage(ConversationMessage):
    """Assistant Markdown with an explicit route to selectable source text.

    带有明确可选择原文入口的助手 Markdown.
    """

    class CopyRequested(TextualMessage):
        """Ask the owning app to show this reply in the selection view.

        请求所属应用在选择视图中显示此回复.
        """

        def __init__(self, message: AssistantMessage) -> None:
            self.message = message
            super().__init__()

    def __init__(
        self,
        rendered: RenderableType,
        *,
        content: str = "",
        pending: bool = False,
        copy_hint: str | None = None,
    ) -> None:
        super().__init__("assistant", rendered, pending=pending)
        self.content = content
        self.tooltip = copy_hint
        self._arrival = ArrivalTimeline()
        self._arrival_timer: Timer | None = None
        self._stream_view_timer: Timer | None = None
        self._stream_generation = 0
        self._stream_renderer: Callable[[str], AssistantMarkdown] | None = None
        self._stream_dirty = False
        self._stream_commit_at = -1.0
        self._arrival_enabled = False
        self._arrival_rows: set[int] = set()
        self._arrival_view_width: int | None = None

    def set_content(self, content: str) -> None:
        self.content = content

    def _motion_allowed(self) -> bool:
        return (
            not self.app.is_headless
            and not self.app.no_color
            and self.app.console.color_system in {"truecolor", "256"}
        )

    def stream_content(
        self, content: str, renderer: Callable[[str], AssistantMarkdown], *, animate: bool
    ) -> None:
        now = monotonic()
        previous = self.content
        appended = content.startswith(previous)
        enabled = animate and self._motion_allowed() and self.screen is self.app.screen
        if not appended or enabled != self._arrival_enabled:
            self.stop_arrival(flush=False)
        self.content = content  # Canonical source is always immediate and whole.
        self._stream_renderer = renderer
        self._arrival_enabled = enabled
        if enabled:
            self._arrival.receive(len(previous) if appended else 0, len(content), now)
        self._stream_dirty = True
        if self._stream_commit_at < 0 or now - self._stream_commit_at >= VIEW_COMMIT_SECONDS:
            self._cancel_view_timer()
            self._commit_stream_view(now)
        elif self._stream_view_timer is None:
            generation = self._stream_generation
            self._stream_view_timer = self.set_timer(
                max(0, self._stream_commit_at + VIEW_COMMIT_SECONDS - now),
                lambda: self._commit_pending_view(generation),
                name="stream-view-commit",
            )
        if self._arrival_timer is None and self._arrival.arrivals:
            self._arrival_timer = self.set_interval(
                FRAME_SECONDS, self._arrival_tick, name="measured-ink"
            )

    def _cancel_view_timer(self) -> None:
        self._stream_generation += 1
        timer, self._stream_view_timer = self._stream_view_timer, None
        if timer is not None:
            timer.stop()

    def _commit_pending_view(self, generation: int) -> None:
        if generation != self._stream_generation:
            return
        self._stream_view_timer = None
        if not self.is_mounted:
            self.stop_arrival(flush=False)
            return
        if self._stream_dirty:
            self._commit_stream_view(monotonic())

    def _commit_stream_view(self, now: float) -> None:
        if self._stream_renderer is None:
            return
        markdown = self._stream_renderer(self.content)
        canonical_body = self.app.console.get_style(theme_style(self, ASSISTANT_TEXT_STYLE))
        markdown.cache_stream_view(
            animate=self._arrival_enabled
            and self.app.console.get_style(markdown.style) == canonical_body
        )
        if isinstance(self.parent, TranscriptScroll):
            self.parent.follow_stream_growth()
        super().update(markdown)
        self._stream_dirty = False
        self._stream_commit_at = now
        self._arrival_rows.clear()
        # Preserve the existing transcript follow policy, including delayed view
        # commits. Never force a user who scrolled up back to the bottom.
        if (
            isinstance(self.parent, VerticalScroll)
            and not isinstance(self.parent, TranscriptScroll)
            and self.parent.is_vertical_scroll_end
        ):
            self.parent.scroll_end(animate=False)

    def _arrival_tick(self) -> None:
        if not self.is_mounted or not self.display or self.screen is not self.app.screen:
            self.stop_arrival()
            return
        now = monotonic()
        self._arrival.prune(now)
        for row in self._arrival_rows:
            self.refresh(Region(0, row, self.size.width, 1))
        if not self._arrival.arrivals:
            self._stop_motion()

    def _stop_motion(self) -> None:
        timer, self._arrival_timer = self._arrival_timer, None
        if timer is not None:
            timer.stop()
        self._arrival.clear()
        for row in self._arrival_rows:
            self.refresh(Region(0, row, self.size.width, 1))
        self._arrival_rows.clear()

    def stop_arrival(self, *, flush: bool = True) -> None:
        self._cancel_view_timer()
        self._stop_motion()
        if flush and self._stream_dirty:
            self._commit_stream_view(monotonic())
        self._stream_dirty = False

    def update(self, content: RenderableType | SupportsVisual = "") -> None:
        # Existing finalization, theme/syntax changes and restored replies use
        # this canonical path; they cannot inherit a streaming clock or range.
        self.stop_arrival(flush=False)
        self._stream_renderer = None
        self._stream_commit_at = -1.0
        super().update(content)

    def on_resize(self, event: events.Resize) -> None:
        previous, self._arrival_view_width = self._arrival_view_width, event.size.width
        # Content growth changes height during normal streaming; that is not a
        # viewport change and must not cancel each newly arrived line.
        if previous is not None and previous != event.size.width:
            self.invalidate_stream_view()

    def invalidate_stream_view(self) -> None:
        self.stop_arrival()
        if isinstance(self.renderable, AssistantMarkdown):
            self.renderable.invalidate_view()
            self.refresh(layout=True)

    def on_hide(self) -> None:
        self.stop_arrival()

    def on_unmount(self) -> None:
        self.stop_arrival(flush=False)
        self._stream_renderer = None

    def render_lines(self, crop: Region) -> list[Strip]:
        strips = super().render_lines(crop)
        if not self._arrival_enabled or not self._arrival.arrivals:
            return strips
        now = monotonic()
        markdown = self.renderable
        if not isinstance(markdown, AssistantMarkdown):
            return strips
        sources = [
            source
            for source in markdown.cached_sources
            if self._arrival.age(source, now) is not None
        ]
        ranks = {source: rank for rank, source in enumerate(sources)}
        primary, secondary = theme_style(self, TEXT_PRIMARY), theme_style(self, TEXT_SECONDARY)
        output: list[Strip] = []
        for y, strip in enumerate(strips, crop.y):
            segments: list[Segment] = []
            for segment in strip:
                style = segment.style
                if style and ARRIVAL_META in style.meta:
                    source = tuple(style.meta[ARRIVAL_META])
                    age = self._arrival.age(source, now)
                    if age is not None and source in ranks and ranks[source] < GLYPH_LIMIT:
                        self._arrival_rows.add(y)
                        style = ink_style(style, age, ranks[source], primary, secondary)
                        segment = Segment(segment.text, style, segment.control)
                segments.append(segment)
            output.append(Strip(segments, strip.cell_length))
        return output

    async def _on_click(self, event: events.Click) -> None:
        if event.chain < 2 or not self.content:
            return
        event.stop()
        self.post_message(self.CopyRequested(self))


class PromptInput(TextArea):
    """Bounded multi-line prompt editor with explicit submit semantics.

    带有明确提交语义且高度有界的多行提示编辑器.

    Terminal bracketed paste is preserved as real document lines. A shared
    input normalizer maps Enter to submit and verified modified-key reports to
    newline; the compatibility registry handles versioned terminal quirks.
    Common editor selection remains local to the prompt.

    终端 bracketed paste 会保留为真实文档行.``Enter`` 提交完整提示,
    输入归一化器将 Enter 映射为发送,将增强修饰键和已验证的终端兼容规则映射为换行.
    编辑选择保持在提示框内.
    """

    @dataclass
    class Submitted(TextualMessage):
        """Prompt submission carrying the complete multi-line value.

        携带完整多行内容的提示提交消息.
        """

        input: PromptInput
        value: str

        @property
        def control(self) -> PromptInput:
            return self.input

    @dataclass
    class ImagePasteRequested(TextualMessage):
        """The user asked to attach the clipboard image to the next message.

        用户请求把剪贴板图片附加到下一条消息.
        """

        input: PromptInput

        @property
        def control(self) -> PromptInput:
            return self.input

    BINDINGS: ClassVar[list[BindingType]] = [
        # Takes over TextArea's text paste: the app handler falls back to the
        # text paste when the system clipboard holds no image.
        #
        # 接管 TextArea 的文本粘贴:系统剪贴板没有图片时,应用处理器回落到文本粘贴.
        Binding("ctrl+v", "paste_image", "Paste", show=False),
    ]

    def __init__(
        self,
        *,
        placeholder: str = "",
        id: str | None = None,
        enter_behavior: str = "send",
        soft_wrap: bool = True,
        input_normalizer: TerminalInputNormalizer | None = None,
    ) -> None:
        super().__init__(soft_wrap=soft_wrap, tab_behavior="focus", id=id)
        self.keyboard_capability = TerminalKeyboardCapability()
        self.input_normalizer = input_normalizer or TerminalInputNormalizer.from_environment()
        self.enter_behavior = enter_behavior
        self.placeholder = placeholder

    @property
    def keyboard_help_key(self) -> str:
        compatibility_help_key = self.input_normalizer.compatibility_help_key
        if compatibility_help_key is not None:
            return compatibility_help_key
        if self.keyboard_capability.modified_enter_observed:
            return self.keyboard_capability.help_key
        return self.input_normalizer.help_key

    @property
    def value(self) -> str:
        """Compatibility alias used by the existing prompt lifecycle.

        供现有提示生命周期使用的兼容别名.
        """

        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value.replace("\r\n", "\n").replace("\r", "\n"))

    @property
    def cursor_position(self) -> int:
        row, column = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + column

    @cursor_position.setter
    def cursor_position(self, position: int) -> None:
        bounded = max(0, min(position, len(self.text)))
        prefix = self.text[:bounded]
        row = prefix.count("\n")
        column = len(prefix.rsplit("\n", maxsplit=1)[-1])
        self.move_cursor((row, column))

    def get_line(self, line_index: int) -> Text:
        if line_index == 0 and not self.text and self.placeholder:
            return Text(self.placeholder, style=theme_style(self, TEXT_PLACEHOLDER), end="")
        return super().get_line(line_index)

    def action_paste_image(self) -> None:
        """Ask the app to attach the system clipboard image.

        请求应用附加系统剪贴板图片."""

        self.post_message(self.ImagePasteRequested(self))

    async def _on_key(self, event: events.Key) -> None:
        self.keyboard_capability.observe(event.key)
        action = self.input_normalizer.normalize(event.key, event.character)
        if action is TerminalInputAction.NEWLINE:
            event.prevent_default().stop()
            self.insert_prompt_newline()
            return
        if action is TerminalInputAction.SEND:
            event.prevent_default().stop()
            if self.enter_behavior == "newline":
                self.insert_prompt_newline()
            elif not self.disabled and not self.read_only:
                self.post_message(self.Submitted(self, self.text))
            return
        # PASS_THROUGH intentionally reaches TextArea's standard key handling.
        if event.key == "ctrl+a":
            event.prevent_default().stop()
            self.action_select_all()
            return
        await super()._on_key(event)

    def insert_prompt_newline(self) -> None:
        """Insert at the selection without submitting. / 在选区插入换行,不提交。"""
        if self.disabled or self.read_only:
            return
        result = self.replace("\n", *self.selection, maintain_selection_offset=False)
        self.move_cursor(result.end_location)

    async def _on_paste(self, event: events.Paste) -> None:
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        if text:
            result = self.replace(text, *self.selection, maintain_selection_offset=False)
            self.move_cursor(result.end_location)
        event.prevent_default().stop()

    def sync_content_height(self) -> None:
        """Fit the draft while reserving most of a short terminal for reading.

        输入区按草稿增高,在矮终端保留主阅读区;超长草稿在输入区内滚动.
        """

        viewport_limit = max(2, self.screen.size.height // 4)
        visible_lines = max(
            1, min(self.wrapped_document.height, _PROMPT_MAX_VISIBLE_LINES, viewport_limit)
        )
        self.styles.height = visible_lines
        if self.parent is not None:
            self.parent.styles.height = visible_lines + self.parent.styles.gutter.height

    def _on_resize(self) -> None:
        super()._on_resize()
        self.call_after_refresh(self.sync_content_height)


class ToolFeedbackMessage(ConversationMessage, can_focus=True):
    """A stable Tool Activity card with a bounded selection viewport.

    带有有界选择 viewport 的稳定 Tool Activity 卡片."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "advance_disclosure", "Inspect", show=False),
        Binding("space", "toggle_peek", "Toggle peek", show=False),
        Binding("escape", "collapse_peek", "Summary", priority=True, show=False),
        Binding("up", "select_previous_tool", "Previous tool", show=False),
        Binding("down", "select_next_tool", "Next tool", show=False),
    ]

    class AdvanceRequested(TextualMessage):
        """Advance Summary to Peek, or Peek to Inspector."""

        def __init__(self, card: ToolFeedbackMessage) -> None:
            self.card = card
            super().__init__()

    class TogglePeekRequested(TextualMessage):
        """Toggle only the Conversation-local Summary/Peek state."""

        def __init__(self, card: ToolFeedbackMessage) -> None:
            self.card = card
            super().__init__()

    class CollapseRequested(TextualMessage):
        """Return a Peek viewport to its stable Summary."""

        def __init__(self, card: ToolFeedbackMessage) -> None:
            self.card = card
            super().__init__()

    class SelectionRequested(TextualMessage):
        """Move the selected tool within a multi-tool Peek viewport."""

        def __init__(self, card: ToolFeedbackMessage, delta: int) -> None:
            self.card = card
            self.delta = delta
            super().__init__()

    def __init__(self, rendered: RenderableType, *, entry_index: int) -> None:
        super().__init__("tool", rendered)
        self.entry_index = entry_index
        self.peek_active = False
        self.tool_count = 1

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        message = (
            self.TogglePeekRequested(self) if self.peek_active else self.AdvanceRequested(self)
        )
        self.post_message(message)

    def action_advance_disclosure(self) -> None:
        self.post_message(self.AdvanceRequested(self))

    def action_toggle_peek(self) -> None:
        self.post_message(self.TogglePeekRequested(self))

    def action_collapse_peek(self) -> None:
        if self.peek_active:
            self.post_message(self.CollapseRequested(self))

    def action_select_previous_tool(self) -> None:
        if self.peek_active and self.tool_count > 1:
            self.post_message(self.SelectionRequested(self, -1))

    def action_select_next_tool(self) -> None:
        if self.peek_active and self.tool_count > 1:
            self.post_message(self.SelectionRequested(self, 1))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        del parameters
        if action in {"select_previous_tool", "select_next_tool"}:
            return self.peek_active and self.tool_count > 1
        if action == "collapse_peek":
            return self.peek_active
        return True


__all__ = [
    "AssistantMarkdown",
    "AssistantMessage",
    "AttachedTerminalPanel",
    "ConversationMessage",
    "MenuOptionButton",
    "PromptInput",
    "ToolFeedbackMessage",
    "TranscriptScroll",
]
