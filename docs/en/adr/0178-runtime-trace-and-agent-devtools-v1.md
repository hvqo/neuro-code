# ADR 0178: Runtime Trace and Agent DevTools V1

[简体中文](../../zh-CN/adr/0178-runtime-trace-and-agent-devtools-v1.md) · **English**

- Status: Accepted
- Date: 2026-09-26
- Scope: In-memory Agent runtime diagnostics, Textual trace inspection, and metadata-only export

## Status

Accepted.

## Context

Session history explains what was said and which durable tools ran, but it is
not an operational timing view. Diagnosing slow turns, repeated model steps,
provider retries, context pressure, permission waits, and serial tool batches
requires bounded lifecycle facts without retaining prompts or tool payloads.
Ordinary logs do not provide a stable hierarchy or a useful turn-level
reduction, and they must not become another source of runtime truth.

## Decision

**Trace is a read-only projection, not history or authority.** Runtime emits
existing lifecycle events plus a small set of ephemeral timing events. The
application-owned `TraceCollector` reduces those facts into trace, turn, step,
request, provider-attempt, tool-batch, tool, context, replan, verification,
finalizer, and subagent records. It does not make execution decisions and does
not enter prompts, Project Memory, compaction sources, recovery facts, or
durable Session history.

**Observe first, interpret later.** Runtime reports monotonic durations and
typed outcomes. Summaries, longest operations, parallel overlap, cache usage,
and timelines are calculated from those facts by the collector. No LLM
interprets or scores a trace. The trace ID identifies one observed turn;
request IDs and tool-call IDs reuse existing identities. A child trace may
reference its parent trace without merging their records or histories.

**Timing labels describe only observed boundaries.** Provider TTFT is the
provider request start through the first response output event (text,
reasoning, or tool output). User-visible TTFT is TUI turn acceptance through
the first non-empty visible text delta. Request duration and stream duration
are measured separately. Tool permission wait is `TOOL_REQUESTED` to
`TOOL_STARTED`; execution duration is `TOOL_STARTED` to a terminal tool event.
Context build, verification, finalizer, and replan spans use monotonic time.
The system does not infer private network/connect timing.

**Retention and rendering are bounded.** The in-memory collector retains at
most 32 turns, 8,192 records total, and 2,048 records per turn. The ledger
renders at most 48 rows at a time. Export is metadata-only and capped at 4 MiB;
it supports JSON and JSONL. Restart clears trace data because it is diagnostic,
not durable truth.

**Privacy is allowlist-based.** Trace records may contain safe provider/model
labels, IDs, status/error categories, durations, token/cache counts, context
generation and cache-boundary facts, tool names, byte counts, truncation and
artifact flags, and bounded provider-attempt summaries. They never retain API
keys, authorization data, full prompts, hidden reasoning, tool arguments,
tool-result or shell-output bodies, secret URLs/query strings, or raw provider
error messages. Export uses the same projection.

**Collection is fail-open.** Collector and diagnostic delivery failures do
not change Agent results. Diagnostic events are delivered only to the active
interface, are not returned in `AgentRunResult.events`, and are not persisted.
CLI JSONL filters these DevTools-only events to preserve its existing public
event stream. Existing provider requests, cache-boundary semantics, Session
history, permissions, workspace, sandbox, verification, and compaction remain
authoritative.

## Consequences

- `/trace` opens a paginated, searchable Textual ledger with an inspector,
  timing and usage summaries, and a timeline based on measured spans.
- `/trace export` copies metadata-only JSON; `/trace export jsonl` copies
  metadata-only JSONL.
- Provider TTFT, visible TTFT, token/cache usage, retries/failovers, permission
  wait, execution time, parallel overlap, context operations, replans,
  verification, finalization, and surfaced subagent runs can be compared in
  one turn view.
- Trace retention is process-local. OpenTelemetry, persistent trace storage,
  provider-private network timing, automatic efficiency recommendations, and
  LLM-generated trace summaries remain out of scope.

## Validation

Tests cover hierarchy, model timing and usage, provider retries/failover,
parallel tool batches and permission wait, context and compaction events,
rollover, replan, verification/finalizer/subagent spans, failure/cancellation,
retention, redaction/export, ephemeral delivery, unchanged model projection,
CLI event filtering, and a 1,100-record paginated TUI trace.
