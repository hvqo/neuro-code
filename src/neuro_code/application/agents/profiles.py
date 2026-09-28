"""Built-in Agent profile catalog.

The catalog contains only immutable intent. Runtime availability is resolved
separately from concrete tools, providers, platform, and security policy.
"""

from __future__ import annotations

from neuro_code.application.execution_policy import ExecutionBudgetPolicy
from neuro_code.domain.agents.profile import (
    AgentBehaviorPolicy,
    AgentCapability,
    AgentContextPolicy,
    AgentMemoryPolicy,
    AgentProfile,
    AgentRole,
    AgentSubagentPolicy,
    AgentVerificationPolicy,
    AgentWorkspacePolicy,
    WorkspaceWriteMode,
)
from neuro_code.domain.execution import ExecutionBudget

_READ = frozenset(
    {
        AgentCapability.WORKSPACE_READ,
        AgentCapability.GIT_INSPECT,
        AgentCapability.LSP,
        AgentCapability.SESSION_HISTORY_READ,
    }
)
_COMMON_READ = frozenset(
    {
        *_READ,
        AgentCapability.WEB_SEARCH,
        AgentCapability.WEB_FETCH,
        AgentCapability.PROJECT_MEMORY_READ,
    }
)
_MAIN_CAPABILITIES = frozenset(
    {
        *_COMMON_READ,
        AgentCapability.WORKSPACE_WRITE,
        AgentCapability.SHELL_EXECUTE,
        AgentCapability.TASK_PLAN,
        AgentCapability.USER_INTERACT,
        AgentCapability.WORKING_SET_MANAGE,
        AgentCapability.CONTEXT_ROLLOVER,
        AgentCapability.BACKGROUND_MANAGE,
        AgentCapability.TERMINAL_INTERACT,
        AgentCapability.EXTENSION_INVOKE,
    }
)


def _budget(max_steps: int) -> ExecutionBudget:
    return ExecutionBudgetPolicy.from_max_steps(max_steps)


MAIN_AGENT_PROFILE = AgentProfile(
    profile_id="main",
    name="Main Agent",
    description="User-facing Neuro Code agent using the active session policy.",
    role=AgentRole.MAIN,
    behavior=AgentBehaviorPolicy.STANDARD,
    capability_policy=_MAIN_CAPABILITIES,
    memory_policy=AgentMemoryPolicy(read_project_memory=True),
    context_policy=AgentContextPolicy(include_project_memory_snapshot=True),
    workspace_policy=AgentWorkspacePolicy(read=True, write_mode=WorkspaceWriteMode.SCOPED),
    verification_policy=AgentVerificationPolicy(),
)

EXPLORER_AGENT_PROFILE = AgentProfile(
    profile_id="explorer",
    name="Explorer",
    description="Collect bounded repository and public-web evidence without workspace writes.",
    role=AgentRole.EXPLORER,
    behavior=AgentBehaviorPolicy.READ_ONLY_EXPLORATION,
    capability_policy=_COMMON_READ,
    system_guidance=(
        "Inspect evidence read-only. Batch independent reads and searches; report concrete "
        "findings and uncertainty without modifying the workspace."
    ),
    memory_policy=AgentMemoryPolicy(read_project_memory=True),
    context_policy=AgentContextPolicy(include_project_memory_snapshot=True),
    execution_budget_ceiling=_budget(12),
    workspace_policy=AgentWorkspacePolicy(read=True),
)

PLANNER_AGENT_PROFILE = AgentProfile(
    profile_id="planner",
    name="Planner",
    description="Produce a bounded plan from repository evidence without executing mutations.",
    role=AgentRole.PLANNER,
    behavior=AgentBehaviorPolicy.PLANNING,
    capability_policy=frozenset(
        {*_READ, AgentCapability.TASK_PLAN, AgentCapability.VERIFICATION_RUN}
    ),
    system_guidance=(
        "Return only the bounded plan requested by the application. Inspect evidence as needed; "
        "do not execute workspace mutations or claim that planned work has run."
    ),
    execution_budget_ceiling=_budget(8),
    workspace_policy=AgentWorkspacePolicy(read=True),
    verification_policy=AgentVerificationPolicy(verify_workspace_mutations=False),
)

REVIEWER_AGENT_PROFILE = AgentProfile(
    profile_id="reviewer",
    name="Reviewer",
    description="Review repository changes and verification evidence without editing source.",
    role=AgentRole.REVIEWER,
    behavior=AgentBehaviorPolicy.REVIEW,
    capability_policy=frozenset(
        {
            *_READ,
            AgentCapability.VERIFICATION_RUN,
            AgentCapability.TASK_PLAN,
        }
    ),
    system_guidance=(
        "Review the supplied changes and verification evidence. Do not modify source files; "
        "report findings with evidence and severity."
    ),
    execution_budget_ceiling=_budget(12),
    workspace_policy=AgentWorkspacePolicy(read=True),
)

WRITABLE_WORKER_AGENT_PROFILE = AgentProfile(
    profile_id="writable_worker",
    name="Writable Worker",
    description="Implement a bounded task in a managed writable workspace.",
    role=AgentRole.WRITABLE_WORKER,
    behavior=AgentBehaviorPolicy.IMPLEMENTATION,
    capability_policy=frozenset(
        {
            AgentCapability.WORKSPACE_READ,
            AgentCapability.WORKSPACE_WRITE,
            AgentCapability.GIT_INSPECT,
            AgentCapability.LSP,
            AgentCapability.TASK_PLAN,
            AgentCapability.VERIFICATION_RUN,
        }
    ),
    system_guidance=(
        "Implement only the assigned bounded change inside the managed workspace. Read before "
        "editing and report what you verified; do not expand into unrelated changes."
    ),
    execution_budget_ceiling=_budget(12),
    workspace_policy=AgentWorkspacePolicy(
        read=True,
        write_mode=WorkspaceWriteMode.MANAGED_WORKTREE,
    ),
)

LEADER_AGENT_PROFILE = AgentProfile(
    profile_id="leader",
    name="Leader",
    description="Coordinate an existing bounded Task DAG and own its terminal decision.",
    role=AgentRole.LEADER,
    behavior=AgentBehaviorPolicy.ORCHESTRATION,
    capability_policy=frozenset({AgentCapability.SUBAGENT_SPAWN, AgentCapability.TASK_PLAN}),
    system_guidance=(
        "Use only the bounded orchestration choices supplied by the application. Do not claim "
        "that a worker ran or adopt its changes unless durable runtime evidence confirms it."
    ),
    execution_budget_ceiling=_budget(1),
    subagent_policy=AgentSubagentPolicy(allow_spawn=True, max_children=8, max_depth=1),
    workspace_policy=AgentWorkspacePolicy(read=False),
    verification_policy=AgentVerificationPolicy(verify_workspace_mutations=False),
)

BUILTIN_AGENT_PROFILES: tuple[AgentProfile, ...] = (
    MAIN_AGENT_PROFILE,
    EXPLORER_AGENT_PROFILE,
    PLANNER_AGENT_PROFILE,
    REVIEWER_AGENT_PROFILE,
    WRITABLE_WORKER_AGENT_PROFILE,
    LEADER_AGENT_PROFILE,
)
_PROFILES_BY_ID = {profile.profile_id: profile for profile in BUILTIN_AGENT_PROFILES}


def builtin_agent_profile(profile_id: str) -> AgentProfile:
    """Resolve one known profile ID without accepting dynamic imports or fallback."""

    if not isinstance(profile_id, str):
        raise TypeError("profile_id must be text")
    try:
        return _PROFILES_BY_ID[profile_id]
    except KeyError as error:
        raise ValueError("unknown built-in Agent profile") from error


__all__ = [
    "BUILTIN_AGENT_PROFILES",
    "EXPLORER_AGENT_PROFILE",
    "LEADER_AGENT_PROFILE",
    "MAIN_AGENT_PROFILE",
    "PLANNER_AGENT_PROFILE",
    "REVIEWER_AGENT_PROFILE",
    "WRITABLE_WORKER_AGENT_PROFILE",
    "builtin_agent_profile",
]
