"""Independent syntax selection and representative live code preview."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select, Static

from neuro_code.interfaces.tui.syntax import SYNTAX_THEMES, ResolvedSyntaxTheme
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import syntax_theme
from neuro_code.shared.syntax_theme import SyntaxTheme
from neuro_code.shared.ui_language import UiLanguage

SYNTAX_PREVIEW = """from dataclasses import dataclass

# 代码审查 / repository review
@dataclass
class Review:
    path: str = "src/app.py"

    def score(self, count: int = 42) -> float:
        return count / 2.0 if self.path else 0.0
"""


class SyntaxThemeSettingsScreen(ModalScreen[SyntaxTheme | None]):
    """Preview changes immediately; Save commits and Cancel restores the choice."""

    CSS = """
    SyntaxThemeSettingsScreen { align: center middle; background: $modal-overlay 25%; }
    #syntax-settings-dialog {
        width: 88%; max-width: 84; height: 90%; max-height: 36;
        padding: 1 2; border: round $border; background: $surface;
    }
    #syntax-settings-title { color: $text-primary; margin-bottom: 1; }
    #syntax-settings-description, #syntax-resolved { color: $text-muted; height: auto; }
    #syntax-settings-content { height: 1fr; scrollbar-size-vertical: 1; }
    #syntax-choice { margin: 1 0; }
    #syntax-preview { height: auto; margin-top: 1; }
    #syntax-settings-actions { height: auto; align-horizontal: right; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        selected: SyntaxTheme,
        *,
        language: UiLanguage,
        preview: Callable[[SyntaxTheme], None],
    ) -> None:
        super().__init__()
        self.selected = selected
        self.language = language
        self.preview = preview

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(ui_text(self.language, "settings.syntax.title"), id="syntax-settings-title"),
            VerticalScroll(
                Static(
                    ui_text(self.language, "settings.syntax.description"),
                    id="syntax-settings-description",
                ),
                Select(
                    [
                        (
                            ui_text(self.language, "settings.syntax.auto")
                            if choice is SyntaxTheme.AUTO
                            else definition.label,
                            choice.value,
                        )
                        for choice, definition in SYNTAX_THEMES.items()
                    ],
                    value=self.selected.value,
                    allow_blank=False,
                    id="syntax-choice",
                ),
                Static(id="syntax-resolved"),
                Static(id="syntax-preview"),
                id="syntax-settings-content",
            ),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="syntax-settings-cancel"),
                Button(ui_text(self.language, "settings.extra.save"), id="syntax-settings-save"),
                id="syntax-settings-actions",
            ),
            id="syntax-settings-dialog",
            classes="modal-dialog modal-m",
        )

    def on_mount(self) -> None:
        self._refresh_preview()
        self.query_one("#syntax-choice", Select).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "syntax-choice" and isinstance(event.value, str):
            self.selected = SyntaxTheme(event.value)
            self.preview(self.selected)
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        theme = syntax_theme(self)
        assert isinstance(theme, ResolvedSyntaxTheme)
        key = "settings.syntax.terminal" if theme.terminal_fallback else "settings.syntax.resolved"
        self.query_one("#syntax-resolved", Static).update(
            ui_text(self.language, key, theme=SYNTAX_THEMES[theme.resolved_choice].label)
        )
        self.query_one("#syntax-preview", Static).update(
            Syntax(SYNTAX_PREVIEW, "python", theme=theme, word_wrap=True, padding=1)
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "syntax-settings-save":
            self.dismiss(self.selected)
        elif event.button.id == "syntax-settings-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
