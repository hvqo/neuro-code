# ADR 0176: Independent Search API backend

[简体中文](../../zh-CN/adr/0176-independent-search-api-backend.md) · **English**

- Status: Accepted
- Date: 2026-09-24
- Scope: Model-independent Web Search through a configured Search API key

## Context

DeepSeek's Responses API supports client function calls but ignores the
built-in `web_search` request type. Other MAIN profiles may likewise lack a
trusted executable hosted-search route. A configured model API key therefore
does not by itself make Neuro's search route executable. Managed model
profiles also intentionally do not infer hosted tools from a compatible wire
protocol. The effective Settings view must distinguish a model provider from an
actual Search backend.

## Decision

Neuro supports the Brave Web Search API as an independent backend for the
existing canonical `web_search` client tool. The user supplies
`BRAVE_SEARCH_API_KEY` through the process environment or a secret manager and
restarts Neuro. The key is not stored in the workspace, a provider profile, or
durable conversation. Configuration registers the environment variable as a
protected secret and includes its value in tool-result redaction.

For `auto`, the stable priority remains MAIN hosted search, an explicit
executable `WEB_SEARCH` route, then an automatically discovered trusted hosted
route. If none exists, a valid Brave key selects `SEARCH_API`. `sidecar` may
also use the independent API when no hosted route is configured. `disabled`
does not expose search, and `inline` still requires MAIN hosted capability. An
explicit but unavailable `WEB_SEARCH` route does not silently fall back to a
different service. The binding registers the local tool only for a resolved
backend and an allowed tool name. A missing or malformed key leaves search
unavailable.

The Brave adapter sends one bounded HTTPS request to a fixed endpoint, never
follows redirects, caps query length, response bytes, source count, and time,
and projects only bounded title, URL, and snippet fields into canonical
`WebSearchResult`. Domain restrictions are applied to the API query and checked
again against returned source hostnames. HTTP failures become typed,
credential-free search errors. Cancellation closes the request. Search results
remain untrusted evidence; no model-written summary or page instruction gains
authority through this backend.

The independent API is usable with any MAIN provider that can call Neuro's
client tool, including DeepSeek, Qwen, Anthropic, and Gemini. Their own hosted
search capability claims remain separate and fail closed as before. The Search
API path is reported as such in Settings and `/status`, rather than as a model
provider. A separate API key and network access are required; an API key's
presence does not prove that the remote service will accept it.

## Verification

Focused tests cover the real provider adapters' composition and model-visible
tool execution, bounded HTTP projection, domain filtering, redaction, typed
failures, cancellation, and Settings status. Live paid-service execution is
not part of the default completion gate.

## References

- [Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search)
- [Brave API authentication](https://api-dashboard.search.brave.com/documentation/guides/authentication)
- [DeepSeek Responses API tool compatibility](https://api-docs.deepseek.com/guides/responses_api/)
