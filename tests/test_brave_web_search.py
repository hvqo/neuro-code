from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_search import (
    HostedWebSearch,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
)
from neuro_code.application.web_search.service import WebSearchService
from neuro_code.infrastructure.web_search.brave import BraveWebSearchBackend


class _NoRoute:
    def route(self, role: RuntimeRole) -> ModelRoute | None:
        del role
        return None


class _NoResolver:
    def resolve(self, route: ModelRoute) -> HostedWebSearch | None:
        del route
        return None


class BraveWebSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_fixed_endpoint_and_filters_untrusted_sources(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Official &amp; current",
                                "url": "https://docs.example.com/guide",
                                "description": "Version 2",
                            },
                            {
                                "title": "Other",
                                "url": "https://other.example.net/guide",
                                "description": "Excluded by the local domain check",
                            },
                            {
                                "title": "Spoofed domain",
                                "url": "https://docs.example.com.evil.net/guide",
                                "description": "Also excluded by the local domain check",
                            },
                            {
                                "title": "Unsafe",
                                "url": "file:///etc/passwd",
                                "description": "Not a web URL",
                            },
                        ]
                    }
                },
            )

        backend = BraveWebSearchBackend("test-search-key", transport=httpx.MockTransport(respond))
        service = WebSearchService(_NoRoute(), _NoResolver(), direct_backend=backend)
        result = await service.search(
            WebSearchRequest("current guide", allowed_domains=("example.com",))
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "api.search.brave.com")
        self.assertEqual(requests[0].url.path, "/res/v1/web/search")
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[0].headers["X-Subscription-Token"], "test-search-key")
        self.assertIn("site:example.com", json.loads(requests[0].content)["q"])
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].title, "Official & current")
        self.assertEqual(result.sources[0].url, "https://docs.example.com/guide")
        self.assertEqual(result.provider_profile, "brave-search-api")

    async def test_blocked_domain_excludes_subdomains_after_search(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            self.assertIn("NOT site:blocked.example", json.loads(request.content)["q"])
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "web": {
                        "results": [
                            {"title": "Blocked", "url": "https://sub.blocked.example/page"},
                            {"title": "Allowed", "url": "https://allowed.example/page"},
                        ]
                    }
                },
            )

        backend = BraveWebSearchBackend("test-key", transport=httpx.MockTransport(respond))
        result = await backend.search(
            WebSearchRequest("query", blocked_domains=("blocked.example",))
        )
        self.assertEqual(
            [source.url for source in result.sources], ["https://allowed.example/page"]
        )

    async def test_valid_empty_web_response_is_not_a_provider_failure(self) -> None:
        backend = BraveWebSearchBackend(
            "test-key",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"query": {"original": "unmatched query"}, "web": None},
                )
            ),
        )
        result = await backend.search(WebSearchRequest("unmatched query"))
        self.assertEqual(result.sources, ())
        self.assertIn("no matching web sources", result.evidence_text)

    async def test_blocked_idna_domain_cannot_bypass_local_filter(self) -> None:
        backend = BraveWebSearchBackend(
            "test-key",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={
                        "web": {
                            "results": [
                                {"title": "Blocked", "url": "https://bücher.example/page"},
                                {"title": "Allowed", "url": "https://allowed.example/page"},
                            ]
                        }
                    },
                )
            ),
        )
        result = await backend.search(
            WebSearchRequest("query", blocked_domains=("xn--bcher-kva.example",))
        )
        self.assertEqual(
            [source.url for source in result.sources], ["https://allowed.example/page"]
        )

    async def test_secret_is_redacted_before_request_and_from_response(self) -> None:
        seen_queries: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen_queries.append(json.loads(request.content)["q"])
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "web": {
                        "results": [
                            {
                                "title": "secret-value title",
                                "url": "https://example.com/guide",
                                "description": "secret-value snippet",
                            }
                        ]
                    }
                },
            )

        backend = BraveWebSearchBackend("secret-value", transport=httpx.MockTransport(respond))
        service = WebSearchService(
            _NoRoute(),
            _NoResolver(),
            direct_backend=backend,
            redaction_values=("secret-value",),
        )
        result = await service.search(WebSearchRequest("find secret-value"))
        self.assertEqual(seen_queries, ["find [REDACTED]"])
        self.assertNotIn("secret-value", str(result))

    async def test_http_failures_have_typed_credential_free_errors(self) -> None:
        cases = (
            (401, WebSearchErrorCode.SEARCH_AUTHENTICATION),
            (429, WebSearchErrorCode.SEARCH_RATE_LIMIT),
            (503, WebSearchErrorCode.SEARCH_PROVIDER_ERROR),
            (302, WebSearchErrorCode.SEARCH_PROVIDER_ERROR),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                backend = BraveWebSearchBackend(
                    "secret-value",
                    transport=httpx.MockTransport(
                        lambda request, status=status: httpx.Response(status)
                    ),
                )
                with self.assertRaises(WebSearchError) as raised:
                    await backend.search(WebSearchRequest("query"))
                self.assertIs(raised.exception.code, expected)
                self.assertNotIn("secret-value", str(raised.exception))

    async def test_large_or_invalid_response_and_query_fail_closed(self) -> None:
        for content in (b"x" * 1_048_577, b"not-json"):
            with self.subTest(content_length=len(content)):
                backend = BraveWebSearchBackend(
                    "test-key",
                    transport=httpx.MockTransport(
                        lambda request, content=content: httpx.Response(
                            200,
                            headers={"Content-Type": "application/json"},
                            content=content,
                        )
                    ),
                )
                with self.assertRaises(WebSearchError) as raised:
                    await backend.search(WebSearchRequest("query"))
                self.assertIs(raised.exception.code, WebSearchErrorCode.SEARCH_PROVIDER_ERROR)

        backend = BraveWebSearchBackend(
            "test-key",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=json.dumps({"web": {"results": []}}).encode(),
                )
            ),
        )
        with self.assertRaises(WebSearchError) as raised:
            await backend.search(WebSearchRequest("x" * 601))
        self.assertIs(raised.exception.code, WebSearchErrorCode.SEARCH_INVALID_REQUEST)

    async def test_cancellation_stops_the_http_request(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def respond(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(200, json={"web": {"results": []}})

        backend = BraveWebSearchBackend("test-key", transport=httpx.MockTransport(respond))
        task = asyncio.create_task(backend.search(WebSearchRequest("query")))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()


if __name__ == "__main__":
    unittest.main()
