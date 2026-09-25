# ADR 0176: Provider Search routing and external fallback

[简体中文](../../zh-CN/adr/0176-independent-search-api-backend.md) · **English**

- Status: Accepted
- Date: 2026-09-24
- Scope: Resolve executable Web Search routes across native, provider-adapter, and external backends

## Context

Provider search protocols are not interchangeable. OpenAI Responses, Anthropic
Messages, Gemini Interactions, DeepSeek's Anthropic-compatible Search, and
Brave's independent API each have distinct request and response contracts.
DeepSeek's OpenAI-compatible Responses and Chat interfaces do not execute the
built-in web_search tool. An OpenAI-compatible protocol or a model API key
alone therefore cannot establish executable Search capability.

## Decision

### One canonical contract and trusted capability registry

application.ports.web_search remains the provider-neutral request, result,
error, mode, route-kind, and execution-port boundary. A trusted
SearchCapabilityRegistry resolves only concrete known adapters from provider
service identity, protocol, model capability facts, and route configuration.
It never infers Search support from OpenAI compatibility or a provider name
alone. The existing OpenAI Responses, Anthropic Messages, and Gemini
Interactions adapters remain the native protocol implementations.

The effective route kind is reported as native, provider adapter, external
fallback, or unavailable. Composition registers the local web_search tool
only when a concrete client-side route chain exists and the active tool policy
allows it. Native MAIN search remains available only when the trusted provider
capability and tool-combination checks pass.

### Ordered route resolution

For auto and sidecar, executable routes are ordered as follows:

1. Trusted MAIN native Search, when usable alongside the active client tools.
2. A registered provider-specific adapter or configured hosted Search route.
3. A user-configured Brave Search API key.
4. An explicit unavailable state, with no model-visible web_search tool.

The DeepSeek adapter uses the user's existing DeepSeek API credential with the
Anthropic-compatible /messages Search contract. It sends that credential only
to a fixed HTTPS endpoint on api.deepseek.com. Resolution requires
service_id = "deepseek", HTTPS, the exact official host, an official API base
path, and a non-proxy-managed credential. A custom base URL, compatibility
gateway, or unknown host is not eligible; its key is never sent to the official
endpoint. Such profiles proceed to another configured route or Brave.

The adapter sends web_search_20250305 using an explicit bounded search
instruction in an Anthropic text block, then converts bounded text, sources,
and citations into canonical WebSearchResult. Domain restrictions are stated
to the provider and enforced again against returned source hosts. It does not
expose provider-native payloads to Agent Runtime. Qwen and other compatible providers remain
unsupported unless a future trusted adapter or explicit native capability is
added; enable_search or a Responses-like API is not assumed to mean
web_search.

Brave is a final user-configured fallback. Its key is stored in Neuro's
user-state credentials.json through Settings → Web, or supplied through
BRAVE_SEARCH_API_KEY, which takes precedence. The file is protected by
filesystem permissions (0600 on POSIX) but is not encrypted or stored in a
system keychain. Search credentials never enter the workspace or durable
conversation.

disabled continues to disable Search. Explicit inline continues to require
MAIN native Search and fails closed when unavailable. In routed modes,
unsupported capability, endpoint-unavailable, timeout, or did-not-search
outcomes may advance to the next route. Authentication and rate-limit errors
surface immediately. Invalid requests and malformed responses are not hidden
by another provider. A did-not-search response is scoped to that request: it
may fall back for that request but does not mark the route unavailable. Each
new request re-evaluates candidates from configured priority order, so a
successful fallback does not permanently outrank a higher-priority route.
Route-wide unsupported or endpoint-unavailable failures remain suppressed for
the lifetime of the binding after confirmation.

### Bounded, untrusted evidence and diagnostics

Provider results are projected into the existing bounded canonical result.
Domain filters are checked against returned hosts. HTTP clients use fixed
endpoints where applicable, do not follow redirects for Search APIs, enforce
response-size/time limits, and do not surface credentials or untrusted provider
error bodies. Search evidence remains untrusted data, not instruction authority.

The application-owned runtime inspection reports availability, execution path,
route kind, safe provider/model labels, and the configured fallback in the
existing Settings and /status surfaces. It omits endpoints and secrets.

## Verification

Mock-transport tests cover DeepSeek's official credential route, custom-base
containment, explicit search request mapping and domain filtering, normalized
result projection, native/adapter/Brave ordering, request-scoped no-search
fallback and next-request priority retry, unsupported and unavailable fallback,
authentication/rate-limit behavior, malformed responses, no-route tool gating,
and existing provider/runtime integration. Live paid-provider calls are not
part of the default test gate.

## References

- [DeepSeek Anthropic API compatibility](https://api-docs.deepseek.com/guides/anthropic_api/)
- [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)
- [Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search)
- [Brave API authentication](https://api-dashboard.search.brave.com/documentation/guides/authentication)
