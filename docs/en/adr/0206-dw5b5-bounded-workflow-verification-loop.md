# ADR 0206: Bounded Workflow VERIFY / REPAIR loop

[简体中文](../../zh-CN/adr/0206-dw5b5-bounded-workflow-verification-loop.md)

- Date: 2026-10-12
- Status: Accepted for the internal DW5b-5 capability
- Schema: unchanged at 43

## Decision and scope

DW5b-5 composes the existing durable Interpreter, Activity store, VERIFY
adapter, and REPAIR adapter into one explicitly invoked, bounded drive
capability. `WorkflowLoopCoordinator.drive_once()` is not another Interpreter,
Agent loop, scheduler, worker, or background service. Each control decision is
made by `DurableWorkflowInterpreter.advance_once()` from the latest persisted
Run, Step, Activity, output, journal, and budget facts. The coordinator invokes
only the existing VERIFY/REPAIR adapters for their exact persisted invocation.
It performs no Bash or file mutation itself.

This is an internal composition capability only. No CLI/TUI entry, automatic
Ultracode routing, generic Planner, completion judge, or product workflow
surface is added. A verified loop does not set the Workflow Run to COMPLETED.

## Typed control-flow shape

The accepted loop is a statically unrolled chain using only DW1 `Activity` and
`Branch` nodes:

```text
VERIFY_1 -> Branch(status == PASS)
              PASS: end
              FAIL: REPAIR_1 -> fresh VERIFY_2 -> Branch(status == PASS)
                       PASS: end
                       FAIL: REPAIR_2 -> fresh VERIFY_3 -> Branch(status == PASS)
                                PASS: end
                                FAIL: end
```

The final FAIL arm is empty, so there is no third REPAIR without a later
verification opportunity. The loop-plan recognizer accepts at most three
distinct VERIFY step IDs and two distinct REPAIR step IDs, verifies each exact
status `ResultRef` edge, and rejects extra VERIFY/REPAIR declarations or
unsupported shapes. Counts are frozen in the compiled definition; no model
text, arbitrary JSON, Python expression, or runtime counter can enlarge them.

DW1 Repeat is deliberately not used. It is post-tested: its body runs before
the `until` condition is evaluated, which cannot express “never repair after
the final failed verification” without changing Repeat semantics. The bounded
static Branch chain needs no IR, validator, compiler, or schema migration.

## Causal evidence and freshness

Each REPAIR input resolves to the exact legally consumed VERIFY FAIL in the
same Run and parent session, including its full `StepIdentity`, result
fingerprint, independent VERIFY journal proof, and current bounded parent
workspace evidence. The existing REPAIR adapter owns first-dispatch, owner and
fence checks, permission, parent Runtime, workspace, mutation, evidence, and
trusted usage. REPAIR completion or changed files never imply PASS.

A following VERIFY has its own step and invocation identity and performs a
new real configured command. Its typed input binds the exact preceding durable
REPAIR output as provenance. VERIFY source resolution now accepts a completed
REPAIR only after validating that exact REPAIR terminal proof; this does not
make REPAIR a verification result. The normal VERIFY adapter captures a fresh
workspace projection, executes the approved command, and requires fresh
evidence again when the Interpreter consumes the result. A stale PASS is not
consumed. These bounded projections are optimistic evidence, not a globally
atomic filesystem snapshot.

## Drive, budgets, and recovery

One explicit `drive_once()` call is capped at 96 durable Interpreter actions.
It reloads the current Run and immutable definition before every action and
checks the caller's current owner/fence. It delegates Activity recovery and
execution to existing adapters, then asks `advance_once()` to consume or
publish from durable facts. Busy execution, external DAG work, blocked
permission, cancellation, owner replacement, unknown accounting, stale
workspace, or other unresolved facts return a typed stop result. There is no
sleep, repeated polling, or automatic replay of historical RUNNING work.

Static limits are three VERIFY attempts and two REPAIR attempts. Existing
per-Activity reservations and the single DW2 Run ledger continue to enforce
model calls, tool calls, tokens, and wall time. Usage comes only from ADOPT,
VERIFY, and REPAIR trusted accounting. Unknown is not zero and cannot be
cleared by taking another iteration. Budget exhaustion or uncertain side
effects stop the drive safely.

The application-level outcomes are `VERIFIED`, `EXHAUSTED`, `BLOCKED`,
`FAILED`, `INDETERMINATE`, `WAITING`, and `CANCELLED`. `VERIFIED` means the
latest required real VERIFY PASS was consumed with valid freshness evidence;
it is not Workflow completion. Control-flow exhaustion retains the existing
`WAITING: completion requirements pending` state. Completion requirements,
generic retry, and product routing remain later work.

## Validation boundary

The focused acceptance uses real SQLite persistence, a temporary Git parent
workspace, real foreground pytest through the existing ToolExecutor, and the
existing AgentRuntime with an offline scripted Provider for REPAIR. Provider
responses are fixtures, not a live or commercial Provider accuracy claim.
Existing DW5b-2/3/4 recovery, permission, sandbox, accounting, and execution
arbitration remain owned by their original adapters and stores.
