# ADR 0162: Fresh active-context rollover

- Status: Accepted
- Date: 2026-09-15
- Scope: CM3a bounded normal-agent context lifecycle control

## Context

The normal Agent keeps the canonical durable `SessionItem` sequence while
building each model request from that sequence plus refreshed synthetic
instructions, skills, Working Set, provider/runtime guidance, and other
binding-owned projections. Existing compaction can replace an old bounded
projection at its safe boundaries, but there is no explicit user/model
control for starting a completely fresh active context while continuing the
same session.

CM3a needs that control without making a second history, copying the Working
Set, changing provider affinity, or treating transient runtime context as
conversation history. The durable marker must also make a crash between the
control and the next model request deterministic.

## Decision

Add one monotonic `context_generation` integer to the existing `sessions` row.
Schema version 32 adds the column with a non-negative default and migrates
legacy schema-31 databases additively. `SessionStore` owns reading and
atomically advancing the marker; `SessionContextRolloverApplicationService`
is the application boundary. The marker is session metadata, not a copied
history sequence, summary, or Working Set row.

The normal composition binds a runtime-only `new_context` tool to the current
`ToolContext.session_id`. The tool has no model arguments, is exclusive, and
is not exposed through child/subagent bindings. It is marked
`side_effecting=False` because the existing flag describes workspace/shell
mutation; its bounded acknowledgement explicitly reports `durable_write`.
Before advancing the marker, the tool verifies that the configured output
limit can represent the largest valid acknowledgement. A too-small limit
therefore fails before mutation. A successful advance returns the generation,
`fresh_context=true`, and `durable_write=true` in a compact JSON result.

`AgentLoopRunner` retains the complete in-memory turn sequence for existing
finalization and persistence, but separately projects the active model
context. At the beginning of a run with a non-zero durable generation, the
active projection starts with the existing system messages and the current
turn's new user input; old durable items remain in the full sequence and are
not injected into that active projection. After a successful sole
`new_context` call, the same projection boundary is installed immediately:
the system prefix and current user message seed the next request, followed by
the new generation notice and later runtime/model/tool items. Instructions,
skills, and the current Working Set are rebuilt by the existing
`ContextBuilder`/Working Set path for that request.

The control must be the only tool call in its model step. A mixed batch is
rejected without advancing the generation. An unavailable, invalid, or
output-limited control also leaves the marker unchanged. The control result
itself follows the ordinary tool/event/finalization path when the turn is
completed; the session marker is committed before the next model request and
is therefore the recovery source of truth even if the process stops before
turn finalization. On a later run, the persisted generation installs the same
fresh projection before model execution. Existing unresolved-turn recovery
rules still apply; rollover does not auto-replay a provider request.

Rollover does not remove or replace compaction. Existing compaction remains
owned by its current safe-boundary gate, provider-window/accounting rules,
durable compaction rows, and recovery reconstruction. A new generation clears
the in-memory compaction projection for the prior active context; subsequent
compaction, if independently triggered at an allowed boundary, assesses only
the new active projection. Provider selection, failover, native context
affinity, permissions, verification, workspace/checkpoint state, and final
response commitment remain on their existing paths.

## Invariants and non-goals

- The session identifier and canonical ordered durable `SessionItem` history
  are unchanged. CM1 list/search/read remains the way to retrieve older exact
  public items on demand.
- The current CM2 Working Set row is read again for each request and is not
  copied, reset, or rewritten by rollover. Its synthetic model message, the
  rollover notice, refreshed instructions/skills, and other runtime-only
  messages are excluded from canonical history by the existing persistence
  filter.
- A rollover never changes provider identity, permissions, verification
  evidence, workspace authority, sandbox policy, or final-response truth.
  It changes only the active model projection and the durable generation
  marker.
- The control is bounded, session-scoped, and normal-agent-only. There is no
  automatic rollover policy, cross-session memory, embedding retrieval,
  semantic search, history schema/index redesign, artifact redesign, or
  removal of existing compaction.

## Compatibility and validation

The schema change is additive: existing sessions start at generation zero,
and existing history/Working Set/compaction data remains readable. Forked
sessions retain their existing history-copy behavior but start with the
default generation and do not inherit CM2 Working Set state. Focused tests
cover in-turn fresh projection, Working Set and canonical-history
preservation, pre-write output-limit rejection, and generation recovery
after an interrupted next model step. Ruff, format, focused mypy, and
documentation parity are the change-level checks; the repository's full CI
matrix remains the authoritative full validation gate.
