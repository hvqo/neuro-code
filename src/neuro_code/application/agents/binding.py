"""Deterministic Agent profile capability resolution."""

from __future__ import annotations

from collections.abc import Collection

from neuro_code.application.ports.agent_profiles import (
    AgentCapabilityResolution,
    AgentCapabilityUnavailableReason,
    EffectiveAgentBinding,
    UnavailableAgentCapability,
)
from neuro_code.application.ports.model import (
    ModelCapability,
    ModelCapabilitySet,
)
from neuro_code.domain.agents.profile import (
    AgentCapability,
    AgentProfile,
    AgentProfileOverride,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import ExecutionBudget, ToolCallBudget
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.shared.errors import ConfigurationError

ALL_AGENT_CAPABILITIES = frozenset(AgentCapability)

_TOOL_CAPABILITIES: dict[str, AgentCapability] = {
    **dict.fromkeys(
        (
            "read_file",
            "read_files",
            "list_dir",
            "list_tree",
            "glob",
            "grep",
            "grep_many",
            "workspace_diff",
            "skill",
        ),
        AgentCapability.WORKSPACE_READ,
    ),
    "lsp": AgentCapability.LSP,
    "search_replace": AgentCapability.WORKSPACE_WRITE,
    "apply_patch": AgentCapability.WORKSPACE_WRITE,
    "bash": AgentCapability.SHELL_EXECUTE,
    "git_inspect": AgentCapability.GIT_INSPECT,
    "web_search": AgentCapability.WEB_SEARCH,
    "google_search": AgentCapability.WEB_SEARCH,
    "x_search": AgentCapability.WEB_SEARCH,
    "web_fetch": AgentCapability.WEB_FETCH,
    "url_context": AgentCapability.WEB_FETCH,
    "read_project_memory": AgentCapability.PROJECT_MEMORY_READ,
    "subagent": AgentCapability.SUBAGENT_SPAWN,
    "update_plan": AgentCapability.TASK_PLAN,
    "ask_user": AgentCapability.USER_INTERACT,
    "session_history": AgentCapability.SESSION_HISTORY_READ,
    "session_working_set": AgentCapability.WORKING_SET_MANAGE,
    "new_context": AgentCapability.CONTEXT_ROLLOVER,
    "task_output": AgentCapability.BACKGROUND_MANAGE,
    "wait_tasks": AgentCapability.BACKGROUND_MANAGE,
    "kill_task": AgentCapability.BACKGROUND_MANAGE,
    "create_terminal": AgentCapability.TERMINAL_INTERACT,
    "terminal_exec": AgentCapability.TERMINAL_INTERACT,
    "terminal_output": AgentCapability.TERMINAL_INTERACT,
    "terminal_write": AgentCapability.TERMINAL_INTERACT,
    "terminal_resize": AgentCapability.TERMINAL_INTERACT,
    "terminal_wait": AgentCapability.TERMINAL_INTERACT,
    "terminal_kill": AgentCapability.TERMINAL_INTERACT,
    "terminal_start": AgentCapability.TERMINAL_INTERACT,
}

_READ_TOOL_NAMES = frozenset(
    {
        "read_file",
        "read_files",
        "list_dir",
        "list_tree",
        "glob",
        "grep",
        "grep_many",
        "workspace_diff",
        "skill",
        "lsp",
        "git_inspect",
        "read_project_memory",
        "session_history",
    }
)
_WRITE_TOOL_NAMES = frozenset({"search_replace", "apply_patch"})
_SEARCH_TOOL_NAMES = frozenset({"web_search", "google_search"})
_FETCH_TOOL_NAMES = frozenset({"web_fetch", "url_context"})


def tool_names_to_capabilities(tool_names: Collection[str]) -> frozenset[AgentCapability]:
    """Map concrete, already-authorized registry entries to intent capabilities."""

    names = frozenset(tool_names)
    capabilities = {
        capability for name in names if (capability := _TOOL_CAPABILITIES.get(name)) is not None
    }
    if names.difference(_TOOL_CAPABILITIES):
        capabilities.add(AgentCapability.EXTENSION_INVOKE)
    if names.intersection(_READ_TOOL_NAMES):
        capabilities.add(AgentCapability.WORKSPACE_READ)
    if names.intersection(_WRITE_TOOL_NAMES):
        capabilities.add(AgentCapability.WORKSPACE_WRITE)
    return frozenset(capabilities)


def tool_capability(name: str) -> AgentCapability:
    """Return the profile capability that guards one concrete tool."""

    return _TOOL_CAPABILITIES.get(name, AgentCapability.EXTENSION_INVOKE)


def runtime_available_capabilities(
    *,
    tool_names: Collection[str],
    provider_capabilities: ModelCapabilitySet | None,
    application_capabilities: Collection[AgentCapability] = (),
    project_memory_context_available: bool = False,
    verification_available: bool = False,
    provider_tool_names: Collection[str] = (),
) -> frozenset[AgentCapability]:
    """Derive only capabilities backed by current concrete runtime bindings."""

    names = frozenset(tool_names)
    capabilities = set(tool_names_to_capabilities(names))
    capabilities.update(application_capabilities)
    if project_memory_context_available:
        capabilities.add(AgentCapability.PROJECT_MEMORY_READ)
    else:
        # The Project Memory tool definition stays stable in the Main tool
        # catalog for cache continuity, but a binding without an active scope
        # cannot read project memory and must not report the capability effective.
        capabilities.discard(AgentCapability.PROJECT_MEMORY_READ)
    if verification_available:
        capabilities.add(AgentCapability.VERIFICATION_RUN)
    native_tools = set(provider_tool_names)
    if provider_capabilities is not None and (
        (
            provider_capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH)
            and native_tools.intersection({"web_search", "google_search"})
        )
        or (
            provider_capabilities.supports(ModelCapability.HOSTED_X_SEARCH)
            and "x_search" in native_tools
        )
    ):
        capabilities.add(AgentCapability.WEB_SEARCH)
    if (
        provider_capabilities is not None
        and provider_capabilities.supports(ModelCapability.HOSTED_WEB_FETCH)
        and native_tools.intersection({"web_fetch", "url_context"})
    ):
        capabilities.add(AgentCapability.WEB_FETCH)
    return frozenset(capabilities)


def provider_available_capabilities(
    *,
    tool_names: Collection[str],
    provider_tool_names: Collection[str] = (),
    provider_capabilities: ModelCapabilitySet | None,
) -> frozenset[AgentCapability]:
    """Resolve provider-dependent capabilities without guessing hosted support."""

    names = frozenset(tool_names)
    available = set(
        ALL_AGENT_CAPABILITIES - {AgentCapability.WEB_SEARCH, AgentCapability.WEB_FETCH}
    )
    function_tools_supported = provider_capabilities is not None and provider_capabilities.supports(
        ModelCapability.FUNCTION_TOOLS
    )
    native_search_tools = set(provider_tool_names)
    native_search_available = provider_capabilities is not None and (
        (
            provider_capabilities.supports(ModelCapability.HOSTED_WEB_SEARCH)
            and native_search_tools.intersection({"web_search", "google_search"})
        )
        or (
            provider_capabilities.supports(ModelCapability.HOSTED_X_SEARCH)
            and "x_search" in native_search_tools
        )
    )
    if native_search_available or (
        function_tools_supported and names.intersection(_SEARCH_TOOL_NAMES)
    ):
        available.add(AgentCapability.WEB_SEARCH)
    native_fetch_available = (
        provider_capabilities is not None
        and provider_capabilities.supports(ModelCapability.HOSTED_WEB_FETCH)
        and native_search_tools.intersection({"web_fetch", "url_context"})
    )
    if native_fetch_available or (
        function_tools_supported and names.intersection(_FETCH_TOOL_NAMES)
    ):
        available.add(AgentCapability.WEB_FETCH)
    return frozenset(available)


def resolve_agent_binding(
    *,
    profile: AgentProfile,
    provider_name: str,
    model_name: str,
    reasoning_effort: ReasoningEffort,
    tool_names: Collection[str],
    runtime_capabilities: Collection[AgentCapability],
    provider_capabilities: Collection[AgentCapability],
    provider_tool_names: Collection[str] = (),
    platform_capabilities: Collection[AgentCapability],
    permission_capabilities: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    permission_denied_tool_names: Collection[str] = (),
    security_capabilities: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    parent_capabilities: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    execution_budget: ExecutionBudget,
    sandbox_profile: SandboxProfile,
    override: AgentProfileOverride | None = None,
) -> EffectiveAgentBinding:
    """Resolve a declarative profile into an immutable executable binding.

    The function is deterministic and side-effect free. Runtime security remains
    authoritative at each tool call; this result only narrows the model-facing
    catalog and application-managed context projection.
    """

    if not isinstance(profile, AgentProfile):
        raise ConfigurationError("Agent profile is missing or invalid")
    if override is not None and not isinstance(override, AgentProfileOverride):
        raise ConfigurationError("Agent profile override is invalid")
    if not isinstance(sandbox_profile, SandboxProfile):
        raise ConfigurationError("Agent profile sandbox evidence is missing")
    if not isinstance(execution_budget, ExecutionBudget):
        raise ConfigurationError("Agent profile execution budget is missing")
    if not isinstance(reasoning_effort, ReasoningEffort):
        raise ConfigurationError("Agent profile reasoning effort is invalid")
    normalized_tool_names = tuple(tool_names)
    if len(normalized_tool_names) > 512 or len(set(normalized_tool_names)) != len(
        normalized_tool_names
    ):
        raise ConfigurationError("Agent profile runtime tool catalog is invalid")
    if any(
        not isinstance(name, str) or not name or len(name.encode("utf-8")) > 256
        for name in normalized_tool_names
    ):
        raise ConfigurationError("Agent profile runtime tool catalog contains an invalid name")

    override_disabled = override.disabled_capabilities if override is not None else frozenset()
    requested = profile.capability_policy
    override_allowed = ALL_AGENT_CAPABILITIES - override_disabled
    runtime = _canonical_capabilities(runtime_capabilities, field_name="runtime capabilities")
    provider = _canonical_capabilities(provider_capabilities, field_name="provider capabilities")
    platform = _canonical_capabilities(platform_capabilities, field_name="platform capabilities")
    permission = _canonical_capabilities(
        permission_capabilities,
        field_name="permission capabilities",
    )
    security = _canonical_capabilities(security_capabilities, field_name="security capabilities")
    parent = _canonical_capabilities(parent_capabilities, field_name="parent capabilities")
    permission_denied_names = frozenset(permission_denied_tool_names)
    if not permission_denied_names.issubset(normalized_tool_names):
        raise ConfigurationError("permission-denied tools exceed the runtime catalog")
    effective = (
        requested
        & override_allowed
        & runtime
        & provider
        & platform
        & permission
        & security
        & parent
    )
    unavailable: list[UnavailableAgentCapability] = []
    for capability in sorted(requested - effective, key=lambda item: item.value):
        reason = (
            AgentCapabilityUnavailableReason.PROFILE_OVERRIDE
            if capability not in override_allowed
            else AgentCapabilityUnavailableReason.PARENT_POLICY
            if capability not in parent
            else AgentCapabilityUnavailableReason.PERMISSION_POLICY
            if capability not in permission
            else AgentCapabilityUnavailableReason.SANDBOX_POLICY
            if capability not in security
            else AgentCapabilityUnavailableReason.PLATFORM_UNAVAILABLE
            if capability not in platform
            else AgentCapabilityUnavailableReason.PROVIDER_UNAVAILABLE
            if capability not in provider
            else AgentCapabilityUnavailableReason.RUNTIME_UNAVAILABLE
        )
        unavailable.append(UnavailableAgentCapability(capability, reason))
    resolution = AgentCapabilityResolution(
        requested=requested,
        runtime_available=runtime,
        provider_available=provider,
        platform_available=platform,
        profile_override_allowed=override_allowed,
        permission_allowed=permission,
        security_allowed=security,
        parent_available=parent,
        effective=effective,
        unavailable=tuple(unavailable),
    )
    bound_names = tuple(
        name
        for name in normalized_tool_names
        if tool_required_capabilities(name).issubset(effective)
        and name not in permission_denied_names
    )
    normalized_provider_names = tuple(provider_tool_names)
    if len(normalized_provider_names) > 64 or len(set(normalized_provider_names)) != len(
        normalized_provider_names
    ):
        raise ConfigurationError("Agent profile provider tool catalog is invalid")
    if any(
        not isinstance(name, str) or not name or len(name.encode("utf-8")) > 256
        for name in normalized_provider_names
    ):
        raise ConfigurationError("Agent profile provider tool catalog contains an invalid name")
    bound_provider_names = tuple(
        name for name in normalized_provider_names if tool_capability(name) in effective
    )
    budget = intersect_execution_budgets(execution_budget, profile.execution_budget_ceiling)
    if override is not None:
        budget = intersect_execution_budgets(budget, override.execution_budget_ceiling)
    effective_reasoning = apply_reasoning_policy(reasoning_effort, profile)
    constraints = (
        "permission_checked_per_tool_call",
        "workspace_targets_revalidated_per_tool_call",
        f"sandbox:{sandbox_profile.value}",
    )
    return EffectiveAgentBinding(
        profile=profile,
        provider_name=provider_name,
        model_name=model_name,
        reasoning_effort=effective_reasoning,
        capability_resolution=resolution,
        bound_tool_names=bound_names,
        bound_provider_tool_names=bound_provider_names,
        execution_budget=budget,
        security_constraints=constraints,
    )


def _canonical_capabilities(
    values: Collection[AgentCapability],
    *,
    field_name: str,
) -> frozenset[AgentCapability]:
    result = frozenset(values)
    if not all(isinstance(value, AgentCapability) for value in result):
        raise ConfigurationError(f"{field_name} are invalid")
    return result


def tool_required_capabilities(name: str) -> frozenset[AgentCapability]:
    """Return every profile capability required to expose one concrete tool."""

    capability = tool_capability(name)
    if name in _READ_TOOL_NAMES:
        return frozenset({capability, AgentCapability.WORKSPACE_READ})
    if name in _WRITE_TOOL_NAMES:
        return frozenset({capability, AgentCapability.WORKSPACE_WRITE})
    return frozenset({capability})


def apply_reasoning_policy(requested: ReasoningEffort, profile: AgentProfile) -> ReasoningEffort:
    """Apply only a profile's explicit provider-neutral reasoning ceiling."""

    if not isinstance(requested, ReasoningEffort) or not isinstance(profile, AgentProfile):
        raise ConfigurationError("reasoning policy inputs are invalid")
    # Ultracode is an application-level orchestration strategy. Its provider
    # projection is MAX, but the runtime must retain ULTRACODE for workflow
    # selection and durable execution semantics.
    if requested is ReasoningEffort.ULTRACODE:
        return requested
    order = (
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH,
        ReasoningEffort.MAX,
    )
    selected: ReasoningEffort = requested
    policy = profile.reasoning_policy
    if policy.effort is not None and order.index(policy.effort.effective) < order.index(selected):
        selected = policy.effort.effective
    if policy.maximum_effort is not None and order.index(
        policy.maximum_effort.effective
    ) < order.index(selected):
        selected = policy.maximum_effort.effective
    return selected


def intersect_execution_budgets(
    base: ExecutionBudget,
    ceiling: ExecutionBudget | None,
) -> ExecutionBudget:
    if ceiling is None:
        return base
    tool_names = {item.tool_name for item in (*base.per_tool_limits, *ceiling.per_tool_limits)}
    max_per_tool = min(base.max_calls_per_tool, ceiling.max_calls_per_tool)
    per_tool_limits = tuple(
        ToolCallBudget(
            name,
            min(base.limit_for_tool(name), ceiling.limit_for_tool(name)),
        )
        for name in sorted(tool_names)
        if min(base.limit_for_tool(name), ceiling.limit_for_tool(name)) < max_per_tool
    )
    return ExecutionBudget(
        max_model_calls=min(base.max_model_calls, ceiling.max_model_calls),
        max_tool_rounds=min(base.max_tool_rounds, ceiling.max_tool_rounds),
        max_tool_calls=min(base.max_tool_calls, ceiling.max_tool_calls),
        max_calls_per_tool=max_per_tool,
        max_wall_seconds=_optional_seconds_min(
            base.max_wall_seconds,
            ceiling.max_wall_seconds,
        ),
        max_input_tokens=_optional_int_min(base.max_input_tokens, ceiling.max_input_tokens),
        max_output_tokens=_optional_int_min(base.max_output_tokens, ceiling.max_output_tokens),
        max_total_tokens=_optional_int_min(base.max_total_tokens, ceiling.max_total_tokens),
        per_tool_limits=per_tool_limits,
    )


def _optional_int_min(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _optional_seconds_min(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


__all__ = [
    "ALL_AGENT_CAPABILITIES",
    "apply_reasoning_policy",
    "intersect_execution_budgets",
    "provider_available_capabilities",
    "resolve_agent_binding",
    "runtime_available_capabilities",
    "tool_capability",
    "tool_names_to_capabilities",
]
