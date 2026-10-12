# ADR 0205: One real Workflow REPAIR turn

[简体中文](../../zh-CN/adr/0205-dw5b4-real-workflow-repair-adapter.md)

- Date: 2026-10-11
- Status: Accepted for internal DW5b-4 integration

## Status and scope

Accepted for DW5b-4. REPAIR runs one bounded existing AgentRuntime turn. It does
not schedule VERIFY, retry uncertain execution, create a repair loop, judge
Workflow completion, publish DAGs, or add a CLI/TUI entry. REPAIR completion is
not verification PASS. ADOPT and VERIFY retain their existing engines and authority.

## Exact consumed FAIL

The declared REPAIR input must resolve a ResultRef to a legally consumed, real
VERIFY FAIL in the same Run/session and applicable StepIdentity/iteration. The
consumed output, VERIFY execution, immutable terminal evidence, and independent
Run settlement journal are checked together. Literal FAIL, PASS, ambiguous
selectors, missing proof, and mismatched resolved input fail closed. The current
bounded parent workspace projection must equal the verified FAIL projection
before claim; stale evidence cannot authorize repair.

The goal, exact configured verification command, redacted failure summary,
workspace identity, and approved bounds become bounded **untrusted task data**.
They do not grant permission. Prompt evidence is capped at 10,000 characters,
with a 2,048-character goal; the response is redacted and capped at 4,096 UTF-8
bytes. A model saying "fixed" does not prove any change or verification outcome.

## Reuse the parent execution boundary

A composition-owned `BoundWorkflowRepairTurn` retains the exact parent
ConversationBinding and calls its existing ConversationRunner.run. Provider,
Profile, effective capabilities, Permission/approval, Workspace, Sandbox, hooks,
context management, cancellation, and process cleanup remain the parent's.
There is no fresh binding, internal shell, second agent loop, or hidden session.

`WORKFLOW_REPAIR` is an explicit internal TurnSource/ModelRequestSource, not a
USER turn. The existing parent turn lock serializes it. Its task prompt and
assistant reply enter only the temporary model context; ordinary durable parent
conversation items remain unchanged. Exact turn recovery/events and execution
facts are persisted. Generic turn retry does not reconstruct this internal input.
Automatic configured verification is disabled for this turn; a later explicit
Workflow VERIFY remains a separate Activity.

A further trusted tool allowlist permits existing workspace read/discovery/edit
and foreground Bash tools, but excludes delegation, background, and extension
entry points. The scoped tool execution context removes the background manager, rejecting
explicit background Bash and preventing automatic foreground promotion. The parent
binding is not mutated. This only narrows parent authority. Read-only Profiles, denied
permissions, and unavailable required sandboxes cannot gain write authority.

## First dispatch and arbitration

Activity claim and reservation use the existing ledger. RUNNING and a unique
immutable repair execution identity commit together, binding invocation, Run,
Step, session/root, owner/fence, reservation, consumed FAIL, exact turn ID, and
turn-input fingerprint. Only this successful first commit creates a live
observation scope. Historical RUNNING cannot create another turn or obtain
measurement authority. Public reconciliation accepts neither usage nor receipts.

Nonblocking OS locks reuse the existing local SQLite execution-lock mechanism.
The workspace lock is keyed by canonical database path and parent root, so
separate repair invocations/Stores/processes for that root cannot concurrently
execute or settle uncertainty. An invocation lock additionally protects recovery.
The lock covers first execution through settlement; exception, cancellation, or
process death releases it, while lock files retain stable inodes. There is no
PID/timeout ownership inference and no SQLite write transaction spanning a model
request or filesystem operation. A cancelled/reclaimed Run cannot authorize the
next model/tool entry. Cleanup of an already started task is awaited before
releasing execution locks.

## Trusted accounting

A narrow scoped observer wraps actual provider adapter attempts and the existing
ToolExecutor entry after permissions/hooks/preflight. Retry/failover wrappers
forward observations to physical leaf attempts. Failed entered calls count;
preflight/permission rejection does not invent a tool invocation. Intent alone is
not proof of entry/return. Ordinary runtime has no observer and retains its old
call shape and behavior. Hosted provider tools without attributable usage force
unknown tool accounting.

Acknowledgements bind continuous ordinal, exact intent, model/tool identity,
return/failure, and provider-reported processed input/output tokens. Missing token
usage is `None`, never estimated zero. Controlled perf_counter_ns measures wall
time, including runtime and observation work. Known usage is available only to
the live first execution with complete ACKs. Python scope identity is trusted
composition isolation, **not operating-system security against arbitrary Python
code**. Model/MCP/Skill parameters cannot submit these observations or replace
composition dependencies.

Defaults are at most 4 model calls, 8 tool calls, 32,000 input tokens, 8,000 output
tokens and 60,000ms. Reservation and runtime budget are intersected with parent
limits and remaining Workflow capacity. Dispatch checks known prior consumption
and approved ceilings; a provider's indivisible response can exceed a token
reservation, so actual overrun is retained and requires attention. No post hoc
fictional precision is used. `generated_tasks=0`; DW3 owns task publication.

## Workspace and terminal facts

Existing bounded checkpoint projection supplies repository/root/HEAD/index/content
fingerprints. At most 256 path entries are accepted. A before/after projection
and at most 64 changed paths (with truncation flag) distinguish confirmed changes,
no change, and uncertainty. Model waits must not change the workspace; tools may
change it, and the next checked snapshot becomes their owned boundary. Third-party
changes outside these optimistic checks cannot be atomically excluded.

Normal completed Runtime outcome, complete known accounting, stable authority and
workspace evidence produce Activity COMPLETED and the DW1 output
`{status: "COMPLETED", response: string}`. Permission/preflight/runtime blocking
produces BLOCKED; a known execution failure may be FAILED. Missing ACKs/tokens,
cancellation, budget overrun, owner loss or uncertain workspace produce
INDETERMINATE/NEEDS_ATTENTION, without reopening a terminal Run. No unsuccessful
turn publishes a consumable success output.

Repair evidence, immutable Activity result, existing budget settlement, local
Activity event and independent Run journal anchor commit atomically. Reads verify
all fingerprints and source/turn linkage against that independent journal. Local
group rewrites with rehashed fingerprints do not replace the history anchor. This
is not protection against an administrator rewriting the entire database/history.

## Crash and replay

Abandoned CLAIMED has zero model/tool/token usage and unknown elapsed time.
Historical RUNNING never dispatches; incomplete measurement remains unknown even
if files now match desired content or a turn terminal exists. Recovery requires
explicit attention. Complete terminal records replay without model/tool execution,
filesystem mutation, new claim, or duplicate accounting; they do not require the
old live workspace/HEAD. Interpreter consumption is a later independent tick.

SQLite, provider and filesystem are not one transaction. The protocol prevents
automatic duplicate dispatch and reports uncertain windows, **not exactly-once
external effects**. Bounded projection is not a global filesystem snapshot; other
databases, external editors and independently composed ordinary runtimes are not
covered by this local repair lock. Existing parent runner serialization plus
optimistic before/after checks define the supported boundary.

## Exact durable Runtime turn proof

Successful REPAIR settlement reads the exact session/turn `TURN_COMPLETED` event
inside the same SQLite write transaction and verifies the matching
`session_turn_attempts` source is `WORKFLOW_REPAIR`, its frozen input fingerprint,
`committed` resolution and `TURN_COMPLETED` stage. In the atomic turn-finalization
transaction, SessionStore persists execution status/reason/detail, `finalized`
and `recoverable` metadata from the typed Runtime-provided
`SessionExecutionRecord`. This is a per-turn durable summary of the existing
Runtime terminal, not a second completion owner created by REPAIR.

The normal `AgentRunResult.outcome=None` case is accepted only when this exact
durable proof says `execution_status=completed` and all other accounting,
permission and workspace conditions pass. A non-null returned outcome must match
the persisted outcome. Missing, duplicate, ambiguous, uncommitted or conflicting
proof never produces success output; it enters
INDETERMINATE/NEEDS_ATTENTION without automatic redispatch.

REPAIR evidence stores the exact proof and its fingerprint; atomic settlement also
binds its digest into the independent Run journal. Terminal reads, recovery and
Interpreter consumption revalidate the event and attempt by exact turn id and
compare the evidence/journal anchor. They never substitute the session's latest
execution record for a historical turn because that latest-per-session projection
is overwritten by later turns. ACK loss replays the same immutable Activity result.

## Persistence and verification

Schema 42→43 adds only `workflow_repair_executions`, `workflow_repair_events`, and
`workflow_repair_evidence`, with bounded JSON, FK/unique/check constraints and
immutable triggers. No second budget ledger is introduced. Transactional migration
preserves old records; repeated initialization is idempotent. New repair publications
carry a protocol discriminator; legacy unversioned Activity facts remain readable,
but new REPAIR generic terminal writes cannot forge real execution evidence.

Tests distinguish real SQLite transactions/reopen, temporary Git/file mutations,
real AgentRuntime with an offline scripted provider, actual pytest VERIFY, injected
VERIFY exits/faults, and a real independent-process kill. No commercial-model or
cross-filesystem exactly-once acceptance is claimed. Ordinary Runtime/provider,
Activity/Interpreter, ADOPT/VERIFY, migration, permissions/sandbox and Swarm/UltraCode
regressions remain required. `scripts/check_all.py` owns completion gates.
