# ADR 0164: Model-facing tool-result guard

- Status: Accepted
- Date: 2026-09-16
- Scope: CM4a bounded normal-agent tool-result context projection

## Context

Individual tools already apply useful output limits, and some tools persist a
larger redacted result in the existing session-scoped output-artifact store.
Those limits are not a complete boundary for extension tools or every terminal
path. The runtime previously placed `ToolResult.content` directly in a
`Role.TOOL` message, so one unexpectedly large result could dominate the next
model request even though the runtime still needed its complete value for
verification, supervision, progress, workspace evidence, events, and artifact
metadata.

The model-facing context must therefore have one deterministic projection
boundary without creating a second execution truth or a second artifact store.

## Decision

`ToolResult` remains the canonical execution result. `ToolResultContextProjection`
is a typed, provider-neutral value describing the bounded content and metrics
that may be placed in a model-facing tool message. The application runtime
owns the pure `project_tool_result()` policy in
`application/runtime/tool_result_guard.py`.

The policy uses the existing `ToolContext.output_byte_limit` as the configured
local bound and applies a conservative provider-neutral global ceiling of
64 KiB and 16,384 approximate tokens. Token values use the existing
`estimate_text_tokens()` estimator; they are explicitly not provider-native
tokenizer results. The complete request continues through
`ContextPreflight`, which uses the configured `ProviderContextWindow`, output
reserve, safety margin, and `estimate_model_request_tokens()` accounting. The
guard is not a second provider budget or an independent rollover threshold.

Results within both limits pass through byte-for-byte. Oversized text uses a
deterministic UTF-8-safe head/tail projection and a marker that identifies the
omission, indicates whether a fuller existing artifact is available, and asks
for targeted, filtering, or range-based follow-up requests. Error projections
identify themselves as error output and preserve the tail, where exit status
and diagnostics commonly appear. An impossible zero-byte bound produces an
empty bounded projection and retains the size facts in diagnostics.

The projection is applied only when `ToolExecutor` constructs a
`Message(Role.TOOL, ...)`, including successful, error, permission,
cancelled/skipped, parallel, and control-rejection pairings. The same bounded
metadata is added to existing terminal tool events. The canonical full
`ToolResult` is still passed first to hooks, observations, verification,
workspace/progress handling, plan handling, `ToolExecutionResult`, and the
existing terminal event payload. No raw output is copied into projection
metadata.

For a parallel tool batch, `AgentLoop` owns the final aggregate boundary after
the complete ordered call set is known. It first preflights a base request
containing the current context, preserved provider items, the assistant tool
call message, and empty result placeholders. When capacity is known, the
remaining estimated capacity after that base preflight becomes a conservative
aggregate token allowance; otherwise the provider-neutral token ceiling is
used. The aggregate byte allowance is the smaller of the global byte ceiling
and `ToolContext.output_byte_limit * call_count`. Both allowances are split
deterministically across calls, with any remainder assigned to earlier calls.
This leaves every result a share, preserves identity and commit order, and
ensures the next preflight sees exactly the aggregate-projected batch. These
token reservations are approximate; `ContextPreflight` remains authoritative.

A fuller-artifact marker is emitted only when the runtime-owned artifact-store
boundary has returned a canonical `ToolOutputArtifact` during the executor
lifecycle and the result metadata matches that typed handle exactly. Generic
`output_artifact_*` metadata alone is never trusted, so custom/MCP tools cannot
manufacture the claim merely by choosing those keys. CM4a never creates a new
artifact and never changes the existing artifact store, redaction, read,
permission, session-association, or garbage-collection contracts. An artifact
is an external reread surface; it is not model context.

## Invariants and non-goals

- Canonical `ToolResult` is not the model-facing projection. A bounded
  `Role.TOOL` message must not replace full canonical evidence used by
  verification, supervision, finalization, or UI/ACP terminal projections.
- The next request is built and preflighted normally from the projected
  `Role.TOOL` message. No preflight bypass or special-case accounting is
  introduced.
- Parallel result projection has a deterministic per-call share at the
  `AgentLoop` batch boundary; a large result cannot consume the whole batch
  allowance or unpredictably starve later results.
- Tool-call IDs, names, parallel merge order, error state, permission and
  cancellation pairings remain unchanged.
- Explicit redaction happens at the existing runtime boundary before both
  canonical downstream consumers and the model projection; the guard never
  weakens it or exposes artifact paths.
- Existing Bash, filesystem, search, web, background, terminal, TUI, CLI, and
  ACP artifact behavior remains owned by its existing adapter/application
  boundary. Extension tools still receive the same deterministic guard, but
  only typed artifacts returned through the runtime store boundary can enable
  an omission marker.
- There is no LLM summarization, semantic retrieval, automatic old-result
  clearing, new artifact database, cross-session artifact retrieval, result
  rewriting, CM3 change, UltraCode redesign, or subagent policy expansion.

## Compatibility and validation

The change adds no persistence schema and no new durable result owner.
Existing artifact metadata and terminal event consumers remain compatible
because the canonical `content`, `is_error`, metadata, and
`execution_result` fields retain their existing meaning; projection facts are
an additive event field. Focused regressions cover pass-through, head/tail and
error projections, canonical observation/verification content, runtime-owned
artifact reuse, rejection of fake artifact metadata, redaction, preflight
reduction, and parallel identity/order. Existing
artifact-store/read, background, TUI/CLI/ACP, recovery, compaction, rollover,
Working Set, history, and architecture checks remain the compatibility gate.
