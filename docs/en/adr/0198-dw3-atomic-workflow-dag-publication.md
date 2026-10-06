# ADR 0198: DW3 atomic Workflow expansion publication

**English** · [简体中文](../../zh-CN/adr/0198-dw3-atomic-workflow-dag-publication.md)

- Status: Accepted publication foundation
- Date: 2026-10-06
- Scope: prepared intent → immutable DAG publication; no Interpreter

## Immutable batches and the narrow seam

`application.ports.workflow_publication.WorkflowPublicationStore` accepts a frozen
`WorkflowExpansionIntent`, not a dictionary or an execution command. It publishes
one fresh `TaskDag` through the existing SQLite session store. Domain DAG validation
retains 8 nodes, 16 edges, 4 dependencies per node and at most 4 parallel workers.
Each expansion has a new DAG id. Existing DAG graph definitions are never mutated.
The scheduler, Leader, Worker, Permission and Sandbox paths are unchanged.

The caller supplies already-resolved task prompts and input fingerprints. DW3 checks
declared TaskBatch/Map task coverage, dependency shape, route, Repeat/Map identity
scope and declared parallelism. It does not interpolate prompts, evaluate a branch,
advance Repeat, read actual results, or derive Map members. Profile and capability
intent remains in the immutable Workflow Definition; it grants no execution authority.
Future adapters must resolve it through the existing authority boundaries.

## Expansion identity and frozen members

An explicit, globally unique `expansion_id` binds run id, StepIdentity
`(step_id, iteration, item_key)`, input fingerprint, frozen members and DAG definition
fingerprint. Each `ExpansionMember` binds an opaque member key, declared task id,
DAG node id and input fingerprint. Members are canonicalized by `(member_key, task_id)`;
DAG ordinals must follow this order. A TaskBatch uses the literal member key `batch`.
Map uses caller-supplied opaque item keys. Every member must cover one DAG node, and
every item must contain the complete declared template task set. No item is truncated.
Map templates and TaskBatches may only narrow their declared parallelism.

The member-set hash and expansion identity hash use canonical UTF-8 JSON/SHA-256.
The full immutable intent also binds parent session, prompts, dependency order,
max_parallel and UTC DAG creation time. It is compared on every retry, including
fields not included in the existing DAG fingerprint (such as max_parallel).
Changing any intent payload under the same expansion id fails closed. A second
expansion id for the same run/step instance is rejected; a new Repeat iteration or
Map item has a different StepIdentity. A DAG already inserted separately cannot
be adopted by publication, even when its definition happens to match.

## One transaction and durable linkage

Schema 36→37 creates only `workflow_expansions`. Its foreign keys bind the existing
run, step instance, DAG and publication journal generation. Run/step and DAG links
are restrictive; the journal-generation link is deferred until transaction commit.
Unique DAG and run/step keys prevent accidental double publication. Update/delete
triggers retain immutable expansion evidence. Migration is inside the existing
serialized SQLite migration transaction.

The adapter opens one `BEGIN IMMEDIATE` transaction and performs:

1. Run generation, owner identity and owner fence checks; only RUNNING/WAITING runs
   can publish new work. NEEDS_ATTENTION and terminal runs cannot publish.
2. Validation of declared membership and fresh DAG identity.
3. Generated-task budget reservation and exact consumption for the new node count.
4. DAG/node inserts using the caller-owned connection, with no intermediate commit.
5. Run generation increment, WAITING position and a WAITING step instance.
6. Immutable expansion insert and one bounded `dag_published` journal fact.

The expansion table is the durable DAG linkage. Waiting reasons only provide readable
context. Existing unresolved steps remain in the snapshot. The journal records hashes,
linkage and task count, not another copy of all task prompts. The existing 32-KiB
journal bound remains intact. Reads use one WAL snapshot and verify intent hashes,
DAG definition/parallelism, step input, publication accounting and the exact journal
link. DAG lifecycle may evolve through its existing CAS APIs while the definition and
publication identity remain fixed.

## Retry, crash and budget

`expansion_id` is the publication request identity; its SHA-256 names the corresponding
journal event and budget reservation. Exact retries return the original expansion/DAG
identity and current lifecycle snapshots with `replayed=True`. CAS metadata and call
time may differ on a retry: replay acknowledges an existing fact, performs no writes,
and does not grant a stale owner permission to advance. New publication still requires
current generation and owner fence. Callers retain the original immutable intent.

Any failure before commit rolls back DAG/nodes, expansion, Workflow linkage,
reservation/consumption and journal together. A commit followed by a lost client
response is resolved by the same identity. There is no orphan-DAG adoption heuristic
and no side-effect retry or exactly-once worker claim.

Generated-task reservations share the DW2 run ledger and write transaction. The exact
number of inserted nodes is settled once in the same transaction, with zero actual
Model/Tool/token/wall consumption supplied for this publication-only operation.
Existing pending capacity, unknown usage, narrowed ceilings and bounded reservation
limits continue to apply. Concurrent publishers or reservations cannot oversell the
run ceiling. Replay neither reserves nor consumes again.

## Verification and deferred work

Database regressions cover single batches, seven-item fan-out, canonical membership,
invalid bounds, conflicting retries, concurrent processes, stale generation/fence,
shared budget races, six failure windows, reopen integrity, migration rollback and
existing DAG lifecycle compatibility. No SessionTask, worker, lease or tool is created.

DW4+ owns Interpreter control, real-result projection, branch/repeat decisions,
activity adapters and completion proofs. DW3 has no automatic Workflow execution,
Runtime/Planner/UltraCode/TUI wiring or second scheduler. Issues #165 and #167 remain
independent backlog work.
