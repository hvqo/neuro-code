# ADR 0179: Execution Efficiency V1

[简体中文](../../zh-CN/adr/0179-execution-efficiency-v1.md) · **English**

- Status: Accepted
- Date: 2026-09-26
- Scope: Advisory execution phases and low-information exploration guidance in the main Agent turn

## Context

Runtime Trace showed that long repository tasks can spend most active time in
Provider requests while tools execute quickly. The existing scheduler already
runs consecutive parallel-safe calls from one model response concurrently,
but it cannot combine singleton calls emitted in separate model responses.
Changing tool scheduling or adding a planner would risk dependencies,
permissions, Prompt Cache continuity, and the established Supervisor boundary.

## Decision

**Efficiency feedback is advisory and separate from execution authority.** A
per-turn application controller observes bounded `ToolExecutionObservation`
facts and the safe scheduling mode of completed calls. It does not select,
merge, suppress, reorder, retry, or execute tools; it does not change
`max_steps`, reasoning effort, permissions, sandbox, verification, or
Supervisor decisions. Supervisor remains the sole loop-protection authority.

**Detect only repeated low-information exploration.** A candidate is one new,
successful `ProgressKind.EVIDENCE` call to a simple, parallel-safe repository
read/search tool (`read_file`, single-item `read_files`, `grep_many`,
`list_tree`, `glob`, or `git_inspect`). Multi-file `read_files`, multi-query
`grep_many`, errors, duplicate action/observation fingerprints, non-evidence,
exclusive tools, and multi-call batches do not count as singleton rounds and
break the streak. Two consecutive qualifying rounds, or completion of the
existing structured plan, cause one bounded `EXPLORE → ANALYZE` checkpoint
for the turn. The notice asks the model to
synthesize existing evidence, name concrete gaps, and batch independent
follow-up needs while retaining dependent steps. It is guidance, not a
correctness judgment or a step limit; newly produced evidence and justified
follow-up remain available to the model.

**The model declares evidence batches.** The runtime does not invent or merge
calls. A model response may request multiple independent reads/searches in one
step; the existing scheduler executes only calls whose executable tool
capability is parallel-safe, preserves result order, and keeps exclusive,
side-effecting, and interaction-control calls sequential. Work whose next step
depends on a result must wait for that result in a later model step.

**Execution phases are bounded context guidance.** A turn starts in `EXPLORE`.
The low-information checkpoint or completion of the existing plan enters
`ANALYZE`; observed workspace changes and verification enter `VERIFY`; successful verification can enter
`FINALIZE`, and every terminal path records finalization. Phase notices are
append-only synthetic user context. They are emitted only at phase boundaries,
stay outside durable Session history, and never rewrite the stable System
Prefix. No extra model planner/judge is called.
New evidence after an analysis checkpoint returns the phase to `EXPLORE`
without repeating the notice.

**Trace records facts, not decisions.** The existing ephemeral Trace event
stream receives a typed efficiency phase/reason, bounded streak/evidence
counts, and whether guidance was emitted. It receives no prompt text, tool
arguments, result bodies, fingerprints, or secrets. Trace consumes the event
after the policy decision and remains a read-only observability projection.

## Benchmark fixture

A deterministic repository-review fixture reads the same five review files
and returns the same idempotency finding in both runs. The fragmented baseline
uses five singleton evidence batches followed by a final request (6 model
requests, 5 tool calls, 5 batches, 1.00 tool/batch, 60 output tokens). The
adaptive provider uses two singleton reads, then one three-call batch after
the bounded notice (4 model requests, 5 tool calls, 3 batches, 1.67
tools/batch, 44 output tokens). Both report the same fixture cache-reuse ratio
of 0.80. Provider delay is simulated in-process; its measured reduction is a
fixture result, not a live-provider performance claim. The fixture asserts
identical findings, complete evidence coverage, append-only request messages,
stable System content, and unchanged durable history.

## Consequences

- Repository exploration may replace fragmented read-only round trips with a
  model-declared batch without weakening the existing security pipeline.
- The first low-information pattern adds one bounded context message; the
  original evidence remains in conversation and subsequent evidence is never
  filtered.
- Verification and finalization are phase boundaries, not batch triggers.
- Provider latency and output tokens can fall when the model follows the
  guidance; no fixed reduction target or production timing claim is made.
- Trace remains diagnostic. Efficiency state is per-turn and ephemeral.

## Validation

Deterministic tests cover singleton detection, duplicate/error/composite and
multi-call boundaries, new evidence after guidance, workspace/verification
phases, exclusive tool ordering, loop-protection compatibility, Trace
redaction, append-only Prompt Cache structure, durable-history exclusion, and
the representative repository-review before/after fixture.
