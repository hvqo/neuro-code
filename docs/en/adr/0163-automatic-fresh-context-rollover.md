# ADR 0163: Automatic fresh-context rollover

- Status: Accepted
- Date: 2026-09-15
- Scope: CM3b bounded automatic normal-agent context recovery

## Context

CM3a provides an explicit `new_context` control and a durable context
generation boundary. The existing normal-agent runtime also has an
authoritative `ContextPreflight` assessment and a safe-boundary automatic
compaction path. A bounded request can remain too large after the one
compaction that is allowed to preserve selected earlier information, even
though discarding the active projection would make a fresh request fit.

CM3b needs to connect those existing owners. It must not introduce a second
context threshold, summarizer, generation counter, history store, or transient
reset. Compaction and rollover have different purposes: compaction keeps a
bounded summary of selected older information, while rollover starts a new
active generation and leaves canonical history and external task state intact.

## Decision

`AgentLoopRunner` owns one bounded automatic recovery cycle for a normal,
finalizing user turn. `ContextPreflight` remains the only capacity decision
and uses the same request-shape, tool, output-reserve, safety-margin, and
provider-window accounting as the model request.

The sequence is deterministic:

1. Build the real request and run `ContextPreflight`.
2. `SAFE` proceeds normally. `UNKNOWN` preserves the existing behavior and
   does not invent capacity or a rollover threshold.
3. An irreducible `BLOCKED` request is finalized through the existing
   deterministic budget-limited path before compaction or rollover.
4. For `COMPACTION_REQUIRED`, invoke the existing automatic compaction gate
   once at its existing safe boundary. Rebuild the request from the resulting
   compatible projection and run `ContextPreflight` again.
5. If that projection is still `BLOCKED`, automatic rollover is eligible only
   before the first real provider request of the current user turn, when the
   durable controller, session and turn identity are available, the current
   generation has discardable active history, the request is reducible
   (`irreducible_tokens < capacity_tokens`), and the prospective fresh
   projection is strictly smaller. This excludes uncommitted tool results,
   assistant tool calls, and provider-native items from automatic disposal.
   A second automatic rollover attempt is not permitted in the same
   model-step cycle.
6. Run the same `ContextPreflight` against the exact prospective fresh
   request, including the current user, rebuilt Working Set/instructions,
   frozen tools, provider identity, output reserve, and safety margin. Only a
   `SAFE` preview may invoke the existing CM3a rollover controller with the
   current canonical boundary and pending turn anchor. A `BLOCKED` or
   `UNKNOWN` preview does not mutate durable state or call the provider and
   uses deterministic blocking. After a committed `SAFE` preview, rebuild the
   actual request and run one defensive final preflight before calling the
   provider; a remaining `BLOCKED` result uses the existing deterministic
   budget-limited finalization without another compaction or rollover.

The automatic action calls the application rollover controller directly. It
does not synthesize a model tool call or persist a fake `new_context` result.
The controller's monotonic generation, exclusive canonical boundary, pending
turn anchor, committed fallback, finalization promotion, and abandonment
cleanup remain the only durable rollover semantics. If the durable transition
cannot be committed, the runtime keeps the old in-memory projection and takes
the deterministic budget path.

The fresh projection reuses CM3a semantics: the system prefix and current
user request are retained exactly once; refreshed instructions, skills, and
the current CM2 Working Set are rebuilt by their existing owners; prior
generation ordinary items, compaction projection, synthetic runtime notices,
and provider-native preserved/reasoning state are not re-injected. The
canonical durable `SessionItem` sequence is unchanged, so older safe content
remains explicitly recoverable through CM1 `session_history`.

Provider origin handling remains tied to the real generation. After the fresh
boundary, a provider selected by failover may establish the durable origin for
native state produced in the new generation. Existing provider affinity and
native replay compatibility still reject stale foreign state. Permission,
sandbox, workspace/checkpoint, verification, final-response, turn recovery,
and parent/child binding boundaries remain owned by their existing paths.

Automatic policy metadata is added only to the existing preflight event
projection: bounded booleans identify eligibility, preview/attempt, and
committed fresh-boundary success. Raw context, prompts, provider payloads, and
secrets are not persisted in diagnostic metadata. A blocked or unknown
prospective preview is therefore distinguishable from a committed rollover
without adding a new event or durable record.

## Invariants and non-goals

- At most one existing automatic compaction and one automatic rollover are
  attempted for a model request/preflight recovery cycle. A later normal
  model step may start a new bounded cycle.
- `UNKNOWN` is not treated as zero capacity, infinite capacity, or permission
  to guess a threshold. An irreducible request is not rolled over.
- A successful automatic rollover is a durable CM3a transition in the same
  session; it does not delete compaction records, rewrite canonical history,
  duplicate the Working Set, or reset task, permission, verification,
  workspace, sandbox, provider, or finalization state.
- Automatic rollover is a pre-first-request policy only. Mid-turn pressure
  remains with existing compaction/block behavior until a future bounded tool
  result policy can prove recovery of uncommitted evidence.
- Cancellation or a crash at the pending boundary follows CM3a recovery. The
  unresolved turn is not replayed automatically, and finalization or explicit
  abandonment determines whether the candidate boundary is promoted.
- There is no user-configurable rollover threshold, generic context-policy
  DSL, new summarization model, cross-session memory, semantic retrieval,
  embedding index, automatic Working Set rewrite, automatic subagent rollover,
  UltraCode change, CM3b follow-on policy, or removal of explicit
  `new_context`.

## Compatibility and validation

The existing CM3a schema and ports are sufficient. CM3b adds no database
columns, compaction metadata, history index, or new durable counter. Existing
compaction source-count, source-fingerprint, provider, model, affinity, and
non-overlap compatibility rules remain authoritative; a new generation clears
only the old active in-memory compaction projection.

Focused regressions cover safe and unknown requests, one compaction without
rollover, insufficient compaction followed by a fresh provider request, a
fresh seed that remains blocked without mutation, irreducible blocking,
current-user and Working Set preservation, old-history/native-state exclusion
with CM1 durability, mid-turn tool/native evidence retention, cancellation /
reopen at the committed boundary, and failover rebinding of new native state.
Existing CM3a, compaction, preflight, provider, recovery, Working Set,
history, verification, and architecture checks remain part of the change
validation; the repository's full CI matrix is the final gate.
