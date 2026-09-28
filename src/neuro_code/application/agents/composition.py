"""Composition helper for resolving a profile from concrete binding inputs."""

from __future__ import annotations

from collections.abc import Collection

from neuro_code.application.agents.binding import (
    ALL_AGENT_CAPABILITIES,
    provider_available_capabilities,
    resolve_agent_binding,
    runtime_available_capabilities,
    tool_names_to_capabilities,
    tool_required_capabilities,
)
from neuro_code.application.permissions.policy import (
    PermissionDecisionSource,
    PermissionEffect,
    PermissionManager,
)
from neuro_code.application.ports.agent_profiles import EffectiveAgentBinding
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider
from neuro_code.application.ports.tools import ToolCollection
from neuro_code.domain.agents.profile import (
    AgentCapability,
    AgentProfile,
    AgentProfileOverride,
    WorkspaceWriteMode,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import ExecutionBudget
from neuro_code.domain.sandbox.models import SandboxProfile


def bind_agent_profile(
    *,
    profile: AgentProfile,
    provider: ModelProvider,
    tools: ToolCollection,
    sandbox_profile: SandboxProfile,
    execution_budget: ExecutionBudget,
    reasoning_effort: ReasoningEffort,
    active_project_memory: bool = False,
    verification_available: bool = False,
    application_capabilities: Collection[AgentCapability] = (),
    capability_ceiling_tool_names: Collection[str] | None = None,
    provider_tool_names: Collection[str] = (),
    managed_workspace: bool = False,
    permission_capabilities: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    permissions: PermissionManager | None = None,
    override: AgentProfileOverride | None = None,
) -> EffectiveAgentBinding:
    """Resolve once while composing a binding, never inside a model step."""

    names = tuple(definition.name for definition in tools.definitions())
    normalized_provider_tool_names = tuple(provider_tool_names)
    provider_capabilities = getattr(provider, "capabilities", None)
    if not isinstance(provider_capabilities, ModelCapabilitySet):
        provider_capabilities = None
    runtime = runtime_available_capabilities(
        tool_names=names,
        provider_capabilities=provider_capabilities,
        application_capabilities=application_capabilities,
        project_memory_context_available=active_project_memory,
        verification_available=verification_available,
        provider_tool_names=normalized_provider_tool_names,
    )
    provider_available = provider_available_capabilities(
        tool_names=names,
        provider_tool_names=normalized_provider_tool_names,
        provider_capabilities=provider_capabilities,
    )
    platform = set(ALL_AGENT_CAPABILITIES)
    if not profile.workspace_policy.read:
        platform.difference_update(
            {
                AgentCapability.WORKSPACE_READ,
                AgentCapability.GIT_INSPECT,
                AgentCapability.LSP,
            }
        )
    if not sandbox_profile.workspace_writable:
        platform.discard(AgentCapability.WORKSPACE_WRITE)
    security = set(ALL_AGENT_CAPABILITIES)
    if not sandbox_profile.workspace_writable:
        security.discard(AgentCapability.WORKSPACE_WRITE)
    if (
        profile.workspace_policy.write_mode is WorkspaceWriteMode.MANAGED_WORKTREE
        and not managed_workspace
    ):
        security.discard(AgentCapability.WORKSPACE_WRITE)
    if capability_ceiling_tool_names is None:
        parent = ALL_AGENT_CAPABILITIES
    else:
        parent = tool_names_to_capabilities(capability_ceiling_tool_names)
    permission = set(permission_capabilities)
    denied_tools: frozenset[str] = frozenset()
    if permissions is not None:
        denied_tools = _explicitly_denied_tool_names(permissions, tools, names)
        permission.difference_update(_explicitly_denied_capabilities(names, denied_tools))
    return resolve_agent_binding(
        profile=profile,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        reasoning_effort=reasoning_effort,
        tool_names=names,
        provider_tool_names=normalized_provider_tool_names,
        runtime_capabilities=runtime,
        provider_capabilities=provider_available,
        platform_capabilities=platform,
        permission_capabilities=permission,
        permission_denied_tool_names=denied_tools,
        security_capabilities=security,
        parent_capabilities=parent,
        execution_budget=execution_budget,
        sandbox_profile=sandbox_profile,
        override=override,
    )


def _explicitly_denied_tool_names(
    permissions: PermissionManager,
    tools: ToolCollection,
    names: tuple[str, ...],
) -> frozenset[str]:
    """Project blanket explicit denies; keep path-specific checks per call."""

    denied_names: set[str] = set()
    for name in names:
        tool = tools.get(name)
        if tool is None:
            continue
        arguments = {"command": ""} if name == "bash" else {}
        decision = permissions.decide(name, arguments, side_effecting=tool.side_effecting)
        if (
            decision.effect is PermissionEffect.DENY
            and decision.source is PermissionDecisionSource.EXPLICIT_RULE
            and any(
                rule.effect is PermissionEffect.DENY
                and rule.path_pattern is None
                and rule.operation is None
                and rule.matches(name, arguments)
                for rule in permissions.rules
            )
        ):
            denied_names.add(name)
    return frozenset(denied_names)


def _explicitly_denied_capabilities(
    names: tuple[str, ...],
    denied_names: frozenset[str],
) -> frozenset[AgentCapability]:
    return frozenset(
        capability
        for capability in tool_names_to_capabilities(names)
        if (related := [name for name in names if capability in tool_required_capabilities(name)])
        and all(name in denied_names for name in related)
    )


__all__ = ["bind_agent_profile"]
