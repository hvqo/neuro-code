# ADR 0161: Structured session Working Set

[简体中文](../../zh-CN/adr/0161-structured-session-working-set.md) · **English**

- Status: Accepted
- Date: 2026-09-15
- Scope: CM2 bounded durable task-state projection

## Context

CM1 provides bounded, read-only access to the canonical ordered durable
`SessionItem` sequence. That sequence is the source of truth for conversation,
recovery, compaction, export, and fork semantics, but it is not a compact
representation of active task state. CM2 needs one small durable projection
for high-signal state without creating a second transcript or automatically
injecting old raw history.

## Decision

The application owns an immutable `WorkingSetSnapshot` with exactly six
sections: `goal`, `constraints`, `decisions`, `progress`, `unresolved_work`,
and `next_steps`. Each section has at most eight entries; each entry is
bounded, and the complete persisted snapshot and rendered context have
independent byte limits. Entries are either model-authored or history-backed.
A history-backed entry carries an opaque CM1 `SessionItemReference`; the
application resolves every reference against the current session before both
writes and reads. Malformed, stale, tampered, cross-session, and non-public
references fail closed. Hidden reasoning is never a Working Set field.

SQLite stores one `session_working_sets` row per session. The row contains the
current revision and canonical snapshot JSON; schema version 31 adds this
projection without copying `SessionItem` history. A missing row is the empty
revision-zero state for a legacy session. A present malformed row is an error,
not an implicit reset. Complete replacements use `BEGIN IMMEDIATE` and an
expected-revision check, so a stale writer cannot overwrite a newer snapshot
or partially update it. Session deletion cascades the row. Fork, import, and
normal subagent creation do not copy Working Set state: the child starts empty,
which is the safe lifecycle behavior because current CM1 references cannot be
proven to rebind to child history.

`SessionWorkingSetApplicationService` is the single application owner for
validation and configured explicit redaction. The canonical
`SessionApplicationService`, runtime composition, and model tool use this
same contract. Redaction is applied before a snapshot is persisted or exposed,
and is applied again at model projection boundaries.

Before every model request, `AgentLoopRunner` reads the current snapshot for
the runtime-bound session. A non-empty snapshot is rendered deterministically
as a bounded `Message` tagged `SyntheticReason.WORKING_SET` and appended by
`ContextBuilder` after conversation history as volatile tail context. It is
not added to the in-memory durable item sequence, is removed if supplied by a
caller, and is rebuilt after compaction projections. An empty snapshot adds no
message. Keeping it after conversation prevents frequently changing task state
from rewriting the stable project prefix.

`session_working_set` exposes only `read` and complete-replacement `update`.
The schema contains no session selector; `ToolContext.session_id` is the only
trusted scope. Updates require the returned revision and all six sections.
The tool declares `side_effecting=False` because the existing flag governs
workspace and shell mutation; its result metadata explicitly reports
`durable_write=true` for a committed session-state update. The tool definition
is nevertheless exclusive so concurrent model calls cannot race a durable
Working Set replacement through the scheduler. It never rewrites history,
invokes compaction, selects parent/child sessions, or receives a Working Set
from a parent runtime.

## Invariants and non-goals

Working Set is not canonical history. The durable `SessionItem` sequence
remains the only conversation source of truth, and CM1 list/search/read keeps
its existing visibility and reference rules. Working Set is not a compaction
summary: compaction owns its existing durable summary and recovery lifecycle;
Working Set is simply read again for the next request. Working Set is not
cross-session memory: every row, reference, application request, tool call,
and runtime projection is bound to one session.

Append-only history, provider-native preserved context, verification and
workspace/checkpoint state, permission and sandbox boundaries, redaction,
recovery, and parent/child isolation remain owned by their existing services.
CM2 does not add schema/indexing for history search, relevance eviction,
rollover, automatic summarization, tool-result eviction, artifact redesign,
cross-session browsing, embeddings, or a new CLI/TUI/ACP surface.

## Compatibility and validation

The new table is additive and existing sessions remain readable. Focused tests
cover empty legacy sessions, restart persistence, atomic revision-CAS updates,
stale and failed writes, bounded typed snapshots, malformed state, CM1
reference validation, redaction, non-durable synthetic injection, current
session tool binding, fork/subagent isolation, and schema migration. Relevant
architecture checks, Ruff, format, mypy, and documentation parity remain part
of the change gate.
