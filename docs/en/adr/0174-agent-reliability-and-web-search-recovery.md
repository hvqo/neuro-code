# ADR 0174: Agent Reliability, External Evidence, and Web Search Recovery

[简体中文](../../zh-CN/adr/0174-agent-reliability-and-web-search-recovery.md) · **English**

- Status: Accepted
- Date: 2026-09-24
- Scope: Runtime Web Search capability resolution, external-evidence guidance, bounded recovery from repeated execution patterns

## Context

An Agent can correctly discover that a requested project is absent from the
workspace, yet fail to continue with public evidence when its MAIN provider has
no native search tool. A configured Web Search preference alone does not prove
that the active binding has an executable search route or a model-visible
`web_search` definition. Separately, periodic exploration cycles were able to
reach `STUCK` on their first detection, leaving no opportunity for a bounded
strategy change.

Runtime must expose only capabilities supported by trusted model/provider
facts and concrete backends, preserve workspace-first behavior, keep ordinary
tool batching and safety boundaries, and retain finite loop protection.

## Decision

### Effective Web Search resolution

`WebSearchMode.AUTO` resolves after provider capabilities, client tools, and
the binding's `allowed_tool_names` are known:

1. Use MAIN inline hosted search only when the resolved provider explicitly
   supports it and can combine it with the client tools that will be exposed.
2. Otherwise use an explicitly configured, executable `WEB_SEARCH` route.
3. If no route is configured, inspect configured provider profiles in stable
   `(casefold(name), name)` order and select the first profile whose concrete
   hosted-search backend resolves and whose configured credentials are
   available. MAIN and MAIN failover profiles are excluded from this
   independent-sidecar discovery.
4. Do not elevate `UNKNOWN` model or backend capability to `SUPPORTED`. If no
   permitted executable path exists, omit the tool and expose a typed
   unavailable reason on the effective binding.

The composition root constructs the local `web_search` tool only for an
executable sidecar path and only when the binding allows that tool. Existing
native provider definitions remain owned by their trusted adapters. The typed
`RuntimeWebCapabilityInspection` reports effective availability, path,
unavailable reason, safe provider/model labels, and the effective Web Fetch
path. It never carries credentials or endpoint values. TUI `/status` uses the
same post-composition inspection, so it describes the active runtime rather
than a persisted preference.

### Workspace-first external evidence

Stable Runtime guidance asks for local workspace evidence first. If the
workspace does not contain the evidence required for a public or external
claim, the Agent uses model-visible `web_search` when available. If it is not
available, the Agent must state that limitation, distinguish verified local
facts from unverified external claims, and avoid presenting the external part
as verified. Shell remains available under existing permissions and sandbox
rules; this decision neither bans explicitly requested shell networking nor
allows shell scraping to silently substitute for public research.

The guidance is stable context, not a per-turn rewrite. Replan guidance adds
only the short instruction to stop repeating local discovery after absence is
established, escalate to Web Search when available, or state the capability
blocker.

### Detector, recovery, terminal policy

Repeated action/observation, repeated error, periodic-cycle, and no-progress
detectors do not themselves terminate a turn on first detection. A first
detection creates bounded in-memory state for that turn and returns `REPLAN`.
The state contains a typed reason, a hash of the canonical behavior signature,
optional cycle period, and the current tool-call count boundary. It stores no
raw arguments or results and is not persisted.

The Agent receives one bounded strategy opportunity. Novel evidence, workspace
change, plan change, verification progress, or external-state progress clears
the corresponding recovery pressure. If the anomaly persists without
meaningful progress after the minimum bounded strategy calls, the supervisor
returns typed terminal `STUCK`. Existing model/tool budgets remain the outer
execution bound. Per-turn recovery state and all hashes disappear when the
turn ends.

`SupervisorDecision` and terminal completion diagnostics carry bounded
`reason_code`, `replan_attempted`, `replan_count`, optional `cycle_period`, and
`progress_since_replan` facts. The TUI maps known terminal reasons to concise
localized messages. Neither diagnostics nor user-facing capability inspection
contains arguments, result bodies, secrets, secret-bearing endpoints, URLs, or
internal signatures.

## Consequences

- A DeepSeek or other OpenAI-compatible MAIN can use a separately configured,
  trusted Hosted Search backend through the existing canonical Web Search
  service.
- `AUTO` is deterministic and capability-aware; missing or filtered routes
  cannot appear enabled without an explanation in the active runtime status.
- Normal local discovery can transition to external evidence without treating
  that strategy change as a loop. Repeated non-progressing behavior still
  reaches a bounded terminal state.
- Canonical conversation history, permissions, sandbox policy, provider
  affinity, batching order, and existing Web Fetch safety policy remain
  unchanged.
- This decision does not add browser automation, generic shell network bans,
  full runtime tracing, or provider-specific cache controls.
