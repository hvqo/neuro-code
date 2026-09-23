"""Immutable, credential-free inspection of the active runtime's web tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from neuro_code.application.ports.web_fetch import WebFetchExecutionPath
from neuro_code.application.ports.web_search import WebSearchExecutionPath


class WebSearchAvailability(StrEnum):
    DISABLED = "disabled"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class WebSearchUnavailableReason(StrEnum):
    NO_COMPATIBLE_PROVIDER = "no_compatible_search_provider"
    CONFIGURED_ROUTE_UNAVAILABLE = "configured_search_route_unavailable"
    MAIN_CAPABILITY_UNSUPPORTED = "main_search_capability_unsupported"
    TOOL_NOT_ALLOWED = "search_tool_not_allowed"


def _safe_display(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text or None")
    safe = "".join(
        character for character in value if ord(character) >= 32 and ord(character) != 127
    )
    safe = " ".join(safe.split())[:128]
    return safe or None


@dataclass(frozen=True, slots=True)
class RuntimeWebCapabilityInspection:
    """Effective, safe web-tool status after composition and tool filtering.

    Provider profile and model labels are included for operator context. Endpoint,
    credentials, provider errors, and configuration source values are omitted.
    """

    search_availability: WebSearchAvailability
    search_path: WebSearchExecutionPath
    search_reason: WebSearchUnavailableReason | None = None
    search_profile: str | None = None
    search_model: str | None = None
    fetch_path: WebFetchExecutionPath = WebFetchExecutionPath.DISABLED

    def __post_init__(self) -> None:
        if not isinstance(self.search_availability, WebSearchAvailability):
            raise TypeError("search_availability must be canonical")
        if not isinstance(self.search_path, WebSearchExecutionPath):
            raise TypeError("search_path must be canonical")
        if not isinstance(self.fetch_path, WebFetchExecutionPath):
            raise TypeError("fetch_path must be canonical")
        if self.search_reason is not None and not isinstance(
            self.search_reason, WebSearchUnavailableReason
        ):
            raise TypeError("search_reason must be canonical or None")
        if self.search_availability is WebSearchAvailability.DISABLED:
            if (
                self.search_path is not WebSearchExecutionPath.DISABLED
                or self.search_reason is not None
            ):
                raise ValueError("disabled search must use the disabled path without a reason")
        elif self.search_availability is WebSearchAvailability.AVAILABLE:
            if (
                self.search_path
                in {
                    WebSearchExecutionPath.DISABLED,
                    WebSearchExecutionPath.UNAVAILABLE,
                }
                or self.search_reason is not None
            ):
                raise ValueError("available search requires an executable path without a reason")
        elif (
            self.search_path is not WebSearchExecutionPath.UNAVAILABLE or self.search_reason is None
        ):
            raise ValueError("unavailable search requires an unavailable path and reason")
        object.__setattr__(
            self, "search_profile", _safe_display(self.search_profile, name="search_profile")
        )
        object.__setattr__(
            self, "search_model", _safe_display(self.search_model, name="search_model")
        )


__all__ = [
    "RuntimeWebCapabilityInspection",
    "WebSearchAvailability",
    "WebSearchUnavailableReason",
]
