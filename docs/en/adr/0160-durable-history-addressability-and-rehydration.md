# ADR 0160: Durable history addressability and bounded rehydration

[简体中文](../../zh-CN/adr/0160-durable-history-addressability-and-rehydration.md) · **English**

- Status: Accepted
- Date: 2026-09-14
- Scope: CM1 durable session-history addressability and read-only rehydration

## Context

Neuro Code already persists the ordered `SessionItem` sequence in the
session store.  Runtime recovery, compaction, export, and fork semantics use
that sequence as their canonical durable state.  Existing session search is a
session-level discovery projection; it is not an addressable, exact reader for
individual user, assistant, or tool-result items.

CM1 needs a model-facing way to discover and retrieve older safe conversation
text without copying the transcript into another store or placing the whole
history in every model request.  The boundary must remain read-only,
session-scoped, bounded, and safe across restart and fork.

## Decision

The application session-item query owner provides bounded `list`, `search`,
and `read` operations.  They load the canonical ordered durable sequence on
demand and derive compact projections in memory; no history table, FTS
document, or second transcript is added.

Each public item receives an opaque `shr1_` reference derived from its
one-based durable ordinal, full canonical item fingerprint, and a digest of
the current session identity, with a format checksum.  Resolution validates
the token against the bound session, the current ordinal, and the current
item fingerprint.  Append-only persistence keeps an unchanged prefix stable;
cross-session, stale, malformed, or tampered references fail closed.  Raw
session identifiers are never put in references or tool arguments.  A fork
has a different session scope and therefore cannot resolve a parent
reference.

The public projection contains only non-synthetic `USER`, `ASSISTANT`, and
`TOOL` messages.  It exposes redacted visible text and a bounded tool name,
but not system messages, synthetic runtime context, provider-native preserved
items, hidden assistant reasoning, tool-call arguments, or other internal
payloads.  `list` is newest-first metadata, `search` is a bounded textual
match over that safe projection, and `read` returns one exact safe item text
chunk at a UTF-8 boundary.  Page size, query size, preview size, read size,
and tool output are independently bounded.

One normal local `session_history` tool exposes these operations with
`side_effecting=False`.  Its schema contains no session identifier.  The
runtime supplies the trusted current session identity through the internal
`ToolContext` binding, and the tool accepts only references previously
returned for that session.  The existing tool executor remains responsible
for final output redaction and the normal execution boundary.

No query operation writes session state, events, compaction rows, notes,
artifacts, or recovery records.  Reopening a store simply re-derives the
projection from durable items.  If another lifecycle operation replaces an
item so its canonical fingerprint no longer matches, an old reference is
stale rather than silently resolving to different content.

## Invariants and non-goals

Durable item ordering, append-only prefix validation, turn finalization,
recovery, compaction, provider affinity, redaction, permission and workspace
boundaries, verification state, and subagent isolation remain owned by their
existing services.  CM1 does not add a working-set abstraction, relevance
eviction, rollover, notes or summary provenance, artifact redesign,
cross-session browsing, parent-child history browsing, provider accounting,
embedding retrieval, or a new public CLI/TUI/ACP history surface.

## Compatibility and validation

The existing `SessionStore` schema and canonical persistence methods remain
unchanged.  Focused tests cover stable append references, malformed and
cross-session rejection, bounded pagination/search, visible user/assistant/
tool-result text, hidden/native/synthetic exclusion, UTF-8 large-item reads,
fork isolation, runtime session binding, read-only behavior, and registry
wiring.
