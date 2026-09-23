# ADR 0173: Deterministic Microcompaction V1

[简体中文](../../zh-CN/adr/0173-deterministic-microcompaction-v1.md) · **English**

- Status: Accepted
- Date: 2026-09-23
- Scope: Deterministic model-facing projection cleanup before Context Preflight

## Context

Tool Result Guard bounds each result, but a long conversation can still retain
many old results. Full Compaction can summarize a larger historical range and
Fresh Context Rollover can start a new active generation; both have broader
rewrite semantics and cost. Clearing one old result on every request would
keep moving the prefix and repeatedly disturb prompt caching.

Microcompaction must reduce request size without changing canonical session
history or displacing the existing compaction, rollover, permission,
provider-affinity, recovery, verification, or artifact owners.

## Decision

**Microcompaction is a deterministic projection cleanup, not Full Compaction.**
It does not call a model, create a semantic summary, or write to the session,
artifact, audit, verification, or recovery stores. It copies the model-facing
`ModelContext` and replaces eligible tool-result bodies with one fixed,
bounded, generic marker. User and Assistant source messages remain untouched,
and each assistant call stays adjacent to its ordered results.

**The pressure decision is made against the request that will be sent.** The
normal Runtime order is Tool Result Guard → Microcompaction → Context Preflight
→ Full Compaction → Fresh Context Rollover. Microcompaction runs only for a
normal user turn with a known capacity and `COMPACTION_REQUIRED` preflight. It
also evaluates at the post-tool-batch safe point before existing full
compaction logic. The final preflight uses the projected context. If it is
`SAFE`, the Runtime skips Full Compaction and rollover; otherwise the existing
bounded paths proceed unchanged. Irreducible `BLOCKED` and unknown-capacity
requests retain their existing behavior.

**Only complete, old, runtime-confirmed successful groups are eligible.** A
group must be a contiguous assistant message with tool calls followed by one
matching `Role.TOOL` result per call in call order, all before the current user
message. The three most recent prior groups and every current-turn group are
protected. Error, media/binary, incomplete, duplicate, or unknown-status groups
are skipped. Runtime result statuses are learned only from terminal tool
events; after process restart old results are unknown and therefore stay
intact. Malformed context never authorizes a rewrite.

**One explicit trigger makes one batch.** The trigger is actionable context
pressure. A completed batch remains pinned for the same session and context
generation. Another batch requires a later pressure check plus a changed stable
prefix/compaction boundary or append growth of at least eight stable items or
2,048 estimated tokens. All then-eligible groups are compacted together. A
proposal is discarded unless it meets both minimums: 1,024 serialized item
bytes and 256 estimated tokens saved. This prevents per-request rolling
cleanup and avoids cache churn for negligible savings.

**Projection state is bounded and ephemeral.** `MicrocompactionRuntimeState`
tracks exact group fingerprints, stable boundary/prefix fingerprint,
compaction identity, and aggregate telemetry in memory under
`(session_id, context_generation)`. It scans at most 16,384 items, retains at
most 4,096 group fingerprints and 8,192 terminal result statuses, and does not
persist this optimization state. It also caps conservative serialized source
size at 8 MiB, nested values at 65,536 nodes/depth 32, and one assistant group
at 128 calls/256 content parts. After restart the durable history remains
authoritative; an empty snapshot and unknown statuses fail closed. A committed
Fresh Context Rollover resets the state. Full Compaction may change the active
projection but never the canonical source; a changed compaction identity
reconciles the pinned group set against the new projection.

**Telemetry contains aggregates only.** The existing Context Preflight event
may carry trigger reason, stable boundary, group/result counts, estimated
serialized bytes and context tokens before/after, savings, and a typed `NOOP`
reason; source-limit values are marked as saturated. It never contains tool-result text, arguments, paths, artifact ids, or
secret values. Byte estimates use deterministic serialized session items;
token estimates are provider-neutral approximations, not provider-reported
usage.

Microcompaction follows `Stable Prefix → Append-only Conversation → Volatile
Tail`. Context reduction must weigh correctness, token reduction, and prompt
cache preservation together. Historical projection rewrites happen in a
single bounded batch at an explicit pressure boundary, not as one-result-per-
request cleanup. Provider-specific cache keys/breakpoints, semantic retrieval,
LLM summaries, full Runtime Trace, and TTFT telemetry are outside this ADR.

## Consequences

- Model requests can omit several old successful tool results while Session
  History and existing history/artifact retrieval retain the original facts.
- Preflight measures the same microcompacted projection the Provider receives.
- A safe microcompacted request bypasses Full Compaction and Fresh Context
  Rollover; an insufficient projection falls through to those existing paths.
- Restart does not restore this optimization snapshot or trust historical
  tool status, preserving a deterministic, fail-closed recovery path.
- See [ADR 0164](0164-model-facing-tool-result-guard.md), [ADR 0160](0160-durable-history-addressability-and-rehydration.md), [ADR 0162](0162-fresh-context-rollover.md), and [ADR 0172](0172-project-memory-v1.md).
