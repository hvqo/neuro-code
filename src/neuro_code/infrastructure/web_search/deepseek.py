"""DeepSeek's official Anthropic-compatible server-side Web Search adapter.

The adapter deliberately targets one fixed official endpoint. A DeepSeek API
key configured for a custom base URL or compatibility gateway is never sent to
that endpoint.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from neuro_code.application.ports.configuration import ProviderProfile
from neuro_code.application.ports.model import ModelCapability, ModelCapabilitySet
from neuro_code.application.ports.web_search import (
    MAX_EVIDENCE_CHARS,
    MAX_SOURCE_COUNT,
    MAX_SOURCE_SNIPPET_CHARS,
    MAX_SOURCE_TITLE_CHARS,
    MAX_SOURCE_URL_CHARS,
    HostedWebSearchEventSink,
    WebSearchCitation,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
    WebSearchResult,
    WebSearchRouteKind,
    WebSearchRouteName,
    WebSearchSource,
)
from neuro_code.infrastructure.providers.binding import resolve_provider_binding
from neuro_code.shared.errors import ConfigurationError

_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
_OFFICIAL_HOST = "api.deepseek.com"
_OFFICIAL_BASE_PATHS = frozenset({"", "/", "/v1", "/v1/", "/anthropic", "/anthropic/"})
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_API_KEY_CHARS = 16_384
_TIMEOUT_SECONDS = 20.0
_MAX_OUTPUT_TOKENS = 4_096
_API_VERSION = "2023-06-01"
_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}


def is_official_deepseek_profile(profile: ProviderProfile) -> bool:
    """Check whether a profile credential is explicitly bound to DeepSeek's origin.

    Provider catalogs can label DeepSeek's OpenAI-compatible chat route with a
    generic ``openai`` service ID.  The exact official HTTPS origin, rather
    than that catalog label or the independent Anthropic Search path, owns the
    credential-containment decision.
    """

    if profile.protocol not in {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
    }:
        return False
    if profile.auth == "proxy-managed":
        return False
    try:
        parsed = urlsplit(profile.base_url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == _OFFICIAL_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in _OFFICIAL_BASE_PATHS
    )


def _host(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname
        if hostname is None:
            return None
        return hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except (UnicodeError, ValueError):
        return None


def _source_allowed(url: str, request: WebSearchRequest) -> bool:
    host = _host(url)
    if host is None or len(url) > MAX_SOURCE_URL_CHARS:
        return False
    if request.allowed_domains and not any(
        host == domain or host.endswith(f".{domain}") for domain in request.allowed_domains
    ):
        return False
    return not any(
        host == domain or host.endswith(f".{domain}") for domain in request.blocked_domains
    )


def _search_prompt(request: WebSearchRequest) -> str:
    """Build the bounded explicit search instruction expected by DeepSeek Search."""

    lines = [f"Perform a web search for the query: {request.query}"]
    if request.allowed_domains:
        lines.append(
            "Only include sources from these domains: " + ", ".join(request.allowed_domains)
        )
    if request.blocked_domains:
        lines.append("Exclude sources from these domains: " + ", ".join(request.blocked_domains))
    return "\n".join(lines)


def _safe_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _error_for_status(status: int) -> WebSearchError:
    if status in {401, 403}:
        return WebSearchError(
            WebSearchErrorCode.SEARCH_AUTHENTICATION,
            "DeepSeek Search authentication failed",
            http_status=status,
        )
    if status == 429:
        return WebSearchError(
            WebSearchErrorCode.SEARCH_RATE_LIMIT,
            "DeepSeek Search rate limit reached",
            http_status=status,
        )
    if status in {400, 404, 405, 415, 422}:
        return WebSearchError(
            WebSearchErrorCode.SEARCH_UNSUPPORTED,
            "DeepSeek Search endpoint does not support this request",
            http_status=status,
        )
    if 300 <= status < 400:
        return WebSearchError(
            WebSearchErrorCode.SEARCH_UNAVAILABLE,
            "DeepSeek Search endpoint returned an unexpected redirect",
            http_status=status,
        )
    if status >= 500 or status in {408, 425}:
        return WebSearchError(
            WebSearchErrorCode.SEARCH_UNAVAILABLE,
            "DeepSeek Search endpoint is unavailable",
            http_status=status,
        )
    return WebSearchError(
        WebSearchErrorCode.SEARCH_PROVIDER_ERROR,
        f"DeepSeek Search returned HTTP {status}",
        http_status=status,
    )


class DeepSeekSearchAdapter:
    """Execute DeepSeek Web Search through its fixed Anthropic-compatible API."""

    provider_profile = "DeepSeek Search"
    route_kind = WebSearchRouteKind.PROVIDER_ADAPTER
    diagnostic_route = WebSearchRouteName.DEEPSEEK
    capabilities = ModelCapabilitySet.from_supported(ModelCapability.HOSTED_WEB_SEARCH)

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not is_official_deepseek_profile(profile):
            raise ValueError("DeepSeek Search requires the official DeepSeek provider route")
        if not profile.available:
            raise ValueError("DeepSeek Search requires an available provider profile")
        self._profile = profile
        self._environ = environ
        self._transport = transport

    @property
    def model(self) -> str:
        return self._profile.model

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        del event_sink
        try:
            binding = resolve_provider_binding(self._profile, environ=self._environ)
            api_key = binding.api_key
        except (ConfigurationError, ValueError, RuntimeError) as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_AUTHENTICATION,
                "DeepSeek Search credential or transport configuration is unavailable",
            ) from error
        if (
            not api_key
            or len(api_key) > _MAX_API_KEY_CHARS
            or not api_key.isascii()
            or any(ord(character) < 33 or ord(character) == 127 for character in api_key)
        ):
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_AUTHENTICATION,
                "DeepSeek Search credential is invalid",
            )

        body = bytearray()
        http_status: int | None = None
        payload = {
            "model": self._profile.model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": _search_prompt(request)}],
                }
            ],
            "tools": [_SEARCH_TOOL],
        }
        options = binding.http_policy.client_options(
            timeout=httpx.Timeout(_TIMEOUT_SECONDS, connect=5.0),
            transport=self._transport,
        )
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS + 1):
                async with httpx.AsyncClient(
                    **options,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        _ENDPOINT,
                        headers={
                            "x-api-key": api_key,
                            "anthropic-version": _API_VERSION,
                            "content-type": "application/json",
                        },
                        json=payload,
                    ) as response:
                        if not 200 <= response.status_code < 300:
                            raise _error_for_status(response.status_code)
                        http_status = response.status_code
                        async for chunk in response.aiter_bytes(chunk_size=16_384):
                            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                                raise WebSearchError(
                                    WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
                                    "DeepSeek Search response exceeded the size limit",
                                )
                            body.extend(chunk)
        except asyncio.CancelledError:
            raise
        except WebSearchError:
            raise
        except (TimeoutError, httpx.TimeoutException) as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                "DeepSeek Search endpoint timed out",
            ) from error
        except (httpx.HTTPError, OSError) as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                "DeepSeek Search endpoint could not be reached",
            ) from error

        try:
            decoded = json.loads(body)
        except (UnicodeError, ValueError) as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
                "DeepSeek Search returned malformed JSON",
            ) from error
        if not isinstance(decoded, Mapping) or not isinstance(decoded.get("content"), list):
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
                "DeepSeek Search returned an invalid response shape",
            )
        return _project_result(
            decoded,
            request=request,
            model=self._profile.model,
            http_status=http_status,
        )


def _project_result(
    payload: Mapping[str, object],
    *,
    request: WebSearchRequest,
    model: str,
    http_status: int | None,
) -> WebSearchResult:
    content = payload.get("content")
    assert isinstance(content, list)
    did_search = False
    evidence: list[str] = []
    sources: list[WebSearchSource] = []
    citations: list[WebSearchCitation] = []
    source_urls: set[str] = set()

    def add_source(raw: Mapping[str, object]) -> None:
        url = raw.get("url")
        if not isinstance(url, str) or not _source_allowed(url, request):
            return
        title = (
            _safe_text(raw.get("title"), limit=MAX_SOURCE_TITLE_CHARS) or _host(url) or "Web source"
        )
        snippet = _safe_text(
            raw.get("snippet") or raw.get("description") or raw.get("cited_text"),
            limit=MAX_SOURCE_SNIPPET_CHARS,
        )
        key = url.casefold()
        if key in source_urls or len(sources) >= min(request.max_sources, MAX_SOURCE_COUNT):
            return
        try:
            sources.append(
                WebSearchSource(
                    url=url,
                    title=title,
                    provider="DeepSeek Search",
                    snippet=snippet or None,
                )
            )
        except (TypeError, ValueError):
            return
        source_urls.add(key)

    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text":
            text = _safe_text(block.get("text"), limit=MAX_EVIDENCE_CHARS)
            if text:
                evidence.append(text)
            raw_citations = block.get("citations")
            if isinstance(raw_citations, list):
                for raw in raw_citations[:MAX_SOURCE_COUNT]:
                    if not isinstance(raw, Mapping):
                        continue
                    url = raw.get("url")
                    if not isinstance(url, str) or not _source_allowed(url, request):
                        continue
                    add_source(raw)
                    cited = _safe_text(raw.get("cited_text"), limit=4_000)
                    if not cited:
                        continue
                    try:
                        citations.append(
                            WebSearchCitation(
                                url=url,
                                title=(
                                    _safe_text(raw.get("title"), limit=MAX_SOURCE_TITLE_CHARS)
                                    or _host(url)
                                    or "Web source"
                                ),
                                cited_text=cited,
                            )
                        )
                    except (TypeError, ValueError):
                        continue
        elif block.get("type") == "web_search_tool_result":
            did_search = True
            raw_results = block.get("content")
            if isinstance(raw_results, list):
                for raw in raw_results[:MAX_SOURCE_COUNT]:
                    if isinstance(raw, Mapping) and raw.get("type") == "web_search_result":
                        add_source(raw)

    if not did_search:
        raise WebSearchError(
            WebSearchErrorCode.SEARCH_PROVIDER_DID_NOT_SEARCH,
            "DeepSeek Search returned no completed search result",
        )
    return WebSearchResult(
        query=request.query,
        evidence_text="\n".join(evidence)[:MAX_EVIDENCE_CHARS],
        sources=tuple(sources),
        citations=tuple(citations),
        provider_profile="DeepSeek Search",
        model=model,
        truncated=len(sources) >= request.max_sources,
        metadata={"auxiliary": True, "source_count": len(sources)},
        http_status=http_status,
    )


__all__ = ["DeepSeekSearchAdapter", "is_official_deepseek_profile"]
