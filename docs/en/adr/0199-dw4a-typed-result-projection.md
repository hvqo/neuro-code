# ADR 0199: DW4a durable typed result projection

**English** · [简体中文](../../zh-CN/adr/0199-dw4a-typed-result-projection.md)

- Status: Accepted persistence foundation
- Date: 2026-10-06
- Scope: exact terminal result facts; no Workflow execution or adoption

## Source and identity

`WorkflowProjectionStore.project_workflow_result()` accepts only an expansion id,
its expected run/session scope, a timestamp and optionally an expected source hash.
There is no caller-supplied DAG, response or output dictionary. Within one SQLite
snapshot it revalidates the DW3 expansion, definition, frozen member bindings,
publication journal, budget linkage and exact DAG definition. Both the DAG and every
node must be terminal. COMPLETED must contain only completed nodes; FAILED, CANCELLED
and INDETERMINATE follow the existing scheduler's terminal classification.

The projection identity derives from the full expansion identity. Source fingerprint
binds definition/run/session, step/iteration/item, input/member hashes, exact DAG id,
definition, max_parallel, terminal DAG/node generations, node definitions, terminal
status/error and workspace metadata, exact result fingerprints and worker linkage.
Completed nodes require matching durable SessionTask, child session, workspace lease,
checkpoint, parent relay (including integrity digest), final workspace fingerprint
and changed-file count. No workspace files are read or mutated.

## Exact response, not a preview

The existing DAG `response_preview` is bounded to 8 KiB and may be truncated without
a flag. The existing writable-worker result is redacted and bounded to 32 KiB with
an explicit truncation flag. Neither a preview nor a truncated worker result can
satisfy DW1's `response` field.

Schema 37→38 adds `task_dag_result_evidence`, captured in the same existing terminal
node CAS transaction as node state, only for DAGs already bound by DW3. Ordinary
DAGs retain their existing storage/cleanup lifecycle. It stores the exact returned redacted worker
response and truncation evidence, bound to worker/child identity, node generation,
full node snapshot hash and canonical payload hash. This does not change execution,
scheduling, result redaction or worker response limits. It introduces no judge.
Truncated results fail projection; a future exact artifact contract would be a
separate change. A completed legacy/recovered node without exact result evidence
also fails closed; it is not reconstructed from previews or guessed session turns.

For failed/cancelled/skipped/indeterminate nodes with no produced result, `response`
is the empty string and source facts explicitly record `not_produced`. A present
preview without exact evidence is rejected. Status is the actual terminal node
value, including failure and uncertainty; it is never defaulted to success.
`response` means complete redacted worker-contract text, not an unredacted model
transcript, a correctness judgment or verification PASS.

## Existing DW1 output schemas

`workflow_output_schema()` exposes the same schema used by DW1 ResultRef validation.
TaskBatch outputs remain `tasks.<task_id>.{status,response}`. Map outputs remain
`{count,items:[{tasks:...}]}`. Each task is mapped by its frozen member/task/node
binding. Map items follow canonical member-key order; source facts preserve those
keys for future item selectors. No node-order inference or second schema exists.
Closed required fields and scalar types are checked without coercion. JSON remains
canonical UTF-8 with sorted object keys and deterministic SHA-256 hashes.

## Persistence, retries and limits

`workflow_result_projections` holds one immutable fact per expansion/DAG. Both new
tables have restrictive foreign keys and update/delete rejection triggers. A
serialized `BEGIN IMMEDIATE` transaction performs source validation and insertion;
rollback cannot leave a partial projection. Competing callers converge on one fact.
Exact retries return the original fact, including its original timestamp. A changed
source or persisted payload fails integrity checks; an expected-source mismatch
fails conflict checks. Reads revalidate linkage and hashes after restart. Result
JSON is bounded to 1 MiB, worker response to 32 KiB, and existing 8-node bounds remain.
Hashes detect accidental/stale corruption, not an attacker able to rewrite the entire
database and all matching hashes.

Projection is itself durable evidence, not a new journal/control transition. It
changes no Workflow generation, owner, step status, budget, waiting reason or journal.
It cannot execute Branch/Repeat/Map, publish work, mark a Workflow COMPLETED, elevate
Permission/Sandbox authority or modify a parent workspace. Schema-valid output is
not factual correctness. Verification/completion requirements remain future
Interpreter work. DW4b may adopt a completed DAG through existing authority boundaries;
DW4a does not call adoption or any activity/Planner/UltraCode adapter.
