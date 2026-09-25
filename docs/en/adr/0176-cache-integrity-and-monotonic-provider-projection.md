# ADR 0176: Cache Integrity and Monotonic Provider Projection

[简体中文](../../zh-CN/adr/0176-cache-integrity-and-monotonic-provider-projection.md) · **English**

- Status: Accepted
- Date: 2026-09-25
- Scope: Provider request diagnostics, DeepSeek history replay, synthetic context projection, cache epochs, and cache usage metrics

## Context

Prompt caching is affected by the final request produced by a Provider adapter,
not only by `ModelContext`. Rebuilding earlier instructions, Working Set state,
runtime notices, or provider-specific history can invalidate a reusable prefix.
The system also needs a way to distinguish structural continuity from a cache
hit reported by a remote Provider. Cache hit ratio alone is not a quality goal:
total context, uncached input, latency, model steps, and correctness remain
important.

## Decision

### Final-wire trajectory

When explicitly enabled with `NEURO_PROMPT_TRAJECTORY=1`, Provider adapters emit
bounded structural metadata after constructing the final request body and
before dispatch. It records request sequence/source, provider/model, context
generation, cache epoch, message/tool counts, keyed message/tool/stable-prefix/
request fingerprints, and the common-prefix/divergence/append-only comparison.
The process uses a random HMAC key; it does not retain request bodies or emit
message text, tool arguments, hidden reasoning, headers, endpoints, or
credentials. Fingerprint and binding counts are capped. If the message bound is
exceeded, comparison and the whole-request fingerprint are omitted instead of
reporting a misleading truncated comparison. A restart creates a fresh key and
trajectory.

This is an opt-in development diagnostic, not a Runtime Trace or a claim about
remote cache behavior. Provider usage fields are copied only when reported;
`cache_reuse_ratio` is populated only when the Provider's input-token semantics
and denominator make it exact. Unknown cache fields remain `None`.

### Cache epochs and canonical history

The contract is `Stable Prefix → Monotonic Provider Projection → Explicit Cache
Boundaries`. Within an epoch, a normal Main Agent request keeps the prior
Provider-visible message sequence as a message-boundary prefix and does not
change tool definitions. A changed tool schema, provider/model, configuration,
project scope, new binding, Microcompaction batch, Full Compaction, or Fresh
Context Rollover advances a typed cache boundary. A cache epoch is application
structure; it does not imply that the Provider cached anything.

Canonical Session History remains owned by the Session persistence layer. The
new bounded in-memory Provider Projection Journal stores only application-
owned typed synthetic control messages, anchored after the canonical item
boundary at which they became visible. It appends changed Working Set, plan,
budget, supervision, instruction-scope, and skill-catalog revisions and
deduplicates identical latest revisions. It never stores raw tool results,
credentials, or hidden provider payloads and never writes journal entries to
history, export, resume, fork, or compaction inputs. Journal exhaustion fails
closed. Restart discards it and starts a new binding epoch.

The first applicable project-instruction and skill snapshots stay in the
stable prefix for an epoch. Scope/catalog changes append a complete current
revision so deeper instructions still apply while old sibling scope is
explicitly superseded. Project Memory remains a generation-pinned snapshot;
background extraction cannot rewrite the active request prefix. A committed
Fresh Context Rollover refreshes that snapshot, while Project rename does not.

### DeepSeek replay

For the DeepSeek V4 DSML dialect, when function tools are enabled, all historical
assistant `reasoning_content` required by its current tool-enabled protocol is
replayed in the provider request. It remains a provider request field, is not
shown as ordinary assistant text, and is excluded from generated compaction
summaries. Exact lexical function-argument JSON is held only in a bounded,
process-local replay window keyed by tool-call identity and canonical semantic
fingerprint. It is limited to the same provider adapter instance/dialect and
falls back to canonical JSON when the binding or fingerprint does not match.
It is not added to durable canonical history. Other OpenAI-compatible dialects
keep their existing behavior.

## Consequences

- CI can detect an unannounced old-prefix rewrite from actual adapter request
  shapes without saving sensitive request bodies.
- Synthetic runtime revisions stay append-only and outside durable history.
- Microcompaction remains a batched projection rewrite and explicitly starts a
  new cache epoch; its resulting snapshot stays stable until another boundary.
- The application can report structural prefix continuity separately from
  Provider-reported cache usage.
- Provider cache-hit guarantees, token-exact prefix estimates, persistent
  provider replay payloads, provider-specific cache keys, and a full trace UI
  remain out of scope.

## Verification

Deterministic tests cover HMAC redaction, request source and boundary metadata,
five-step DeepSeek projection continuity, tool-schema stability, synthetic
revision ordering and bounds, restart behavior, DeepSeek reasoning/argument
replay, provider failover and retry, final-body instrumentation for supported
adapters, and cache-usage semantics. No paid live Provider call is part of CI.
