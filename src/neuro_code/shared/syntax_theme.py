"""Persistable syntax choices, independent of interface palettes."""

from enum import StrEnum


class SyntaxTheme(StrEnum):
    """Stable preference identifiers; Auto is the backward-compatible default."""

    AUTO = "auto"
    GITHUB_DARK = "github-dark"
    ONE_DARK = "one-dark"
    MONOKAI = "monokai"
    DRACULA = "dracula"
    FRIENDLY = "friendly"
    SOLARIZED_LIGHT = "solarized-light"
