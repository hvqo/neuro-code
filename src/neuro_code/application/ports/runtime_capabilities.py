"""Immutable, credential-free inspection of the active runtime's web tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from neuro_code.application.ports.web_fetch import WebFetchExecutionPath
from neuro_code.application.ports.web_search import (
    WebSearchExecutionPath,
    WebSearchRouteKind,
)


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
class RuntimeSearchProviderOption:
    """Credential-free label for a provider proven executable for Search."""

    profile: str
    model: str

    def __post_init__(self) -> None:
        profile = _safe_display(self.profile, name="search profile")
        model = _safe_display(self.model, name="search model")
        if profile is None or model is None:
            raise ValueError("search provider option requires a profile and model")
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "model", model)


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
    search_route_kind: WebSearchRouteKind | None = None
    search_fallback: str | None = None
    fetch_path: WebFetchExecutionPath = WebFetchExecutionPath.DISABLED
    search_providers: tuple[RuntimeSearchProviderOption, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.search_availability, WebSearchAvailability):
            raise TypeError("search_availability must be canonical")
        if not isinstance(self.search_path, WebSearchExecutionPath):
            raise TypeError("search_path must be canonical")
        if not isinstance(self.fetch_path, WebFetchExecutionPath):
            raise TypeError("fetch_path must be canonical")
        if self.search_route_kind is not None and not isinstance(
            self.search_route_kind, WebSearchRouteKind
        ):
            raise TypeError("search_route_kind must be canonical or None")
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
        if self.search_route_kind is None:
            route_kind = {
                WebSearchExecutionPath.DISABLED: WebSearchRouteKind.DISABLED,
                WebSearchExecutionPath.INLINE_HOSTED: WebSearchRouteKind.NATIVE,
                WebSearchExecutionPath.SIDECAR_HOSTED: WebSearchRouteKind.NATIVE,
                WebSearchExecutionPath.SEARCH_API: WebSearchRouteKind.EXTERNAL_FALLBACK,
                WebSearchExecutionPath.UNAVAILABLE: WebSearchRouteKind.UNAVAILABLE,
            }[self.search_path]
            object.__setattr__(self, "search_route_kind", route_kind)
        object.__setattr__(
            self, "search_fallback", _safe_display(self.search_fallback, name="search_fallback")
        )
        providers = tuple(self.search_providers)
        if len(providers) > 64 or any(
            not isinstance(option, RuntimeSearchProviderOption) for option in providers
        ):
            raise ValueError("search provider options must be a bounded canonical tuple")
        if len({option.profile for option in providers}) != len(providers):
            raise ValueError("search provider profiles must be unique")
        object.__setattr__(self, "search_providers", providers)


__all__ = [
    "RuntimeSearchProviderOption",
    "RuntimeWebCapabilityInspection",
    "WebSearchAvailability",
    "WebSearchUnavailableReason",
]
