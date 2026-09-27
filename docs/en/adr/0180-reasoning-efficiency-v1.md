# ADR 0180: Reasoning Efficiency V1

[简体中文](../../zh-CN/adr/0180-reasoning-efficiency-v1.md) · **English**

- Status: Accepted
- Date: 2026-09-27
- Scope: Deterministic phase guidance and evidence sufficiency in the existing Main Agent loop

## Context

Execution Efficiency V1 reduced fragmented model/tool exchanges in a
repository-review fixture, but real A/B measurements showed that fewer model
requests did not necessarily reduce Provider time. Some requests still spent
long periods reasoning before asking for evidence. The earlier rule that
treated repeated singleton exploration as an `EXPLORE → ANALYZE` signal was
semantically wrong: fragmentation calls for batching remaining evidence needs,
not for claiming that the evidence is sufficient.

## Decision

**Low-information exploration stays in EXPLORE.** After two new, successful,
simple singleton evidence rounds, the existing per-turn controller emits one
bounded append-only batching notice and remains in `EXPLORE`. It asks the model
to reconcile remaining needs with the existing Plan and Working Set, request
known independent evidence together, and wait for dependent results. It does
not infer task completeness or issue another model request.

**ANALYZE requires deterministic evidence sufficiency.** The gate requires at
least one new successful evidence fingerprint, no known unresolved work, and
no pending verification. A completed existing structured Plan is the primary
completion signal. Only when no Plan exists may an existing Working Set prove
progress complete, and then its goal and progress sections must be populated
with no unresolved-work or next-step entries. Unknown Working Set state fails
closed. If the application cannot prove sufficiency from these existing
signals, it stays in `EXPLORE`.

**Act before broad deliberation.** The EXPLORE notice is short and stable. When
remaining files, searches, or checks are already identifiable, it asks for
those tool calls before long synthesis. The model still declares every call;
the existing scheduler and executable tool capabilities remain authoritative
for dependencies, parallel execution, permissions, and ordering.

**Analysis can safely backtrack.** A tool batch requested in `ANALYZE` records
an `analysis_backtrack` count. Evidence-only follow-up returns to `EXPLORE`
with one bounded instruction to name the remaining targeted needs and batch
independent reads. Workspace changes and verification retain their `VERIFY`
boundary while still incrementing the analysis-backtrack counter. New evidence
after verification can return to `EXPLORE`; no needed tool is suppressed.

**FINALIZE is a gated diagnostic phase.** It is recorded only after the model
returns without tool calls, no known unresolved work remains, an existing Plan
is complete if one exists, and verification is passed or every required item
is explicitly satisfied or blocked as unavailable. A terminal Supervisor
decision alone does not prove that the model needs no more tools. These checks
do not delay or change terminal behavior.

**Do not change Provider reasoning effort in V1.** User-selected effort stays
unchanged. This slice adds no provider-specific per-request controls, planner,
judge, step limit, or System Prompt rewrite. Existing append-only runtime
guidance leaves the Stable Prefix intact; Trace remains metadata-only and
non-authoritative.

## Trace

The existing Trace summary reports main-model and finalizer requests, output
tokens, and Provider time by the phase active when each request began. It also
reports `analysis_backtrack_count`, tool calls made from ANALYZE requests, and
returns to EXPLORE before finalization. Trace retains no prompt, tool payload,
or hidden reasoning.

## Deterministic benchmark

The repository-review fixture compares a fragmented baseline that continues
singleton reads after the batching notice and spends simulated Provider time
and output tokens on early deliberation with an adaptive fixture that batches
the three known independent remaining reads. Both read all five files and
produce the same finding. The measured fixture values are local test data, not
live Provider performance claims. A dependent-read variant remains sequential.

## Consequences

- Low-information rounds prompt evidence batching but never imply evidence
  sufficiency.
- Analysis starts only when existing runtime state supports it; uncertainty
  leaves the controller in EXPLORE.
- ANALYZE and VERIFY may backtrack without restricting correct follow-up.
- Provider-level reasoning policy, Prompt Cache behavior, Supervisor, and
  durable history remain unchanged.
- Phase metrics make future A/B runs able to separate time spent in EXPLORE,
  ANALYZE, VERIFY, and FINALIZE without recording hidden reasoning.

## Validation

Deterministic tests cover low-information batching without a phase change,
evidence sufficiency and fail-closed unknown state, analysis and verification
backtracks, finalization gates, phase telemetry, append-only contexts, stable
System content, dependent reads, correctness, and durable history.
