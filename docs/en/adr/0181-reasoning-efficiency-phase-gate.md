# ADR 0181: Reasoning Efficiency Phase Gate Correction

[简体中文](../../zh-CN/adr/0181-reasoning-efficiency-phase-gate.md) · **English**

- Status: Accepted
- Date: 2026-09-27
- Scope: Advisory execution phases, evidence sufficiency, finalization diagnostics, and phase telemetry
- Supersedes: Unknown-state phase-gate semantics in ADR 0180

## Context

Real DeepSeek A/B runs after Reasoning Efficiency V1 regressed: a planless
repository review remained in `EXPLORE`, repeatedly requested reads, reached a
tool safety limit, and fell back to the execution finalizer. The runtime
required a completed Plan or a complete Working Set to leave exploration,
while ordinary `NEXT_STEPS` entries and unavailable Working Set snapshots were
both treated as known unresolved work. The missing completion marker was thus
misread as evidence of insufficiency. Trace phase totals also included
finalizer requests while the turn-level model-request count did not.

A follow-up real DeepSeek A/B then exposed phase thrashing: `UNKNOWN` was used
as an EXPLORE-to-ANALYZE trigger, after which analysis tool calls backtracked to
EXPLORE and each new evidence batch re-entered ANALYZE. This increased model
requests and provider time.

## Decision

**Execution phase remains advisory.** It guides the model but does not become a
new Runtime authority, suppress tools, change Supervisor decisions, alter user
reasoning effort, or change safety limits.

**Evidence sufficiency is tri-state.**

- `KNOWN_INSUFFICIENT` means an explicit blocker exists: an incomplete active
  Plan, an explicit `UNRESOLVED_WORK` Working Set entry, or pending required
  verification.
- `SUFFICIENT` requires successful evidence progress and either a completed
  existing Plan or, when there is no Plan, a complete Working Set with its goal
  and progress populated and both unresolved-work and next-step sections empty.
- `UNKNOWN` means neither completion nor an explicit blocker is established.
  It is not treated as insufficient.

Ordinary `NEXT_STEPS` text is advisory, not a requirement. A missing or
unreadable Working Set makes sufficiency `UNKNOWN`; it does not force continued
exploration. `FINALIZE` is recorded after a no-tool model response when there
is no explicit unresolved requirement, no incomplete active Plan, and required
verification is satisfied or explicitly blocked. Unknown completion metadata
does not block that diagnostic transition.

**`UNKNOWN` is neutral for phase selection.** It neither triggers ANALYZE nor
requires continued exploration, and it does not block normal finalization.
`EXPLORE → ANALYZE` requires the positive `SUFFICIENT` signal from existing
runtime facts; evidence counts, low-information patterns, and an unknown state
cannot substitute for readiness. A planless task whose completion remains
unknown may stay in EXPLORE and proceed directly to synthesis/finalization when
the model stops requesting tools and no explicit blocker remains.

**Low-information exploration remains batching-only.** After two new singleton
evidence rounds, one bounded notice asks for identifiable independent reads to
be batched and keeps the phase in `EXPLORE`. If unknown state continues with
more singleton reads, one bounded `excessive_exploration` checkpoint advises
the model to request tools only for concrete unresolved evidence gaps, or
otherwise synthesize. It is an EXPLORE checkpoint, not a phase transition. A
successful independent batch with unknown sufficiency also remains in EXPLORE.

**Analysis backtrack has readiness-edge hysteresis.** A model tool request in
`ANALYZE` records a backtrack and returns evidence work to `EXPLORE`. After
that backtrack, repeated `SUFFICIENT`, `UNKNOWN`, or ordinary evidence-count
growth cannot by itself re-enter ANALYZE. If the backtrack occurred while
sufficiency was already positive, the controller first observes a
non-sufficient state and then requires a fresh positive `SUFFICIENT` state.
When the backtrack itself leaves sufficiency unknown or insufficient, a later
positive state is the required readiness edge. Duplicate or failed output does
not masquerade as new evidence. Independent calls remain batched by the model
and existing scheduler; dependent calls remain sequential.

## Trace contract

Turn and phase summaries report `main_model_requests` separately from
`finalizer_provider_requests`, and keep `provider_time_main_ms` separate from
`finalizer_elapsed_ms`. Finalizer elapsed time includes bounded orchestration
around its Provider calls, so it is not labeled as Provider time. Finalizer
dispatches no longer inflate main-model phase request totals. Efficiency events
may report the tri-state sufficiency value; all trace data remains metadata-only
and excludes prompts, tool payloads, and hidden reasoning.

## Validation

Deterministic regressions cover neutral unknown-state routing, planless
finalization, positive readiness, readiness-edge hysteresis after analysis
backtracking, normal finalization with advisory Next Steps, preservation of
explicit blockers, bounded excessive-exploration guidance, evidence
deduplication, and separated main/finalizer Trace metrics. The repository
review fixture checks correctness, evidence coverage, model requests, tools,
batches, phase counts, output tokens, cache reuse, and simulated Provider time.
Its timings are test data and are not claims about live Provider performance.
