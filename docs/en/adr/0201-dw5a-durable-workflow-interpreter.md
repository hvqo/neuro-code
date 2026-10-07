# ADR 0201: DW5a durable Workflow interpreter

[简体中文](../../zh-CN/adr/0201-dw5a-durable-workflow-interpreter.md) · **English**

- Status: Accepted internal foundation
- Date: 2026-10-07
- Scope: bounded durable control with deterministic fake activities

## Ownership and one tick

`DurableWorkflowInterpreter.advance_once(run_id, expected_generation, owner_id,
owner_fence, updated_at)` selects at most one durable action. It never runs a DAG,
waits for workers, or loops until completion. Declared sequence order, completed
step instances and BRANCH/ITERATION facts determine the next action. Traversal
is bounded to 256 declarations and journal reading to 40 pages of 100 facts;
these are read bounds, not execution loops. A waiting tick returns no progress
without writing another event. Callers explicitly schedule additional ticks.

Every write goes through DW2 CAS or the DW3 atomic publication seam. The caller
must load the current generation after acknowledgement loss. An old generation,
owner or fence stops before reference resolution/activity evaluation. DW3 replay
is only a historical read and is rejected as an advancement/ownership substitute.
A replacement owner retains DW2 NEEDS_ATTENTION and requires explicit existing
reconciliation; the interpreter does not automatically waive that safeguard.

## Typed values and schema 39

Schema 38→39 adds only `workflow_run_inputs` and `workflow_step_outputs`, with
immutable UPDATE/DELETE triggers. Input is canonical, DW1-schema-validated JSON;
its SHA-256 must equal the existing Run input fingerprint. Freeze it using
`put_workflow_input` after creation, before any ownership claim. A crash between
creation and input attachment leaves a non-executable Run; no caller dictionary
or invented default replaces missing durable input. Generation-0 runs without input are uninitialized durable facts. A future entrypoint
must attach the same immutable input before claim/execution. Legacy runs remain
readable but cannot execute without their original immutable snapshot contract.

InputRef reads that snapshot. TaskBatch/Map ResultRef reads the exact DW4a
projection linked to the consumed step, revalidating Run/Expansion/DAG/member/
node evidence; no preview, latest-result lookup or transcript substitution exists.
The output table stores only projection identity/fingerprint, never another copy
of worker response. Activity results and empty Map facts use the same DW1 schema
and a discriminated local provenance: they are not worker results/projections.
Repeat's declared `iterations/last` or proven iteration-1 selector is derived
from its durable iteration facts and exact body outputs, without another schema.
ItemRef is restricted to the individual Map item scope. Literal and ArtifactRef
remain data; an artifact integrity reference never reads a file or grants authority.

## Publication and Map

First encounter initializes a READY step with canonical resolved-input digest.
A later tick deterministically builds a new immutable Task DAG and calls DW3.
Expansion/node/DAG IDs bind the Run and StepIdentity. DAG creation time uses the
Run's durable creation time, so retries do not change canonical publication.
Task prompts retain the declared template and append canonical inputs as data;
this is not an expression/template execution engine. Profile/capability declarations
are frozen in each immutable Expansion member alongside member/task/node and input
identity, not prompt instructions. Their canonical payload participates in the
member, publication and journal fingerprints. DW3 commits DAG, these bindings,
Run/Step WAITING, budget and journal in the same transaction; no sidecar write or
new schema is needed. Old publications retain their exact historical JSON/digests
but missing execution intent makes them non-executable, without rewriting records.

Before Worker launch the Task DAG store resolves the exact durable node intent
and checks publication/Run/Step/DAG/member/journal linkage. Writable Subagent
rechecks that exact intent and parent session before allocating execution resources.
The existing built-in Profile catalog resolves the requested ID; only writable-worker
role with managed-worktree policy is eligible. Unknown custom IDs and incompatible
built-ins fail closed, never default to writable_worker. No custom registry is added.
The existing create_binding receives the exact profile and unchanged Writable grant.
Before any model call, EffectiveAgentBinding must retain that profile identity and
include every required capability. Required capabilities confer no grant: parent,
Permission, Sandbox, provider, platform, runtime and profile ceilings still intersect.
A rejected launch becomes the existing FAILED node outcome, without retry, replan
or verification interpretation. Non-Workflow DAGs retain their default writable
profile and scheduler behavior; no automatic Workflow execution entry is added.

Map freezes the bounded typed collection before publication. Each member key
contains its zero-padded source ordinal and item digest, preserving array order
and distinguishing duplicate values. Node IDs include member and task identity;
dependencies stay within that member. Member ordering follows DW3 canonicalization.
There is one DAG, at most 8 nodes and the existing parallelism limits. Empty Map
writes `{count:0,items:[]}` atomically with its completed step and no zero-node DAG.
Generated tasks are reserved/consumed only by the DW3 publication transaction;
Repeat/replay never reset run budget.

A WAITING tick reads only its deterministic expansion. Nonterminal DAG or absent
projection means no progress. COMPLETED DAG plus exact persisted DW4a projection
permits one atomic result-link/step-completion/Run/journal commit. FAILED/CANCELLED
DAG stops the Run as FAILED; INDETERMINATE becomes NEEDS_ATTENTION. No automatic
retry, adoption, repair or replanning is inferred from worker text.

## Branch and bounded Repeat

Branch evaluates only DW1 `eq/exists`, then records one BRANCH decision. Required
schema paths resolve before `exists`; missing/corrupt result facts fail closed,
not false. Restart uses the recorded selection, never live reevaluation; unselected
paths create no step, activity or publication.

Repeat initializes READY, durably starts RUNNING, records iteration 1, executes
its declared body one tick at a time, then evaluates post-body `until`. True
completes the Repeat control step; false records the next ITERATION only below
the declared maximum (≤3). False at the limit durably fails with `repeat_limit`.
Each body instance uses its iteration identity. No nested Repeat/Map, mutable VM
stack, bytecode, runtime DAG mutation or second scheduler is introduced.

## Fake activities and crash consistency

The narrow `FakeWorkflowActivity` port is synchronous pure deterministic evaluation.
The shipped fake ADOPT/VERIFY/REPAIR outputs explicitly report `status:"fake"`;
ADOPT reports no parent change, VERIFY returns only the iteration number, REPAIR
reports that no repair was executed. They do not access files, permission, tools,
models, Result Adoption or verification. Stable invocation IDs bind Run, step and
resolved inputs. A pure evaluation may be recomputed before commit, but exactly
one logical output fact survives CAS. This is not an exactly-once claim for future
real side effects.

Local output/projection link, completed step, Run generation and journal commit
in one SQLite transaction, with foreign-key linkage to step and journal generation.
Exceptions roll everything back. Commit-before-ACK restart derives the next action
from the persisted branch, iteration, publication, consumed projection or activity
result, without a duplicate DAG, logical activity or generated-task charge. Reads
check schema, digest, provenance and step/journal linkage and reject tampering.

## Journal payload contract

STEP is a discriminated payload family, not a promise that every event contains
`change`. Consumers must dispatch by `operation`: DW2 `transition` carries `change`,
DW5a `consume_typed_output` carries typed output linkage. Future consumers must not
unconditionally read `payload["change"]`. No event-kind redesign is needed here.

## End boundary and exclusions

Exhausting control flow enters WAITING with
`control_flow_exhausted: completion requirements pending` (the single
`CONTROL_FLOW_EXHAUSTED_REASON` constant). Further ticks are static.
It is not Workflow COMPLETED, verification PASS or permission to adopt. DW1
completion requirements are not evaluated by this slice. Task DAG COMPLETED,
worker response, schema-valid output and fake status never prove business success.

No scheduler/Leader control change, additional execution engine, real parent activity, filesystem mutation,
Planner/UltraCode wiring, model judge, CLI/TUI/Trace, plugin or hook is added.
DW5b must separately define real Activity execution/recovery and verification
consumption, retaining the existing adoption engine and authority boundaries.
Issues #165/#167/#169/#171 are not repaired here.

## Validation

Real SQLite tests cover sequence, typed input immutability/upgrade, Branch replay,
Repeat 1/2/3/limit and restart, exact Repeat selectors, Map 0/1/7 and item isolation,
within-member dependencies, exact projection consumption, failure states, owner
fencing, concurrent publication/results, generated-task exhaustion, atomic rollback,
all five commit-before-ACK windows and tampered input/projection/output. Existing
DW1–DW4b, migration, adoption, scheduler and UltraCode regressions remain gates.

Execution-intent regressions also exercise profile-only/capability-only identity
changes, restart/replay, atomic rollback after DAG insertion, legacy missing intent,
tampering, exact binding and effective-capability rejection at each ceiling. A real
composition test proves the resolved built-in reaches create_binding and missing
runtime capability/unknown profile stops before the provider's first model call.
