# ADR 0182: Agent Profile and Capability Model V1

[简体中文](../../zh-CN/adr/0182-agent-profile-capability-model-v1.md) · **English**

- Status: Accepted
- Date: 2026-09-28
- Scope: Declarative Agent roles, capability resolution, and per-binding runtime projection

## Context

Neuro Code already has provider capability facts, a concrete tool registry,
subagent capability manifests, and authoritative Permission, Workspace, and
Sandbox checks. The missing piece was one binding-time projection that made a
role's intent and the currently executable tool/context set explicit. Without
that projection, schema exposure, tool dispatch, provider-native tools, and
trace diagnostics could make separate decisions.

## Decision

The application uses this chain:

```text
AgentProfile → Capability Resolution → Runtime Binding → Effective Agent
```

`AgentProfile` is immutable declarative intent. It includes identity and role,
behavior and static guidance, provider-neutral model/reasoning policy, typed
capability requests, memory/context policy, execution-budget ceiling,
subagent/workspace-write policy, and verification intent. It contains no
provider endpoint, credential, session state, or mutable execution state.

Capability Resolution intersects requested capabilities with concrete runtime
availability, explicit provider and platform facts, profile override, current
permission and sandbox policy, and the exact parent capability ceiling:

```text
effective = requested ∩ runtime ∩ provider ∩ platform ∩ override
            ∩ permission ∩ sandbox ∩ parent
```

Unavailable requested capabilities retain one typed reason. Unknown provider
function-tool or hosted-tool support does not count as available. A local tool
is bound only when all of its typed requirements are effective. Provider-native
tool schemas are filtered at composition using profile intent and explicit
tool-wide denies; concrete provider capability facts further determine
availability. Path-scoped permission rules remain enforced at each call.

`EffectiveAgentBinding` is created once at the composition boundary and records
the selected profile/provider/model, reasoning policy, immutable capability
resolution, exact local and provider tool names, narrowed budget, security
constraint labels, and a deterministic fingerprint. Runtime uses the same
bound local catalog for both model-visible definitions and dispatch. Dynamic
MCP catalog refresh remains an explicit extension update and is accepted only
for a binding with `extension.invoke`.

The built-in profiles are Main, Explorer, Planner, Reviewer, Writable Worker,
and Leader. Existing subagent factories select Explorer or Writable Worker
explicitly; internal Planner and Leader bindings select their own profiles.
Unlabelled existing bindings remain Main for compatibility. Profile policy
only narrows capabilities and budgets. Main does not add static guidance, so
its system prefix is unchanged. Other static profile guidance is attached only
when a binding is created. Profile changes therefore occur at a binding/cache
boundary, not during an active request sequence.

Profiles do not grant authority. PermissionManager, canonical workspace
targeting, approval, sandbox enforcement, verification, managed-worktree
leases, and existing parent/child grants remain authoritative. Writable Worker
requires the current parent write ceiling, a writable sandbox, and the
existing managed-workspace relay; every actual mutation still passes the
existing tool pipeline. Project Memory writes remain application-owned.
`read_project_memory` keeps its stable schema; when the active binding lacks
the project scope/capability, dispatch returns a fail-closed error.

Trace records bounded profile identity, effective/unavailable capability
labels, budget, security labels, and the binding fingerprint. It excludes
guidance, prompts, arguments, credentials, and memory content. No database
schema, settings editor, new permission system, or second runtime is added.

## Consequences

Role wiring, provider/tool availability, and runtime capability projection now
share one testable binding contract. Dynamic Workflow can later request a
profile and narrower override, then reuse the same resolver and runtime
projection; it must continue to treat profiles as intent and preserve the
existing security authorities.
