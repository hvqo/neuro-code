"""Persistable interface appearance choices. / 可持久化的界面外观选项。"""

from enum import StrEnum


class UiTheme(StrEnum):
    """Persisted appearance identifiers. / 持久化的外观标识。"""

    PORCELAIN = "porcelain"
    GRAPHITE = "graphite"

    SYSTEM = "system"
    TOKYONIGHT = "tokyonight"
    EVERFOREST = "everforest"
    AYU = "ayu"
    CATPPUCCIN = "catppuccin"
    CATPPUCCIN_MACCHIATO = "catppuccin-macchiato"
    GRUVBOX = "gruvbox"
    KANAGAWA = "kanagawa"
    NORD = "nord"
    MATRIX = "matrix"
    ONE_DARK = "one-dark"

    @classmethod
    def from_textual_name(cls, name: str) -> "UiTheme":
        return cls.SYSTEM if name == "textual-ansi" else cls(name.removeprefix("neuro-code-"))

    @property
    def textual_name(self) -> str:
        return "textual-ansi" if self is UiTheme.SYSTEM else f"neuro-code-{self.value}"
