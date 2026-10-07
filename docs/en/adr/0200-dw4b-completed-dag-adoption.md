# ADR 0200: DW4b completed-DAG result adoption adapters

[简体中文](../../zh-CN/adr/0200-dw4b-completed-dag-adoption.md) · **English**

- Status: Accepted internal adapter foundation
- Date: 2026-10-06
- Scope: reuse existing Result Adoption; no Workflow interpreter or verification

## Decision

`CompletedDagAdoptionSource` binds a discriminated Swarm/Workflow source identity,
parent session, exact DAG id/generation/definition fingerprint. A Workflow reference
pins Run, Expansion, Projection identity and fingerprint. Its projection source digest
also binds Step/iteration/item, frozen member bindings, terminal node generations,
exact result evidence and workspace metadata. It confers neither Workflow ownership
nor permission. Caller-supplied DAGs, worker prose and response previews are not sources.

Swarm and Workflow adapters resolve the same typed source for the existing
`ResultAdoptionApplicationService`. The composition root supplies both adapters;
there is no fake Swarm row, id alias, second merge/copy-back engine or scheduler.
The Workflow adapter reads only the DW4a projection port, which revalidates the
DW3 publication and complete source linkage, and checks the returned exact DAG.
Only COMPLETED DAGs with completed writable workers are eligible. FAILED,
CANCELLED and INDETERMINATE projections remain facts but cannot authorize adoption.

## Workspace safety and recovery

The existing application core still inspects the actual parent binding/root/repository/
HEAD, preserved lease and managed READY worktree, READY baseline checkpoint,
capability/grant and final workspace fingerprint. It creates the same bounded
baseline-to-desired three-way targets, rejects worker overlap, protected paths and
parent same-path conflicts, and preserves unrelated dirty files. Writes continue
through the injected permission/workspace/sandbox mutation port. No adapter writes files.

The durable adoption owner/lease, target CAS, desired-image observation and forward
recovery are unchanged. A crash after a write but before ACK is recovered by observing
the desired image rather than repeating the write; a third image is never overwritten.
Workflow provenance is checked again after source inspection before creating a plan.
Live source revalidation protects execution/forward recovery. Durable terminal replay
relies on persisted adoption identity and integrity and does not require execution-time
resources to remain live. Every terminal state returns the existing fact after validating
plan integrity, exact request source and parent session/root, without resolving Projection,
requiring preserved leases/worktrees/checkpoints or checking the current parent HEAD.
Non-terminal replay retains exact source and live parent checks, owner fencing and safe
forward recovery. Recovery uses the immutable materialized plan through the existing
owner lifecycle; neither terminal replay nor projection replay claims a Workflow Run.

## Durable compatibility and identity

Schema remains v38: the existing fingerprinted `plan_json` carries the source variant.
Legacy and newly created Swarm plans retain exactly the prior JSON fields and
fingerprint algorithm, including `swarm_run_id`; decoding exposes typed Swarm
provenance without rewriting SQLite records or discarding integrity checks.
Workflow plans omit `swarm_run_id` and add `completed_source`. Mixed identities fail
closed. The source kind prevents Swarm/Workflow identity collisions.

The caller supplies a stable `adopt-*` id and an exact Workflow projection reference.
The immutable plan binds that id/source to the parent repository/HEAD, exact DAG,
worker facts and generated targets. Exact replay returns/recovers the same durable
plan without repeated mutations. Concurrent preparation can reuse the winning plan
only when every field other than creation time is identical; adoption ownership is
still claimed separately. Reusing an adoption id with a different source,
projection, parent or DAG is rejected. Cache or retry does not confer ownership.

## Result and exclusions

The existing typed `ResultAdoptionRecord` reports state, plan fingerprint, adoption id
and `parent_workspace_changed`. COMPLETED adoption is not verification PASS.
No Workflow Step/Run, generation, budget or journal is advanced; no Branch/Repeat,
VERIFY/REPAIR, Workflow completion, Planner or UltraCode Workflow wiring is introduced.
The existing Swarm/UltraCode path remains compatible. Interpreter/Activity execution,
completion requirements and verification are later stages, not this adapter.

## Validation

Real SQLite regressions cover TaskBatch/Map sources, immutable projection checks,
wrong Run/session/Expansion/DAG, terminal failures, stale preserved resources,
worker overlap, parent conflicts, unrelated dirt, terminal replay after cleanup/parent
commit, non-terminal source revalidation, exact replay/reopen, write-before-ACK
forward recovery, source-kind collisions and old Swarm JSON/fingerprint compatibility.
Existing adoption, Swarm, UltraCode, permission and migration regressions remain gates.
