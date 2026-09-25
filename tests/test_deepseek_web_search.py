from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from neuro_code.application.ports.configuration import AppConfig, ProviderProfile
from neuro_code.application.ports.model import ModelCapability, ModelCapabilitySet
from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_search import (
    HostedWebSearchEventSink,
    WebSearchError,
    WebSearchErrorCode,
    WebSearchRequest,
    WebSearchResult,
    WebSearchRouteKind,
    WebSearchRouteName,
    WebSearchRoutePhase,
    render_web_search_route_trace,
)
from neuro_code.infrastructure.providers.hosted_web_search import (
    RoutedHostedWebSearchBackend,
    RoutedWebSearchBackendResolver,
    SearchCapabilityRegistry,
)
from neuro_code.infrastructure.web_search.deepseek import (
    DeepSeekSearchAdapter,
    is_official_deepseek_profile,
)


def _deepseek_profile(
    *,
    base_url: str = "https://api.deepseek.com",
    auth: str = "env",
    stored_api_key: str | None = None,
) -> ProviderProfile:
    return ProviderProfile(
        name="deepseek-main",
        service_id="deepseek",
        protocol="openai-chat",
        dialect="deepseek-v4",
        model="deepseek-flash",
        base_url=base_url,
        auth=auth,
        api_key_env="DEEPSEEK_API_KEY" if auth == "env" else None,
        stored_api_key=stored_api_key,
        proxy_mode="direct",
    )


def _openai_catalog_deepseek_profile(
    *,
    base_url: str = "https://api.deepseek.com/v1",
) -> ProviderProfile:
    """Model the user's official DeepSeek profile as loaded by the live config."""

    return ProviderProfile(
        name="deepseek",
        service_id="openai",
        protocol="openai-responses",
        dialect="standard",
        model="deepseek-flash",
        base_url=base_url,
        auth="stored",
        stored_api_key="stored-deepseek-key",
        proxy_mode="direct",
    )


class _StubBackend:
    capabilities = ModelCapabilitySet.from_supported(ModelCapability.HOSTED_WEB_SEARCH)

    def __init__(
        self,
        provider_profile: str,
        *,
        result: WebSearchResult | None = None,
        error: WebSearchError | None = None,
        calls: list[str],
    ) -> None:
        self.provider_profile = provider_profile
        self.model = provider_profile
        self.result = result or WebSearchResult(
            "query",
            provider_profile,
            provider_profile=provider_profile,
            model=provider_profile,
        )
        self.error = error
        self.calls = calls

    async def search(
        self,
        request: WebSearchRequest,
        *,
        event_sink: HostedWebSearchEventSink | None = None,
    ) -> WebSearchResult:
        del request, event_sink
        self.calls.append(self.provider_profile)
        if self.error is not None:
            raise self.error
        return self.result


class DeepSeekWebSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_credentials_use_fixed_anthropic_search_endpoint(self) -> None:
        observed: list[httpx.Request] = []
        payload = {
            "content": [
                {
                    "type": "text",
                    "text": "The official guide describes the feature.",
                    "citations": [
                        {
                            "type": "web_search_result_location",
                            "url": "https://docs.example.com/guide",
                            "title": "Guide",
                            "cited_text": "official evidence",
                        }
                    ],
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "search-1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://docs.example.com/guide",
                            "title": "Guide",
                        },
                        {
                            "type": "web_search_result",
                            "url": "https://blocked.example/private",
                            "title": "Filtered",
                        },
                    ],
                },
            ]
        }

        async def respond(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(200, json=payload)

        profile = _deepseek_profile()
        adapter = DeepSeekSearchAdapter(
            profile,
            environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
            transport=httpx.MockTransport(respond),
        )
        self.assertTrue(is_official_deepseek_profile(profile))

        result = await adapter.search(
            WebSearchRequest(
                "feature behavior",
                blocked_domains=("blocked.example",),
            )
        )

        self.assertEqual(len(observed), 1)
        self.assertEqual(str(observed[0].url), "https://api.deepseek.com/anthropic/v1/messages")
        self.assertEqual(observed[0].headers["x-api-key"], "deepseek-secret")
        self.assertNotIn("authorization", observed[0].headers)
        request_payload = json.loads(observed[0].content)
        self.assertEqual(request_payload["model"], "deepseek-flash")
        self.assertEqual(request_payload["max_tokens"], 4096)
        self.assertEqual(
            request_payload["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Perform a web search for the query: feature behavior\n"
                                "Exclude sources from these domains: blocked.example"
                            ),
                        }
                    ],
                }
            ],
        )
        self.assertEqual(request_payload["tools"][0]["type"], "web_search_20250305")
        self.assertNotIn("tool_choice", request_payload)
        self.assertEqual(result.provider_profile, "DeepSeek Search")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.evidence_text, "The official guide describes the feature.")
        self.assertEqual(
            [source.url for source in result.sources], ["https://docs.example.com/guide"]
        )
        self.assertEqual(result.citations[0].cited_text, "official evidence")

    async def test_allowed_domains_are_instructed_and_enforced_after_search(self) -> None:
        observed: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "web_search_tool_result",
                            "content": [
                                {
                                    "type": "web_search_result",
                                    "url": "https://docs.example.com/guide",
                                    "title": "Allowed",
                                },
                                {
                                    "type": "web_search_result",
                                    "url": "https://other.example/guide",
                                    "title": "Filtered",
                                },
                            ],
                        }
                    ]
                },
            )

        adapter = DeepSeekSearchAdapter(
            _deepseek_profile(),
            environ={"DEEPSEEK_API_KEY": "deepseek-secret"},
            transport=httpx.MockTransport(respond),
        )

        result = await adapter.search(
            WebSearchRequest("official guide", allowed_domains=("docs.example.com",))
        )

        request_payload = json.loads(observed[0].content)
        prompt = request_payload["messages"][0]["content"][0]["text"]
        self.assertIn("Only include sources from these domains: docs.example.com", prompt)
        self.assertEqual(
            [source.url for source in result.sources], ["https://docs.example.com/guide"]
        )

    async def test_stored_deepseek_credential_is_reused(self) -> None:
        seen_headers: list[str] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            seen_headers.append(request.headers["x-api-key"])
            return httpx.Response(
                200,
                json={"content": [{"type": "web_search_tool_result", "content": []}]},
            )

        profile = _deepseek_profile(auth="stored", stored_api_key="stored-deepseek-key")
        adapter = DeepSeekSearchAdapter(
            profile,
            transport=httpx.MockTransport(respond),
        )

        await adapter.search(WebSearchRequest("query"))

        self.assertEqual(seen_headers, ["stored-deepseek-key"])

    async def test_official_openai_catalog_profile_executes_without_brave(self) -> None:
        observed: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "web_search_tool_result",
                            "tool_use_id": "search-1",
                            "content": [
                                {
                                    "type": "web_search_result",
                                    "url": "https://docs.example.com/search",
                                    "title": "Search result",
                                }
                            ],
                        }
                    ]
                },
            )

        profile = _openai_catalog_deepseek_profile()
        registry = SearchCapabilityRegistry()
        self.assertTrue(is_official_deepseek_profile(profile))
        self.assertIsInstance(registry.resolve_provider_adapter(profile), DeepSeekSearchAdapter)

        adapter = DeepSeekSearchAdapter(
            profile,
            transport=httpx.MockTransport(respond),
        )
        router = RoutedHostedWebSearchBackend((adapter,))
        result = await router.search(WebSearchRequest("official search test"))

        self.assertEqual(len(observed), 1)
        self.assertEqual(
            str(observed[0].url),
            "https://api.deepseek.com/anthropic/v1/messages",
        )
        self.assertEqual(observed[0].headers["x-api-key"], "stored-deepseek-key")
        self.assertEqual(result.provider_profile, "DeepSeek Search")
        self.assertEqual(
            [event.phase for event in result.route_trace],
            [
                WebSearchRoutePhase.CANDIDATE,
                WebSearchRoutePhase.SELECTED,
                WebSearchRoutePhase.DISPATCHED,
                WebSearchRoutePhase.SUCCEEDED,
            ],
        )
        self.assertEqual(result.route_trace[0].route, WebSearchRouteName.DEEPSEEK)
        self.assertEqual(result.route_trace[-1].http_status, 200)

    def test_custom_openai_gateway_does_not_register_deepseek_search(self) -> None:
        profile = _openai_catalog_deepseek_profile(
            base_url="https://enterprise-gateway.example/v1",
        )

        self.assertFalse(is_official_deepseek_profile(profile))
        self.assertIsNone(SearchCapabilityRegistry().resolve_profile(profile))

    async def test_deepseek_http_and_malformed_failures_are_typed(self) -> None:
        profile = _deepseek_profile()
        cases = (
            (401, b"", WebSearchErrorCode.SEARCH_AUTHENTICATION),
            (429, b"", WebSearchErrorCode.SEARCH_RATE_LIMIT),
            (503, b"", WebSearchErrorCode.SEARCH_UNAVAILABLE),
            (400, b"", WebSearchErrorCode.SEARCH_UNSUPPORTED),
            (200, b"not-json", WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE),
        )
        for status, body, code in cases:
            with self.subTest(status=status, code=code):
                adapter = DeepSeekSearchAdapter(
                    profile,
                    environ={"DEEPSEEK_API_KEY": "deepseek-key"},
                    transport=httpx.MockTransport(
                        lambda request, status=status, body=body: httpx.Response(
                            status,
                            content=body,
                        )
                    ),
                )
                with self.assertRaises(WebSearchError) as raised:
                    await adapter.search(WebSearchRequest("query"))
                self.assertIs(raised.exception.code, code)

    def test_custom_base_url_is_not_registered_as_deepseek_official_search(self) -> None:
        called = False

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={"content": []})

        profile = _deepseek_profile(base_url="https://enterprise-gateway.example/v1")
        self.assertFalse(is_official_deepseek_profile(profile))
        self.assertIsNone(SearchCapabilityRegistry().resolve_profile(profile))
        with self.assertRaises(ValueError):
            DeepSeekSearchAdapter(
                profile,
                environ={"DEEPSEEK_API_KEY": "gateway-secret"},
                transport=httpx.MockTransport(respond),
            )
        self.assertFalse(called)

    def test_qwen_openai_compatibility_is_not_treated_as_native_search(self) -> None:
        qwen = ProviderProfile(
            name="qwen-responses",
            service_id="bailian",
            protocol="openai-responses",
            model="qwen3.5-plus",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key_env="QWEN_API_KEY",
            builtin_tools=("web_search",),
            proxy_mode="direct",
        )

        self.assertIsNone(SearchCapabilityRegistry().resolve_profile(qwen))

    def test_official_route_resolution_uses_provider_adapter_capability(self) -> None:
        profile = _deepseek_profile()
        config = AppConfig(
            cwd=Path("/workspace"),
            state_dir=Path("/state"),
            providers={profile.name: profile},
            default_provider=profile.name,
            selected_provider=profile.name,
        )

        backend = RoutedWebSearchBackendResolver(config).resolve_profile(profile.name)

        self.assertIsInstance(backend, DeepSeekSearchAdapter)
        assert isinstance(backend, DeepSeekSearchAdapter)
        self.assertIs(backend.route_kind, WebSearchRouteKind.PROVIDER_ADAPTER)
        self.assertIsNone(
            RoutedWebSearchBackendResolver(config).resolve(
                ModelRoute(RuntimeRole.MAIN, profile.name, profile.model)
            )
        )

    def test_native_search_resolution_precedes_adapters_and_external_fallback(self) -> None:
        from neuro_code.application.ports.web_search import (
            WebSearchExecutionPath,
            WebSearchMode,
            resolve_web_search_path,
        )

        self.assertIs(
            resolve_web_search_path(
                WebSearchMode.AUTO,
                inline_supported=True,
                sidecar_available=True,
                search_api_available=True,
            ),
            WebSearchExecutionPath.INLINE_HOSTED,
        )

    async def test_provider_adapter_falls_back_to_brave_only_for_unavailable_route(self) -> None:
        calls: list[str] = []
        provider_adapter = _StubBackend(
            "provider-adapter",
            error=WebSearchError(WebSearchErrorCode.SEARCH_UNAVAILABLE, "offline"),
            calls=calls,
        )
        brave = _StubBackend("Brave Search", calls=calls)
        router = RoutedHostedWebSearchBackend((provider_adapter, brave))

        result = await router.search(WebSearchRequest("query"))

        self.assertEqual(calls, ["provider-adapter", "Brave Search"])
        self.assertEqual(result.provider_profile, "Brave Search")
        self.assertEqual(router.provider_profile, "Brave Search")

    async def test_route_trace_keeps_deepseek_failure_and_final_brave_status(self) -> None:
        query = "private search query"
        secret = "never-show-this-credential"
        deepseek = _StubBackend(
            "DeepSeek Search",
            error=WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                "DeepSeek endpoint unavailable",
                http_status=503,
            ),
            calls=[],
        )
        deepseek.diagnostic_route = WebSearchRouteName.DEEPSEEK
        brave = _StubBackend(
            "Brave Search",
            error=WebSearchError(
                WebSearchErrorCode.SEARCH_UNAVAILABLE,
                "Brave Search returned HTTP 422",
                http_status=422,
            ),
            calls=[],
        )
        brave.diagnostic_route = WebSearchRouteName.BRAVE
        router = RoutedHostedWebSearchBackend((deepseek, brave))

        with self.assertRaises(WebSearchError) as raised:
            await router.search(WebSearchRequest(query))

        error = raised.exception
        self.assertEqual(error.http_status, 422)
        self.assertEqual(error.code, WebSearchErrorCode.SEARCH_UNAVAILABLE)
        trace = render_web_search_route_trace(error.route_trace)
        self.assertIn("DeepSeek Search:candidate", trace)
        self.assertIn("DeepSeek Search:selected", trace)
        self.assertIn("DeepSeek Search:dispatched", trace)
        self.assertIn("DeepSeek Search:failed:SEARCH_UNAVAILABLE:HTTP 503", trace)
        self.assertIn("Brave Search:fallback:SEARCH_UNAVAILABLE", trace)
        self.assertIn("Brave Search:failed:SEARCH_UNAVAILABLE:HTTP 422", trace)
        self.assertNotIn(query, trace)
        self.assertNotIn(secret, trace)

    async def test_authentication_failure_does_not_fall_back_to_brave(self) -> None:
        calls: list[str] = []
        provider_adapter = _StubBackend(
            "provider-adapter",
            error=WebSearchError(
                WebSearchErrorCode.SEARCH_AUTHENTICATION,
                "provider credential rejected",
            ),
            calls=calls,
        )
        brave = _StubBackend("Brave Search", calls=calls)
        router = RoutedHostedWebSearchBackend((provider_adapter, brave))

        with self.assertRaises(WebSearchError) as raised:
            await router.search(WebSearchRequest("query"))

        self.assertIs(raised.exception.code, WebSearchErrorCode.SEARCH_AUTHENTICATION)
        self.assertEqual(calls, ["provider-adapter"])

    async def test_rate_limit_does_not_fall_back_to_brave(self) -> None:
        calls: list[str] = []
        provider_adapter = _StubBackend(
            "provider-adapter",
            error=WebSearchError(WebSearchErrorCode.SEARCH_RATE_LIMIT, "rate limited"),
            calls=calls,
        )
        brave = _StubBackend("Brave Search", calls=calls)
        router = RoutedHostedWebSearchBackend((provider_adapter, brave))

        with self.assertRaises(WebSearchError) as raised:
            await router.search(WebSearchRequest("query"))

        self.assertIs(raised.exception.code, WebSearchErrorCode.SEARCH_RATE_LIMIT)
        self.assertEqual(calls, ["provider-adapter"])

    async def test_unsupported_route_falls_back_but_malformed_response_does_not(self) -> None:
        for code, expected_calls in (
            (WebSearchErrorCode.SEARCH_UNSUPPORTED, ["provider", "brave"]),
            (WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE, ["provider"]),
        ):
            with self.subTest(code=code):
                calls: list[str] = []
                first = _StubBackend(
                    "provider",
                    error=WebSearchError(code, "failed"),
                    calls=calls,
                )
                second = _StubBackend("brave", calls=calls)
                router = RoutedHostedWebSearchBackend((first, second))
                if code is WebSearchErrorCode.SEARCH_MALFORMED_RESPONSE:
                    with self.assertRaises(WebSearchError):
                        await router.search(WebSearchRequest("query"))
                else:
                    await router.search(WebSearchRequest("query"))
                self.assertEqual(calls, expected_calls)

    async def test_unavailable_route_is_not_retried_after_it_was_confirmed(self) -> None:
        calls: list[str] = []
        backend = _StubBackend(
            "unavailable",
            error=WebSearchError(WebSearchErrorCode.SEARCH_UNSUPPORTED, "not supported"),
            calls=calls,
        )
        router = RoutedHostedWebSearchBackend((backend,))

        for _ in range(2):
            with self.assertRaises(WebSearchError):
                await router.search(WebSearchRequest("query"))

        self.assertEqual(calls, ["unavailable"])

    async def test_did_not_search_falls_back_once_then_retries_primary_next_request(self) -> None:
        calls: list[str] = []
        primary = _StubBackend(
            "DeepSeek Search",
            error=WebSearchError(
                WebSearchErrorCode.SEARCH_PROVIDER_DID_NOT_SEARCH,
                "no completed search result",
            ),
            calls=calls,
        )
        primary.diagnostic_route = WebSearchRouteName.DEEPSEEK

        async def primary_search(
            request: WebSearchRequest,
            *,
            event_sink: HostedWebSearchEventSink | None = None,
        ) -> WebSearchResult:
            calls.append("DeepSeek Search")
            if calls.count("DeepSeek Search") == 1:
                raise WebSearchError(
                    WebSearchErrorCode.SEARCH_PROVIDER_DID_NOT_SEARCH,
                    "no completed search result",
                )
            del request, event_sink
            return primary.result

        primary.search = primary_search  # type: ignore[method-assign]
        brave = _StubBackend("Brave Search", calls=calls)
        brave.diagnostic_route = WebSearchRouteName.BRAVE
        router = RoutedHostedWebSearchBackend((primary, brave))

        first = await router.search(WebSearchRequest("first query"))
        second = await router.search(WebSearchRequest("second query"))

        self.assertEqual(first.provider_profile, "Brave Search")
        self.assertEqual(second.provider_profile, "DeepSeek Search")
        self.assertEqual(calls, ["DeepSeek Search", "Brave Search", "DeepSeek Search"])

    async def test_parallel_requests_do_not_repeat_a_confirmed_unavailable_route(self) -> None:
        calls: list[str] = []
        backend = _StubBackend(
            "unavailable",
            error=WebSearchError(WebSearchErrorCode.SEARCH_UNAVAILABLE, "offline"),
            calls=calls,
        )
        router = RoutedHostedWebSearchBackend((backend,))

        results = await asyncio.gather(
            router.search(WebSearchRequest("query one")),
            router.search(WebSearchRequest("query two")),
            return_exceptions=True,
        )

        self.assertTrue(all(isinstance(result, WebSearchError) for result in results))
        self.assertEqual(calls, ["unavailable"])


if __name__ == "__main__":
    unittest.main()
