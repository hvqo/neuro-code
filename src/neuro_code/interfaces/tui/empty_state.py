"""Static, presentation-only empty-conversation identity.

Only the corrected-source core mark is used; no hexagonal border is retained.
No original image, terminal probe, timer or session data is read at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text
from textual.geometry import Offset, Region
from textual.widgets import Static

from neuro_code.interfaces.tui.empty_state_logo import LOGO_ROWS

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

    DEFAULT_CSS = """
    EmptyStateIdentity {
        overlay: screen;
        position: absolute;
        background: $background;
        color: $text-dim;
        text-style: dim;
        padding: 0;
        margin: 0;
        border: none;
        display: none;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="empty-state-identity", markup=False)
        self.content_seen = False
        self.asset_size: str | None = None
        self.rows: Mapping[str, tuple[str, ...]] | None = LOGO_ROWS

    def consume(self) -> None:
        self.content_seen = True
        self.display = False

    def reset(self) -> None:
        self.content_seen = False
        self.display = False

    def arrange(self, viewport: Region, terminal: tuple[int, int]) -> None:
        placement = logo_layout(viewport, terminal)
        self.asset_size = None
        self.display = not self.content_seen and self.rows is not None and placement is not None
        if not self.display or placement is None or self.rows is None:
            return
        size, region = placement
        self.asset_size = size
        self.styles.width, self.styles.height = region.width, region.height
        self.absolute_offset = Offset(region.x, region.y)
        self.update(Text("\n".join(self.rows[size])))
