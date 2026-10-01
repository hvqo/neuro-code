"""Corrected-source A/B/C artwork preview on the real empty-state lifecycle.

This module retains preview candidates only; production selects the core mark.
Activation here is a static
presentation preview, not a timer, keyboard binding or runtime behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text
from tests.visual.showcases import _VisualFixtureRunner
from textual.app import ComposeResult
from textual.geometry import Region

from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.empty_state import EmptyStateIdentity
from neuro_code.interfaces.tui.theme import TEXT_DIM, theme_style
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.shared.ui_theme import UiTheme

ASSETS = json.loads(Path(__file__).with_name("representations.json").read_text(encoding="utf-8"))
VARIANTS = ("A", "B", "C")
THEMES = (UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM)
VIEWPORTS = ((120, 40), (100, 32), (80, 24))


class PreviewIdentity(EmptyStateIdentity):
    """Supply reviewed-source candidates without choosing a production asset."""

    def __init__(self, variant: str) -> None:
        super().__init__()
        self.variant = variant
        self.rows = {
            size: tuple(content[variant]["core"]) for size, content in ASSETS["sizes"].items()
        }

    def arrange(self, viewport: Region, terminal: tuple[int, int]) -> None:
        super().arrange(viewport, terminal)
        if not self.display or self.asset_size is None:
            return
        layers = ASSETS["sizes"][self.asset_size][self.variant]
        text = Text()
        outline_style = theme_style(self.app, TEXT_DIM) + " dim"
        for row_index, (core, outline) in enumerate(
            zip(layers["core"], layers["outline"], strict=True)
        ):
            if row_index:
                text.append("\n")
            for core_cell, outline_cell in zip(core, outline, strict=True):
                if core_cell != " ":
                    text.append(core_cell)
                else:
                    text.append(outline_cell, style=outline_style)
        self.update(text)


class IdentityExplorationApp(NeuroCodeApp):
    CSS = (
        NeuroCodeApp.CSS
        + """
        EmptyStateIdentity.activated-preview {
            color: $text-secondary;
            text-style: none;
        }
    """
    )

    def __init__(self, variant: str, theme: UiTheme) -> None:
        if variant not in VARIANTS:
            raise ValueError("Unknown corrected-source variant")
        self.variant = variant
        self.identity_theme = theme
        super().__init__(
            _VisualFixtureRunner(),  # type: ignore[arg-type]
            language=UiLanguage.ENGLISH,
            ui_theme=theme,
            provider_name="fixture-provider",
            model_name="fixture-model",
            cwd=Path("/workspace/neuro-code"),
        )

    def compose(self) -> ComposeResult:
        # Replace only artwork; all layout, history and resize handlers are real.
        for widget in super().compose():
            yield (
                PreviewIdentity(self.variant) if isinstance(widget, EmptyStateIdentity) else widget
            )

    def set_peak_preview(self, enabled: bool) -> None:
        self.query_one(EmptyStateIdentity).set_class(enabled, "activated-preview")

    def geometry(self) -> dict[str, object]:
        identity = self.query_one(EmptyStateIdentity)
        transcript = self.query_one("#transcript")
        return {
            "source_sha256": ASSETS["source_sha256"],
            "variant": self.variant,
            "theme": self.identity_theme.value,
            "viewport": list(self.size),
            "asset_size": identity.asset_size,
            "logo_visible": identity.display,
            "logo": list(identity.region),
            "conversation_content": list(transcript.content_region),
            "composer": list(self.query_one("#prompt-surface").region),
            "footer": list(self.query_one("#runtime-bar").region),
            "scroll_max_y": transcript.max_scroll_y,
            "transcript_entries": len(self._entries),
        }
