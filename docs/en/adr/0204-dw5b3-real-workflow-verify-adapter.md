# ADR 0204: Real Workflow VERIFY adapter

[简体中文](../../zh-CN/adr/0204-dw5b3-real-workflow-verify-adapter.md) · **English**

- Date: 2026-10-09
- Status: Accepted for internal DW5b-3 integration
- Scope: one explicit verification command; no REPAIR or completion judge

## Decision and authority

`VerificationTracker` observes runtime facts; it is not an executor or a content-version authority. Bootstrap builds `ApprovedWorkflowVerification` from the explicit user `verification_command` setting. The existing command classifier accepts pytest and static-check families. Workflow inputs, worker responses, README text and model JSON cannot supply a command or permission grant.

`WorkflowVerifyActivityAdapter` uses the actual parent binding's `ToolExecutor`, its tool collection and current Permission, capability, Workspace, Sandbox, hooks and undo boundaries. Bash owns foreground process execution, timeout, bounded/redacted output and process-tree cancellation. Background promotion is disabled for this one call. There is no second shell, Agent Loop, scheduler or automatic test discovery. A single-use trusted pre-entry guard runs after permission/approval, hooks and workspace preflight, immediately before entering the existing tool port. It rereads the exact invocation/execution, RUNNING Activity, Activity owner/fence, Run waiting state and frozen Workflow owner/fence, session/root and first-dispatch scope. Cancelled or reclaimed execution does not enter Bash or the process launcher. A tool-entry observation follows only a successful final guard; a permission refusal counts zero tool calls. Tests and static checks can write files and caches: classification does not make a process read-only or bypass mutation policy.

## Durable identity and schema 42

Schema 41 has ADOPT-specific mutation metering; it cannot represent an approved verification command, exit state or source workspace revision. The minimal atomic 41→42 migration adds two insert-only tables: `workflow_verification_executions` (one execution identity per invocation) and `workflow_verification_evidence` (one terminal evidence per execution). FK, uniqueness and immutable triggers preserve existing Activity records and ledger. Migration does not invent evidence or known usage for old attempts.

Execution binds the existing immutable invocation/request, Run/session/Step identity, owner/fence/reservation, exact approved configuration, parent root and bounded workspace evidence. Consumed ResultRef inputs are pinned to their existing typed output fingerprint and journal, including applicable ADOPT/projection facts. Input JSON is compared with that frozen source; it never supplies a latest DAG or execution authority. Persisted evidence binds the execution fingerprint, terminal state, exit code, bounded redacted summary/digest, output and actual usage. The terminal budget-consumption event in the independent Workflow Run journal pins execution/invocation/request, approved configuration, owner/fence, the tool terminal observation, exit/outcome, before/after workspace evidence, evidence digest and Activity result fingerprint. It uses the existing settlement generation/request identity, not a second budget event. Read and consumption validate this exact historical anchor. Rehashing evidence, result, snapshot and the last local Activity event together cannot turn FAIL into PASS or replace command/workspace/source while that Run journal remains intact. This does not promise resistance to an administrator rewriting the entire database.

New VERIFY publications carry `verification_protocol=1` in their immutable Run publication journal. Generic Activity finish rejects new VERIFY terminal writes by the definition's Activity kind, regardless of execution-row existence or source-id prefix. New terminal VERIFY facts require complete dedicated evidence and the independent settlement anchor. Interpreter and direct output writes use the declared VERIFY kind and require the live freshness-consumption scope. New `FAKE_ACTIVITY` output writes are prohibited. An already persisted unversioned legacy publication/output remains historical read-only; it cannot grant a new dispatch or consume permission. Control-only tests inject explicit test ports or construct old SQL fixtures, never retain a production fake PASS writer. No schema increment beyond 42 is needed.

## First dispatch and recovery

Claim atomically reserves the existing run-level budget. First owner CAS commits RUNNING together with execution identity before calling the tool. A nonblocking OS lock, keyed by resolved local SQLite path and invocation, covers preflight through terminal settlement. Separate Store instances/processes share it (POSIX flock, Windows one-byte lock); descriptors close on exception/cancellation/process death, and lock files are never unlinked. A busy recovery writes nothing. SQLite write transactions do not span the shell operation.

A historical CLAIMED/RUNNING attempt never obtains another dispatch permit. Missing terminal evidence recovers conservatively to INDETERMINATE: RUNNING tool/wall usage remains unknown; abandoned CLAIMED keeps zero execution counts and unknown preparation wall time. No time/PID heuristic or observed file contents is used to infer execution count. An in-process task-local first-execution scope restricts known settlement; this is a trusted composition boundary, not OS isolation against arbitrary Python code or a hostile database administrator. Command/workspace dependencies and the pre-entry guard are composition-owned ports and are not exposed as model/MCP/Skill/ToolCall JSON parameters. Ordinary ToolExecutor calls leave the optional guard unset and retain their existing policy and cancellation behavior.

Evidence, immutable Activity result, budget consumption, Run safety state and journal commit in one SQLite transaction. Rollback preserves RUNNING without a partial proof; commit with lost outer ACK replays the same terminal fact without running the command. Cancellation waits for the existing Bash process cleanup before releasing arbitration. The final guard closes cancellation completed during approval/hooks/preflight; a later concurrent cancellation can still race the external process entry and is handled by the existing execution cancellation/cleanup. A local SQLite read and OS process startup are not a cross-system atomic transaction. If the cancellation watcher interrupts the pending final guard before a terminal tool observation is acknowledged, the existing RUNNING recovery conservatively retains unknown usage, even when a test launcher can establish zero spawns; it never redispatches. A completed pre-entry rejection observation counts zero calls. Unknown/exceeded usage produces NEEDS_ATTENTION. Late accounting does not reopen terminal Workflow state.

## Workspace evidence and freshness

The existing bounded checkpoint projection supplies repository/root/HEAD, Git index, tracked content and non-ignored untracked content. HEAD alone and an observation-generation counter are insufficient. Unsupported/oversized projections cannot produce PASS. Ignored caches are outside the source projection; use the existing bounds, never an unrestricted repository hash.

Before execution capture the projection and bounded source-path metadata watch. During the command, observe these paths every 50ms without rehashing contents; changed inode/mode/size/mtime/ctime invalidates the result even if bytes are later restored. Recapture the complete bounded projection at command completion. Before Interpreter consumption recapture it again. A changed source revision blocks stale PASS and FAIL before any Workflow output commit; direct real-VERIFY output writes without the fresh-consumption scope fail closed.

This is bounded optimistic filesystem freshness, not atomic filesystem/SQLite isolation. External changes entirely between observations, especially new-and-removed untracked files, cannot be universally detected; ignored files and external resources are not verified. It does not claim a general verification PASS for resources outside the captured source projection. Source writers must be quiescent during verification/consumption; there is no distributed filesystem lock.

`workspace_generation` remains the durable Run generation captured at execution start: an ordering/correlation integer, not a filesystem version. The separate projection fingerprint carries content identity.

## Output semantics

- Real exit 0 with complete unchanged source evidence: Activity COMPLETED, `status=PASS`.
- Recognized pytest/static check exit 1 with complete unchanged evidence: Activity COMPLETED, `status=FAIL`; later Branch/Repeat can consume it.
- Missing trusted configuration/unsupported source, missing explicitly required sandbox launcher or permission denial before tool entry: BLOCKED, no PASS/FAIL.
- Exit without reliable code, signal, timeout, cancellation, workspace drift or crash: INDETERMINATE / NEEDS_ATTENTION.
- Other nonzero exits (e.g. collection/configuration/executor errors): FAILED, not a fabricated test FAIL.

The public DW1 output stays `{status: string, workspace_generation: integer}`; real successful executions limit status to PASS/FAIL. Only foreground tool invocation and controlled `perf_counter_ns` wall duration are charged; generated tasks, model calls and tokens remain zero. Reservation bounds one call and the configured deadline plus bounded preparation allowance; actual overrun is recorded, never clamped. Recovery accepts neither caller amounts nor caller receipts. VERIFY completion does not declare Workflow COMPLETED or satisfy global completion requirements.

## Verification and limitations

Focused tests distinguish actual Bash/pytest/ruff in temporary Git repositories from injected command boundaries used for crash/ACK/overrun faults. SQLite rollback/reopen, independent-process arbitration/death, stale PASS, dirty content, consumed ADOPT→VERIFY, FAIL routing, Permission/Sandbox refusal, migration and legacy regressions are covered. New attack regressions include actual pytest FAIL followed by SQLite backup/reopen and coordinated local rehash, missing/conflicting journal anchors, and zero-spawn approval/preflight/hook cancellation using Permission, ToolExecutor, Bash and a recording real launcher. A started Bash descendant cancellation test complements the existing platform process-tree suite. Test-only injected command/workspace ports prove control and transaction invariants, not real shell authority. No exactly-once shell side-effect guarantee is asserted; uncertain execution is never automatically repeated.

Result Adoption core, Task DAG scheduling, ordinary verification tracking and Swarm/UltraCode semantics remain unchanged. Interpreter only publishes/consumes Activity; it does not execute commands. REPAIR, VERIFY→REPAIR loops, completion policy, Planner and UI wiring remain future work.
