"""Presentation-only identity with a bounded, user-initiated reveal.

Only the corrected-source core mark is used; no hexagonal border is retained.
No original image, terminal probe or session data is read at runtime.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from itertools import groupby
from time import monotonic
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import RenderResult
from textual.color import Color
from textual.geometry import Offset, Region
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Static

from neuro_code.interfaces.tui.empty_state_logo import LOGO_ROWS
from neuro_code.interfaces.tui.empty_state_reveal import FRAME_DEADLINES, VORTEX_FRAMES
from neuro_code.interfaces.tui.terminal_palette import TerminalColorLevel, TerminalPalette

# Core-only artwork stays within the accepted empty-state geometry.
LOGO_SIZES = {"large": (32, 16), "medium": (24, 12), "small": (16, 8)}


def logo_layout(viewport: Region, terminal: tuple[int, int]) -> tuple[str, Region] | None:
    """Center inside conversation space; hide rather than squeeze the shell."""
    width, height = terminal
    size = (
        "large"
        if width >= 120 and height >= 40
        else ("medium" if width >= 100 and height >= 32 else "small")
    )
    columns, lines = LOGO_SIZES[size]
    if width < 70 or height < 22 or viewport.width < columns + 8 or viewport.height < lines + 4:
        return None
    return size, Region(
        viewport.x + (viewport.width - columns) // 2,
        viewport.y + (viewport.height - lines) // 2,
        columns,
        lines,
    )


class EmptyStateIdentity(Static):
    """Non-layout overlay, latched off until a new transcript is bound."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"empty-state--muted", "empty-state--secondary"}

    class FocusComposer(Message):
        """A logo click keeps input ownership with the existing Composer."""

    DEFAULT_CSS = """
    EmptyStateIdentity {
        overlay: screen;
        position: absolute;
        background: $background;
        color: $text-dim 28%;
        text-style: dim;
        padding: 0;
        margin: 0;
        border: none;
        display: none;
    }
    EmptyStateIdentity > .empty-state--muted { color: $text-muted; }
    EmptyStateIdentity > .empty-state--secondary { color: $text-secondary; }
    """

    def __init__(self) -> None:
        super().__init__(id="empty-state-identity", markup=False)
        self.content_seen = False
        self.asset_size: str | None = None
        self.rows: Mapping[str, tuple[str, ...]] | None = LOGO_ROWS
        self._frame_index: int | None = None
        self._timer: Timer | None = None
        self._started_at = 0.0
        self._animation_generation = 0
        self._frame_styles: tuple[Style, ...] | None = None

    @property
    def animation_running(self) -> bool:
        return self._frame_index is not None

    def _motion_allowed(self) -> bool:
        palette = getattr(self.app, "terminal_palette", TerminalPalette())
        return (
            not self.app.is_headless
            and not self.app.no_color
            and (
                self.app.console.color_system in {"truecolor", "256"}
                or palette.color_level in {TerminalColorLevel.TRUECOLOR, TerminalColorLevel.ANSI256}
            )
        )

    def on_click(self, event: events.Click) -> None:
        if event.button != 1:
            return
        event.stop()
        self.post_message(self.FocusComposer())
        self.activate()

    def activate(self) -> bool:
        """Ignore repeats; ordinary appearance and fallback never schedule motion."""
        if (
            self.animation_running
            or not self.display
            or self.content_seen
            or self.asset_size is None
            or self.rows is not LOGO_ROWS
            or not self._motion_allowed()
        ):
            return False
        self._animation_generation += 1
        self._started_at = monotonic()
        self._frame_index = 0
        self._advance_animation(self._animation_generation)
        return True

    def _advance_animation(self, generation: int) -> None:
        # Stopped timers can already have queued a callback. Generation fencing
        # prevents it from advancing a later activation.
        if generation != self._animation_generation or not self.animation_running:
            return
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if not self.display or not self.is_attached or self.screen is not self.app.screen:
            self.cancel_animation()
            return
        elapsed_ms = (monotonic() - self._started_at) * 1000
        index = bisect_right(FRAME_DEADLINES, elapsed_ms)
        if index >= len(VORTEX_FRAMES) - 1:
            self.cancel_animation()
            return
        if self._frame_index != index:
            self._frame_index = index
            self.refresh(layout=False)
        self._timer = self.set_timer(
            max(0.001, (FRAME_DEADLINES[index] - elapsed_ms) / 1000),
            lambda: self._advance_animation(generation),
            name="empty-state-vortex",
        )

    def cancel_animation(self) -> None:
        """Release the one-shot timer and return to the canonical static render."""
        self._animation_generation += 1
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        was_running = self.animation_running
        self._frame_index = None
        self._frame_styles = None
        if was_running:
            self.refresh(layout=False)

    def on_hide(self) -> None:
        self.cancel_animation()

    def on_unmount(self) -> None:
        self.cancel_animation()

    def render(self) -> RenderResult:
        if self._frame_index is None or self.asset_size is None:
            return super().render()
        frame = VORTEX_FRAMES[self._frame_index]
        if self._frame_index == 0:
            return super().render()
        if self._frame_styles is None:
            self._frame_styles = self._resolve_frame_styles()
        text = Text()
        for row_index, (row, levels) in enumerate(
            zip(frame.rows[self.asset_size], frame.levels[self.asset_size], strict=True)
        ):
            if row_index:
                text.append("\n")
            for level, cells in groupby(zip(row, levels, strict=True), key=lambda item: item[1]):
                text.append("".join(char for char, _ in cells), self._frame_styles[int(level, 16)])
        return text

    def _resolve_frame_styles(self) -> tuple[Style, ...]:
        """Neutral brightness follows the theme, never the reference's RGB palette."""
        resting = self.visual_style.rich_style
        peak = self.get_component_rich_style("empty-state--secondary", partial=True)
        foreground, background = resting.color, self.visual_style.background
        if (
            foreground is None
            or foreground.is_default
            or peak.color is None
            or peak.color.is_default
            or background.ansi is not None
        ):
            return (resting, *(peak + Style(dim=level < 8) for level in range(1, 16)))
        # Rich's deterministic dim representation mixes 40% toward background.
        # Interpolate existing semantic colors, without a logo-specific palette.
        start = Color.from_rich_color(foreground).blend(background, 0.4)
        target = Color.from_rich_color(peak.color)
        return (
            resting,
            *(
                Style(color=start.blend(target, level / 15).rich_color, dim=False)
                for level in range(1, 16)
            ),
        )

    def consume(self) -> None:
        self.cancel_animation()
        self.content_seen = True
        self.display = False

    def reset(self) -> None:
        self.cancel_animation()
        self.content_seen = False
        self.display = False

    def arrange(self, viewport: Region, terminal: tuple[int, int]) -> None:
        placement = logo_layout(viewport, terminal)
        size = placement[0] if placement is not None else None
        if self.asset_size != size or self.content_seen or self.rows is None:
            self.cancel_animation()
        previous_size = self.asset_size
        self.asset_size = size
        self.display = not self.content_seen and self.rows is not None and placement is not None
        if not self.display or placement is None or self.rows is None:
            self.cancel_animation()
            return
        size, region = placement
        self.asset_size = size
        self.styles.width, self.styles.height = region.width, region.height
        self.absolute_offset = Offset(region.x, region.y)
        if previous_size != size or not self.animation_running:
            self.update(Text("\n".join(self.rows[size])))
