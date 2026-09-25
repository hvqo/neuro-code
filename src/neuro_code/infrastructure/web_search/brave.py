"""Bounded Brave Search API adapter for model-independent Web Search."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from html import unescape
from urllib.parse import urlsplit

import httpx

from neuro_code.application.ports.model import ModelCapability, ModelCapabilitySet
from neuro_code.application.ports.web_search import (
    MAX_SOURCE_SNIPPET_CHARS,
    MAX_SOURCE_TITLE_CHARS,
    MAX_SOURCE_URL_CHARS,
    HostedWebSearchEventSink,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
    WebSearchResult,
    WebSearchRouteKind,
    WebSearchRouteName,
    WebSearchSource,
)

BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_QUERY_CHARS = 600
_MAX_QUERY_WORDS = 75
_MAX_API_KEY_CHARS = 16_384
_MAX_ERROR_RESPONSE_BYTES = 8_192
_MAX_PROVIDER_ERROR_DETAIL_CHARS = 240
_TIMEOUT_SECONDS = 15.0


def _matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _display_text(value: str, maximum: int) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in unescape(value)
    )
    return " ".join(printable.split())[:maximum]


def _search_query(request: WebSearchRequest) -> str:
    query = request.query
    if request.allowed_domains:
        sites = " OR ".join(f"site:{domain}" for domain in request.allowed_domains)
        query = f"{query} AND ({sites})"
    elif request.blocked_domains:
        query += "".join(f" NOT site:{domain}" for domain in request.blocked_domains)
    if len(query) > _MAX_QUERY_CHARS or len(query.split()) > _MAX_QUERY_WORDS:
        raise WebSearchError(
            WebSearchErrorCode.SEARCH_INVALID_REQUEST,
            "Brave Search query exceeds its 600-character or 75-word limit",
        )
    return query


async def _provider_error_diagnostic(
    response: httpx.Response,
    *,
    api_key: str,
    query: str,
) -> str | None:
    """Project a tiny, sanitized part of Brave's structured 4xx error response."""

    if (
        response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        != "application/json"
    ):
        return None
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4_096):
        if len(body) + len(chunk) > _MAX_ERROR_RESPONSE_BYTES:
            return None
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None

    parts: list[str] = []
    code = error.get("code")
    if (
        isinstance(code, str)
        and code.isascii()
        and 0 < len(code) <= 64
        and all(character.isalnum() or character in "_-" for character in code)
    ):
        parts.append(f"code={code.upper()}")
    detail = error.get("detail")
    if isinstance(detail, str) and detail:
        detail = detail.replace(api_key, "[REDACTED]")
        if query:
            detail = re.sub(re.escape(query), "[QUERY REDACTED]", detail, flags=re.IGNORECASE)
        detail = _display_text(detail, _MAX_PROVIDER_ERROR_DETAIL_CHARS)
        if detail:
            parts.append(f"detail={detail}")
    return "; ".join(parts) if parts else None


def _source_allowed(url: str, request: WebSearchRequest) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"} or hostname is None:
        return False
    try:
        host = hostname.casefold().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if request.allowed_domains and not any(
        _matches_domain(host, domain) for domain in request.allowed_domains
    ):
        return False
    return not any(_matches_domain(host, domain) for domain in request.blocked_domains)


def _project_sources(payload: object, request: WebSearchRequest) -> tuple[WebSearchSource, ...]:
    if not isinstance(payload, Mapping):
        raise WebSearchError(
            WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
            "Brave Search returned an invalid response shape",
        )
    web = payload.get("web")
    # Brave's response schema makes `web` nullable when there are no web hits.
    if web is None and isinstance(payload.get("query"), Mapping):
        return ()
    if not isinstance(web, Mapping) or not isinstance(web.get("results"), list):
        raise WebSearchError(
            WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
            "Brave Search returned invalid result data",
        )
    sources: list[WebSearchSource] = []
    seen_urls: set[str] = set()
    for entry in web["results"][:64]:
        if not isinstance(entry, Mapping):
            continue
        url = entry.get("url")
        title = entry.get("title")
        description = entry.get("description")
        if (
            not isinstance(url, str)
            or len(url) > MAX_SOURCE_URL_CHARS
            or not isinstance(title, str)
            or not title.strip()
            or not _source_allowed(url, request)
            or url.casefold() in seen_urls
        ):
            continue
        try:
            source = WebSearchSource(
                url=url,
                title=_display_text(title, MAX_SOURCE_TITLE_CHARS),
                provider="Brave Search",
                snippet=(
                    _display_text(description, MAX_SOURCE_SNIPPET_CHARS)
                    if isinstance(description, str)
                    else None
                ),
            )
        except (TypeError, ValueError):
            continue
        sources.append(source)
        seen_urls.add(url.casefold())
        if len(sources) >= request.max_sources:
            break
    return tuple(sources)


class BraveWebSearchBackend:
    """Search a fixed HTTPS API endpoint with bounded, untrusted result projection."""

    provider_profile = "brave-search-api"
    model = "Brave Web Search"
    route_kind = WebSearchRouteKind.EXTERNAL_FALLBACK
    diagnostic_route = WebSearchRouteName.BRAVE
    capabilities = ModelCapabilitySet.from_supported(ModelCapability.HOSTED_WEB_SEARCH)

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or not api_key.isascii()
            or len(api_key) > _MAX_API_KEY_CHARS
            or any(ord(character) < 33 or ord(character) == 127 for character in api_key)
        ):
            raise ValueError("Brave Search API key is invalid")
        self._api_key = api_key
        self._transport = transport

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        del event_sink  # This HTTP backend does not emit model-hosted tool events.
        query = _search_query(request)
        body = bytearray()
        http_status: int | None = None
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(_TIMEOUT_SECONDS, connect=5.0),
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        "POST",
                        _ENDPOINT,
                        json={"q": query, "count": min(20, max(request.max_sources, 8))},
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "X-Subscription-Token": self._api_key,
                        },
                    ) as response:
                        http_status = response.status_code
                        if response.status_code in {401, 403}:
                            raise WebSearchError(
                                WebSearchErrorCode.SEARCH_AUTHENTICATION,
                                "Brave Search rejected the configured API key",
                                http_status=response.status_code,
                            )
                        if response.status_code == 429:
                            raise WebSearchError(
                                WebSearchErrorCode.SEARCH_RATE_LIMIT,
                                "Brave Search rate limit was reached",
                                http_status=response.status_code,
                            )
                        if response.status_code in {408, 504}:
                            raise WebSearchError(
                                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                                "Brave Search timed out",
                                http_status=response.status_code,
                            )
                        if response.status_code != 200:
                            diagnostic = None
                            if response.status_code in {400, 422}:
                                diagnostic = await _provider_error_diagnostic(
                                    response,
                                    api_key=self._api_key,
                                    query=query,
                                )
                            detail = (
                                f"; [untrusted Brave diagnostic: {diagnostic}]"
                                if diagnostic
                                else ""
                            )
                            raise WebSearchError(
                                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                                f"Brave Search returned HTTP {response.status_code}{detail}",
                                http_status=response.status_code,
                            )
                        if (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .casefold()
                            != "application/json"
                        ):
                            raise WebSearchError(
                                WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
                                "Brave Search returned a non-JSON response",
                            )
                        async for chunk in response.aiter_bytes(chunk_size=16_384):
                            body.extend(chunk)
                            if len(body) > _MAX_RESPONSE_BYTES:
                                raise WebSearchError(
                                    WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
                                    "Brave Search response exceeded the size limit",
                                )
        except TimeoutError as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE, "Brave Search timed out"
            ) from error
        except httpx.TimeoutException as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE, "Brave Search timed out"
            ) from error
        except httpx.HTTPError as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE, "Brave Search endpoint is unavailable"
            ) from error
        try:
            payload = json.loads(body)
        except (UnicodeError, ValueError) as error:
            raise WebSearchError(
                WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE,
                "Brave Search returned invalid JSON",
            ) from error
        sources = _project_sources(payload, request)
        return WebSearchResult(
            query=request.query,
            evidence_text=(
                f"Search returned {len(sources)} source(s). Verify claims against the linked pages."
                if sources
                else "Search returned no matching web sources."
            ),
            sources=sources,
            provider_profile=self.provider_profile,
            model=self.model,
            http_status=http_status,
        )


__all__ = ["BRAVE_SEARCH_API_KEY_ENV", "BraveWebSearchBackend"]
