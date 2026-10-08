# ADR 0202: DW5b-1 durable Workflow Activity protocol

[简体中文](../../zh-CN/adr/0202-dw5b1-durable-workflow-activity-protocol.md) · **English**

- Status: Accepted internal foundation
- Date: 2026-10-07
- Scope: protocol, persistence and bounded Interpreter consumption; no real Activity adapter

## Interpreter and Activity ownership

The Interpreter initializes a READY step in one tick. The next tick resolves
DW1 inputs and publishes a `WorkflowActivityInvocation`: invocation, Step WAITING,
Run WAITING and an ACTIVITY journal fact commit in one SQLite transaction.
`advance_once()` returns immediately. Later nonterminal ticks return
`activity_waiting`, `progressed=False`, without writes, reservations or dispatch.

An external owner explicitly uses the narrow `WorkflowActivityStore` port.
Its owner/fence/revision is separate from the Workflow control owner. No daemon,
timer, scheduler, model, tool, filesystem mutation or long-running call is added.
Ownership here coordinates a future adapter; it does not grant Permission,
Sandbox, Workspace, Branch or Repeat authority.

## Identity and immutable evidence

The invocation ID reuses SHA-256 of Run + StepIdentity + input fingerprint.
StepIdentity includes step, iteration and item key. The immutable invocation
fingerprint additionally binds definition, parent session, ActivityKind and exact
bounded canonical request. DW1 reference schemas validate resolved inputs;
literals and artifact bindings retain exact declared values. A retry's timestamp
does not change request identity. Same ID/different request conflicts.

`WorkflowActivityResult` binds invocation/request/kind, terminal state, source
ID/fingerprint, UTC terminal time, actual usage and (only for COMPLETED) canonical
output. Source identity is provenance, not execution or verification authority.
COMPLETED output must satisfy existing `activity_output_schema`; schema-valid
JSON alone cannot replace the durable result. There is no new output schema.

## State and side-effect-start boundary

`READY → CLAIMED → RUNNING → COMPLETED / FAILED / BLOCKED / INDETERMINATE`.
CLAIMED can also terminate as FAILED/BLOCKED/INDETERMINATE before dispatch.
COMPLETED requires the RUNNING boundary. Revision CAS, SQLite writer serialization
and an irreversible state trigger permit only one claimant; owner fence is
independent of Workflow owner fence. There is no generic reclaim/reset/retry API.

READY means only that an invocation exists. CLAIMED means an owner and reservation
exist, but effects have not started. The adapter must durably mark RUNNING before
crossing its underlying side-effect-start boundary. A crash just after that write
can mean nothing actually started, but the protocol conservatively assumes effects
may have started. Process disappearance is never evidence for retry. Future
adapters must prove their own operation's outcome or record INDETERMINATE.

The legal adapter sequence is: atomically claim and reserve budget; commit RUNNING;
confirm that commit succeeded and this is an authorized first dispatch rather than
historical replay; only then invoke the underlying ADOPT/VERIFY/REPAIR operation.
Before RUNNING, FAILED/BLOCKED/INDETERMINATE results must report exact integer zero
for generated tasks, model/tool calls and input/output tokens. Positive or unknown
values are rejected before accounting or state writes; pre-dispatch wall time may
be nonzero. Domain reconstruction also rejects such invalid persisted facts, even
when their fingerprints and journal linkage have been recomputed.

Zero pre-dispatch execution usage is a necessary guard, not proof that no external
effect occurred. Future adapters must still prove first-dispatch identity, enforce
meaningful pre-dispatch ceilings and define durable underlying recovery semantics.
The generic protocol cannot substitute a start ACK replay for those proofs.

Exact claim/start ACK replays return an existing fact; a RUNNING replay is not
permission to execute twice. Terminal replay checks persisted invocation, result,
schema, budget and journal integrity, without requiring a live execution process
or current Activity owner. Conflicting result/source remains rejected.

## Schema 40 and transactions

Schema 39→40 adds `workflow_activity_attempts`, immutable
`workflow_activity_results` and a bounded local `workflow_activity_events`
journal (at most four lifecycle facts per invocation). The existing output CHECK
also accepts `activity`; migration preserves old rows/fingerprints verbatim,
including `fake_activity`. Reads verify indexed identity, snapshot digest,
publication generation linkage, local journal continuity and terminal evidence.
Immutable triggers reject terminal changes/deletion and invocation mutation.

Workflow ACTIVITY facts discriminate publication/result consumption. Activity-local
facts record claim/running/terminal revisions and snapshot fingerprints, without
raw output, filesystem data or secrets. Run budget facts retain existing
RESERVED/CONSUMED discriminators. This is snapshot + recovery evidence, not a
second event-sourcing framework or Runtime authority.

Publication is atomic with Step/Run/journal. Claim and existing Run-level budget
reservation are atomic. Result settlement, terminal attempt, immutable result and
journals are atomic. Any failure rolls all participating facts back. A commit
followed by ACK loss can be reopened and read without another invocation or charge.

## Budget and terminal consumption

The port reuses `BudgetAmounts`, `BudgetReservation` and Run ceiling. Activity
reservations and terminal usage must have `generated_tasks` equal to exact integer
zero: Activities do not publish Task DAGs. Nonzero/unknown values fail closed on
construction, writes and recovery reads. DW3 retains generated-task publication
accounting; the global `BudgetAmounts` contract and schema 40 remain unchanged.
An owner
must reserve known upper bounds at claim before dispatch; exceeding the ceiling
rolls back claim. Publication itself has no model/tool consumption. Adapters must
explicitly report `None` for unknown usage rather than filling missing usage with
zero. Outstanding reservations remain outstanding after crash/restart.

Terminal settlement records actual usage even if it exceeds a reservation/ceiling.
Unknown committed usage or ceiling overrun puts the Run in NEEDS_ATTENTION;
late accounting cannot reopen a cancelled/failed Run. Replay never charges twice.
Existing explicit ledger reconciliation can refine unknown fields without changing
known consumption or rewriting the immutable original result.

On a later tick, COMPLETED produces `OutputKind.ACTIVITY`, Step COMPLETED, Run
RUNNING and an ACTIVITY consumption fact atomically. The transaction revalidates
the exact durable result and budget. FAILED consumption atomically fails Step/Run;
BLOCKED/INDETERMINATE require attention. No automatic repair/retry follows.
Missing invocation for a waiting step is an integrity failure. Consumption ACK
loss resumes from persisted step/output, never by executing the Activity again.

## Compatibility and exclusions

Legacy FAKE_ACTIVITY output remains readable. The former deterministic fake can
remain a pure test helper but is no longer the default production Activity path.
Tests explicitly drive an external owner. Branch/Repeat/Map, DW3 publication,
DW4 projection/adoption and exact Profile/capability binding remain unchanged.

No ADOPT, VERIFY or REPAIR adapter is implemented. Result Adoption,
VerificationTracker, AgentRuntime, Task DAG scheduler/Leader/Worker, Swarm,
Replan and UltraCode are unchanged. Neither COMPLETED Activity nor valid output
means verification PASS or Workflow COMPLETED. DW5b-2 must supply an adapter's
durable source checks, real authority boundary and safe reconciliation separately.

## Validation

Real SQLite tests cover atomic publication/claim/settlement/consumption rollback,
ACK loss and reopen, competing processes without timing sleeps, stale owner/fence,
immutable terminal evidence, request/result tamper, typed schemas, unknown/overrun
budget, cancellation before dispatch, historical replay, and schema-39 fake output
compatibility. DW5a control and existing Workflow/Task DAG/Swarm/UltraCode gates
remain required.

P1 regressions cover every pre-dispatch failure state and model/tool/token dimension
(positive/unknown rejection, zero usage and nonzero wall time), Activity generated-task
ownership, zero writes on rejection and rehashed snapshot/result/journal/ledger tamper.
RUNNING unknown usage, overrun, terminal replay and reconciliation retain their
existing accounting and recovery tests.
