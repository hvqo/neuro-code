# ADR 0203: DW5b-2 real Workflow ADOPT adapter

[简体中文](../../zh-CN/adr/0203-dw5b2-real-workflow-adopt-adapter.md) · **English**

- Status: Accepted internal adapter
- Date: 2026-10-08
- Scope: real ADOPT only; schema remains 40

## Exact source and identity

`WorkflowAdoptActivityAdapter` is an explicitly invoked application service, not
an Interpreter callback or a scheduler. A real ADOPT declaration reserves the
input name `source` for a direct DW1 `ResultRef`: `TaskBatch.tasks` or `Map.items`.
Only the entire producer batch is selected. Literal JSON, caller DAG IDs, latest
DAG selection, member selectors and Repeat aggregate selectors cannot grant
adoption authority. A direct producer inside Repeat uses the invocation iteration;
an outer dominating producer uses iteration zero, matching DW5a resolution.

The resolver verifies the immutable declaration, completed producer StepIdentity,
consumed PROJECTION output index and consumption journal. It reconstructs the DW1
output shape from the invocation's frozen resolved input only to check the exact
consumed output fingerprint; business response content is never parsed for source
identity or success. The index supplies the projection ID/fingerprint and the
producer identity supplies the deterministic DW5a expansion ID. Live execution
then uses `WorkflowCompletedDagSourceAdapter` to validate exact Run, Expansion,
Projection, DAG/node generations, parent session and preserved lease facts.

The deterministic `adopt-<sha256>` identity binds Run, invocation, StepIdentity,
request fingerprint, exact Workflow source reference and parent session. This
reuses `WorkflowAdoptionSourceRef` and `ResultAdoptionRequest`; no fake Swarm row,
identity alias or second adoption engine is introduced.

## Authorized dispatch and bounded operations

The factory constructs the existing `ResultAdoptionApplicationService` using the
existing parent binding and mutation port. Its normal capability, worktree,
checkpoint, three-way, path, protected-file, symlink, conflict and sandbox checks
remain authoritative. All writes use that mutation port, never adapter filesystem
writes or shell commands. Interpreter ticks only publish or consume facts.

For a fresh READY invocation, source/parent authority is checked, then claim and
budget reservation commit atomically. Only that call's successful claim and RUNNING
commit authorize first dispatch. Historical claim/start ACK replay is not dispatch
permission. CLAIMED under another owner returns busy, without borrowing its token.
The adapter has no generic Activity reset, retry or takeover.

Before each mutation-port operation, a bounded wrapper checks the Run is still
WAITING for this invocation, the frozen plan/session/target matches, a new durable
target dispatch revision exists, and operation/wall ceilings have headroom. Default
reservation is 64 operations (the existing maximum target count) and 30,000ms;
composition may choose narrower bounds, with at most 300,000ms wall time. Deadline
is anchored to the original RUNNING timestamp, never renewed on restart. Target
revisions conservatively bound prior dispatch attempts, including uncertain crashes;
they are not converted into alleged exact usage. A historical APPLYING target does
not alone authorize another port call. Desired-image observation can finish it
without another write. Exhausted/uncertain ceilings preserve RUNNING and outstanding
reservation, mark NEEDS_ATTENTION, and do not automatically retry.

Wall time bounds dispatch starts, not an already running platform mutation. The
underlying permission/workspace/sandbox port retains its own execution limits.
There is no new timer, polling loop or unbounded operation budget.

## Terminal proof and reconciliation

The narrow `WorkflowAdoptionStore.reconcile_workflow_adoption` reads the actual
underlying terminal record in the same SQLite transaction as Activity settlement.
It does not accept an arbitrary source hash or fake Activity owner. The bound
ADOPT path cannot use generic `finish_workflow_activity` to invent a terminal fact.
Normal recovery reads also verify the underlying proof.

Proof binds adoption ID, immutable plan fingerprint, terminal state/version,
ordered target/evidence digest, `parent_workspace_changed`, terminal timestamp and
usage. COMPLETED requires applied targets with desired-image evidence. The Activity
result additionally binds exact invocation/request. Mapping is:

| Adoption | Activity | Meaning |
|---|---|---|
| COMPLETED | COMPLETED | DW1 output reports `status=completed` and the durable changed flag, including legitimate false |
| CONFLICT | BLOCKED | Parent/overlap conflict is not success; no automatic repair |
| FAILED | FAILED | Underlying failure retained |
| INDETERMINATE | INDETERMINATE | Uncertain effects require attention |
| Nonterminal / busy | No terminal result | Existing core ownership and forward recovery remain authoritative |

Pre-plan validation errors remain errors, not fabricated terminal adoption evidence.

Schema 40 has no independently persisted dispatch-measurement receipt. Target
revisions bound attempts but do not prove how many mutation-port calls happened:
a crash can precede dispatch or follow a write before its ACK. Neither revisions
nor an adapter-local counter establish durable measured wall time. We therefore
remove the public `usage` argument entirely rather than accept caller amounts,
ordinary receipts/dataclasses or self-hashed execution identities as proof.

Both normal first execution and historical reconciliation retain operation/wall
usage as `None`; an empty frozen plan independently proves zero operations, but
wall usage remains unknown. Generated tasks, model calls and tokens are zero.
There is currently **no trusted known operation/wall settlement path**. Successful
adoption still yields a durable COMPLETED Activity, but unknown accounting puts
the Run in NEEDS_ATTENTION and prevents automatic Interpreter consumption. A
separate authorized, evidence-backed ledger reconciliation and explicit resume
are required before a later Interpreter tick can consume that fact. This is a
conservative product limitation, not an assertion of precise usage or automatic
recovery. A future trusted execution-measurement seam would be necessary to enable
known first-execution accounting; this repair does not create one or a second
ledger. Existing unknown/overrun accounting and cancelled-Run behavior are retained.
Reconciliation can refine only unknown ledger fields; immutable result/proof
continues to record the original unknown usage.

Terminal reconciliation is owner-independent historical fact reading. It checks
exact source/parent binding and persisted integrity, without requiring live
Projection validation, lease, worktree, checkpoint or original parent HEAD. It
cross-checks the immutable SQLite Expansion and publication journal, consumed
output, Projection fingerprint/source snapshot, exact DAG generation/definition,
frozen members and worker identities. Rehashing a modified adoption plan cannot
replace those independent anchors. Missing history fails closed. No new ownership
or workspace mutation occurs. Terminal recovery reads enforce the same provenance
and conservative-usage invariants. Nonterminal recovery still validates live
sources and parent identity, then uses existing core owner liveness/lease/CAS.
Another live core owner remains busy.

## Crash boundaries

| Window | Recovery |
|---|---|
| RUNNING committed, no adoption fact | No redispatch based on RUNNING replay; NEEDS_ATTENTION with reservation retained |
| Plan committed, before mutation | Same adoption ID/plan, live validation and existing core ownership recovery |
| Mutation happened, ACK lost | Existing target CAS and desired-image observation; no duplicate write to that desired image |
| Adoption terminal, Activity result absent | Read terminal proof and atomically settle; no live-resource requirement or mutation |
| Activity result or Interpreter consumption ACK lost | Exact durable replay; no repeated settlement or adoption |
| Third-party parent change | Existing three-way/conflict/indeterminate checks; never overwrite unrelated/new content |

Settlement failure rolls back budget, attempt, result and journal together; the
underlying adoption terminal fact remains available for the next reconciliation.
No cross-database transaction or exactly-once external filesystem promise is made.

## Compatibility and exclusions

Schema 40 is unchanged; no migration or historical rewrite. Generic DW5b-1 protocol
fixtures without the reserved source binding remain readable. Swarm/UltraCode use
the same unmodified Result Adoption engine, owner and recovery semantics. DW3 task
accounting, Interpreter two-phase behavior and Runtime are unchanged.

COMPLETED adoption and `parent_workspace_changed` are not verification PASS. ADOPT
adapter settlement does not consume output, advance a Step/Run, publish a DAG or
complete a Workflow. The existing ledger alone may put a Run in NEEDS_ATTENTION
on uncertain accounting. Later Interpreter ticks consume the Activity fact.
VERIFY, REPAIR, completion requirements, Planner, CLI/TUI wiring and an automatic
adapter dispatcher remain future stages.

## Evidence

Focused tests use real SQLite, temporary Git repositories and the existing shared
adoption engine with injected workspace mutation ports. They cover TaskBatch/Map,
real parent file contents, unrelated dirty preservation, unchanged output, exact
source/parent rejection, stale resources, concurrent owners, dispatch ceilings,
terminal proof tamper, rollback, cancellation, ACK-loss windows and zero duplicate
mutation. Existing Activity, publication, projection, Interpreter, adoption,
Swarm/Replan/UltraCode, security and migration regressions remain required.
