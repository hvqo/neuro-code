# ADR 0203: DW5b-2 real Workflow ADOPT adapter

[简体中文](../../zh-CN/adr/0203-dw5b2-real-workflow-adopt-adapter.md) · **English**

- Status: Accepted internal adapter
- Date: 2026-10-08
- Scope: real ADOPT only; schema 41 (measurement evidence)

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

## Trusted execution accounting

Schema 41 adds three immutable evidence tables: `workflow_adoption_executions`,
`workflow_adoption_dispatch_events` and `workflow_adoption_measurements`. These are
execution evidence, **not a second budget ledger**. No existing adoption records
or schema-40 Activity results are rewritten or retrospectively made precise.

`execute_workflow_adoption()` is a narrow first-dispatch execution boundary. It
atomically commits CLAIMED → RUNNING and an exact execution identity, binding the
invocation/request, deterministic adoption ID, reservation, owner/fence and running
revision. An existing RUNNING attempt cannot enter this boundary, even with its
original owner token. It accepts the composition-owned mutation dependency and a
controller callback, never amounts, caller timestamps for usage, or a receipt.
The callback runs the **same** Result Adoption engine with an instrumented port.
The live scope additionally requires a process-local, non-serializable authorization
issued only after that first CAS. Constructing a scope/receipt from database IDs or
reopening the process cannot create a new measurement writer. The authorization is
revoked on scope exit; it alone does not prove a call, so actual entry/ACK measurement
and durable identity checks are still required.
Dependency injection is trusted composition, not an untrusted plugin API; an
arbitrary process able to replace the mutation implementation or rewrite all DB
facts is outside this contract. Ordinary hashes are integrity checks, not authority.

The repository-owned scope writes an intent before dispatch, directly invokes
`WorkspaceMutationPort.apply()`, and writes a separate return/exception ACK. An
ordinary exception after actual port entry counts as a call, regardless of whether
the filesystem changed. Pre-dispatch validation/preparation counts no tool calls.
Intent alone proves no actual entry. Target revisions only detect missing measured
boundaries and conservatively enforce ceilings; they are never converted to usage.
A callback bypassing its measured port cannot seal known zero usage for those
APPLYING transitions. A cancelled/incomplete scope is not restarted or resealed.

Only the live scope can close measurement after observing the exact underlying
terminal adoption. Wall usage is the ceiling-rounded elapsed `perf_counter_ns()`
interval from committed RUNNING scope entry through preparation, port calls and
underlying terminal observation. It includes operation receipt persistence overhead,
not pre-claim checks or later Activity settlement. It is independent of the caller's
wall-clock timestamp. Pre-dispatch checks bound new calls, not the duration of an
already entered call; measured overruns retain actual elapsed usage and the existing
ledger puts the Run into NEEDS_ATTENTION. Generated tasks/model calls/tokens stay 0.

Historical reconciliation accepts **no** `usage`/receipt input. Known usage is
settled only inside the live first-execution scope, after rechecking immutable
execution identity, contiguous intent/ACK pairs, exact plan mutations and the
terminal adoption digest. Completion measurement, immutable Activity result,
budget settlement and journal commit **in one SQLite transaction**. The public
reconciliation path can replay an already committed terminal result; it cannot
create new known usage from a serialized receipt. An orphan measurement without
its Activity result is invalid and fails closed, even if its ordinary hash matches.

If execution or terminal settlement crashes before this transaction commits,
operation/wall usage stays `None` (an empty plan independently proves zero port
calls, but cannot prove wall usage). Even all ACKs without a completed atomic
settlement do not establish precise wall consumption. After commit, ACK loss
replays the same known terminal fact without new execution or charging. Neither
intent count, target revision nor desired image is an execution receipt.

Measurement + Activity result + budget settlement + journal commit in the
existing single transaction. Exact replay does not charge again. A normal complete two-call or
zero-call ADOPT leaves a known-accounted Run WAITING; the next independent Interpreter
tick consumes its output without accounting/resume intervention. Unknown or exceeded
accounting enters NEEDS_ATTENTION. Reconciliation fills only unknown ledger fields;
immutable Activity usage remains its original execution fact. Late accounting never
reopens a cancelled/terminal Run.

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

## Active execution and recovery arbitration

Adoption terminal is not evidence that its Activity execution scope has ended.
A nonblocking OS exclusive lock, keyed by canonical local SQLite path and stable
invocation identity, protects first RUNNING/execution-identity commit through
prepare/adopt, port calls, terminal observation, atomic measurement/result/ledger/
journal settlement and scope exit. POSIX uses `flock`; Windows uses a one-byte
`msvcrt` lock. No SQLite write transaction is held across external execution.

Public recovery acquires the same lock before a `BEGIN IMMEDIATE` transaction and
re-reads all state/proof inside it. An active holder causes `concurrent_execution`;
the Adapter returns bounded `busy`, with no mutation, accounting or Run change.
Deadlines, PID estimates and leases never override this lock. Recovery marking a
RUNNING invocation with no underlying fact also observes the lock. The first task
may mark its own uncertain ceiling while holding it; this task-local bookkeeping
is not a substitute for the OS arbitration and cannot be borrowed by a child task.

Process death or explicit scope exit releases the OS lock. Recovery can then
commit conservative unknown usage when no complete atomic result exists; it does
not acquire a new execution scope or permission to redispatch. An orphan serialized
measurement still fails closed. Immutable terminal Activity replay only reads and
verifies durable facts and does not wait for execution liveness, rewrite usage or
require preserved resources. Complete known settlement always wins before lock
release; an outer ACK loss only replays that same known result.

Lock files remain unlocked after release and are never unlinked during database
use: removing/recreating an inode would split exclusivity. They contain no receipt,
owner authority or usage and do not create a second ledger or permanent held lock.
This is arbitration among cooperating local processes using the same database,
not OS isolation of Python dependencies or a distributed filesystem protocol.
Schema remains 41. No exactly-once transaction spans SQLite and the filesystem.

## Crash boundaries

| Window | Recovery |
|---|---|
| RUNNING committed, no adoption fact | Active scope: busy; abandoned scope: no redispatch, NEEDS_ATTENTION with reservation retained |
| Plan committed, before mutation | Same adoption ID/plan, live validation and existing core ownership recovery |
| Intent persisted / mutation happened, ACK lost / incomplete scope | Unknown accounting; existing target CAS and desired-image observation prevent repeated writes to that desired image |
| Adoption terminal, Activity result absent | Active execution lock: busy without settlement. After scope exit/process death: unknown atomic settlement; no live-resource requirement or mutation |
| Activity result or Interpreter consumption ACK lost | Exact durable replay; no repeated settlement or adoption |
| Third-party parent change | Existing three-way/conflict/indeterminate checks; never overwrite unrelated/new content |

Settlement failure rolls back measurement, budget, attempt, result and journal together; the
underlying adoption terminal fact remains available for the next reconciliation.
No cross-database transaction or exactly-once external filesystem promise is made.

## Compatibility and exclusions

Schema 40 → 41 creates only the three evidence tables and immutable constraints;
no historical rewrite. Generic DW5b-1 protocol
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
