# ADR 0197: DW2 durable Workflow state and persistence

**English** · [简体中文](../../zh-CN/adr/0197-dw2-durable-workflow-state.md)

- Status: Accepted persistence foundation
- Date: 2026-10-06
- Scope: durable control facts; no Workflow execution path

## Identity and storage

DW1 definitions remain immutable. The existing session SQLite database upgrades from
schema 35 to 36 inside the serialized migration transaction. Five tables hold
`workflow_definitions`, `workflow_runs`, `workflow_step_instances`,
`workflow_budget_reservations`, and `workflow_transition_journal`. The existing
`SqliteSessionStore` composes one `WorkflowsMixin` implementing the canonical
`application.ports.workflow_state.WorkflowStateStore` protocol. No bootstrap,
Runtime, scheduler, CLI or TUI consumer is wired to this port.

A definition is keyed by SHA-256 of canonical IR and stores exact canonical text,
IR schema version and compiler version (currently 1). Insert repeats are accepted
only when every stored field agrees. SQL update triggers additionally protect
immutable definitions and journal records. Definition id names are descriptive;
they may have multiple immutable fingerprints. `run_id` independently identifies
one use of a definition and cannot equal that definition fingerprint. Its parent
session and input fingerprint are immutable. Inputs/results are not evaluated.

Step identity is `(step_id, iteration, item_key)`, hashed from a deterministic
JSON tuple. Iteration 0 denotes an unscoped step; declared Repeat children use
1–3. Map template instances require an opaque item key. The repository checks
these scopes against the stored definition without expanding or executing it.
Each run is bounded to 256 step instances and 256 budget reservations.
Journal facts are bounded to 32 KiB each and paginated; there is no event-count
ceiling that could discard late accounting or prevent cancellation. Artifact results are bounded integrity references, not executable
paths, grants, or proof of successful adoption.

## Snapshot, journal and atomicity

Run statuses are `READY`, `RUNNING`, `WAITING`, `COMPLETED`, `FAILED`, `CANCELLED`
and `NEEDS_ATTENTION`. A run snapshot owns generation, owner/fence, control position,
UTC accounting timestamps, status and bounded diagnostic facts. Status-only
updates preserve control position; an explicit position replaces it. Child step and
reservation rows belong to that snapshot. Reads use one WAL transaction so readers
cannot combine old run state with new child rows.

Every successful mutation uses `BEGIN IMMEDIATE`, generation CAS and one journal
insert in the same transaction. Failures roll back all tables. The journal contains
bounded typed facts: create, claim/resume, state/step transitions, branch choices,
iteration, reservations, consumption and reconciliation. Branch decisions cannot
be rewritten and Repeat iteration facts advance one round at a time. These checks
record declared control facts; they do not evaluate conditions or publish Task DAGs.

This is snapshot plus audit evidence, not full event sourcing: recovery loads the
snapshot, not a second interpreter rebuilding authority from arbitrary events.
Journal pages are bounded to 100. A request id is unique within a run and fingerprints
its exact canonical payload, including CAS expectations and timestamp. Retrying the
same payload returns `replayed=True`, the original committed generation, and the
current snapshot; it neither re-applies the command nor advances generation. Reusing
an id with a changed payload fails. Callers must retain the original request identity
and payload across retries rather than inventing new ids after uncertain commits.

## Owner fence and crash recovery

Claim is explicit CAS, with both expected generation and previous owner fence. The
first claim changes READY to RUNNING. Every subsequent claim increments the fence
and records NEEDS_ATTENTION, including same-owner restart claims. Old owners cannot
advance with a stale fence. No clock-based lease expiry guesses that an old process
or side effect has stopped. Taking ownership preserves all steps, running/waiting
facts, results and outstanding budget; it does not reset or replay work.

The fence protects durable control writes only. It does not promise exactly-once
shell/tools, stop an external process, or grant Permission/Sandbox authority.
Future recovery must reconcile evidence before any effect can safely be retried.
Normal terminal states cannot advance, but identical request retries still work.
Late consumption/reconciliation may update their ledger without reopening the run.
Cancel/failure can retain unfinished steps and reservations for recovery evidence;
completion rejects outstanding steps, reservations and unknown budget usage.

## Durable budget ledger

The ledger covers generated tasks, model calls, tool calls, input/output tokens and
integer wall milliseconds. Run ceilings default to the DW1 declaration and may
only narrow it. They do not allocate or widen global/Profile grants; intersection
with actual Runtime ceilings and actual consumption wiring remain future work.

A stable reservation id stores known upper bounds and its UTC creation timestamp.
Settlement records exact usage and settlement time. Pending reservations also
report unknown consumed totals; reserved capacity is not a zero-consumption fact. Explicit zero is permitted only
as a supplied accounting fact; missing/unknown usage is `None` and propagates to the
aggregate. Unknown consumption persists and blocks further reservations/progress
until a separate typed reconciliation fills only unknown dimensions. Already known
values cannot be silently changed. Actual overruns are persisted (rather than
rejected and lost), mark NEEDS_ATTENTION and prevent further normal progress.

Reservation/settlement timestamps plus durable wall-millisecond amounts support
future accounting across process restarts. DW2 does not start clocks, measure real
Model/Tool calls, or infer elapsed consumption from a new process's monotonic clock.
A crash with unresolved reservation retains it; it never releases it as zero.

## Verification and deferred work

Database regressions cover migration rollback/old database upgrades, real competing
OS processes, stale generation/fence, duplicate/conflicting requests, snapshot and
journal rollback, immutable records, step scope, branch/iteration facts, bounded
ledgers, unknown/overrun accounting and reopening SQLite. Tests also assert that no
DAG/session task is published. Session deletion cascades run-owned facts; immutable
definitions remain available. Workflow state is not copied by session export/fork
and is not included in FTS.

DW3 still owns Workflow expansion and atomic Task DAG publication, effect adapters,
output projection, Runtime accounting and recovery proof. DW2 introduces none of
those paths. DW1 deep typed-IR hardening remains tracked separately in Issue #165.
