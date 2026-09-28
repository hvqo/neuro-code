"""Canonical application boundary for resolved Agent profile bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from neuro_code.domain.agents.profile import AgentCapability, AgentProfile
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import ExecutionBudget


class AgentCapabilityUnavailableReason(StrEnum):
    PROFILE_OVERRIDE = "profile_override"
    PARENT_POLICY = "parent_policy"
    PERMISSION_POLICY = "permission_policy"
    SANDBOX_POLICY = "sandbox_policy"
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"


@dataclass(frozen=True, slots=True)
class UnavailableAgentCapability:
    capability: AgentCapability
    reason: AgentCapabilityUnavailableReason

    def __post_init__(self) -> None:
        if not isinstance(self.capability, AgentCapability):
            raise TypeError("unavailable capability must be canonical")
        if not isinstance(self.reason, AgentCapabilityUnavailableReason):
            raise TypeError("unavailable reason must be canonical")


@dataclass(frozen=True, slots=True)
class AgentCapabilityResolution:
    requested: frozenset[AgentCapability]
    runtime_available: frozenset[AgentCapability]
    provider_available: frozenset[AgentCapability]
    platform_available: frozenset[AgentCapability]
    profile_override_allowed: frozenset[AgentCapability]
    permission_allowed: frozenset[AgentCapability]
    security_allowed: frozenset[AgentCapability]
    parent_available: frozenset[AgentCapability]
    effective: frozenset[AgentCapability]
    unavailable: tuple[UnavailableAgentCapability, ...]

    def __post_init__(self) -> None:
        for name in (
            "requested",
            "runtime_available",
            "provider_available",
            "platform_available",
            "profile_override_allowed",
            "permission_allowed",
            "security_allowed",
            "parent_available",
            "effective",
        ):
            values = frozenset(getattr(self, name))
            if not all(isinstance(value, AgentCapability) for value in values):
                raise TypeError(f"{name} must contain AgentCapability values")
            object.__setattr__(self, name, values)
        unavailable = tuple(self.unavailable)
        if not all(isinstance(item, UnavailableAgentCapability) for item in unavailable):
            raise TypeError("unavailable must contain typed capability reasons")
        if self.effective != (
            self.requested
            & self.runtime_available
            & self.provider_available
            & self.platform_available
            & self.profile_override_allowed
            & self.permission_allowed
            & self.security_allowed
            & self.parent_available
        ):
            raise ValueError("effective capabilities must be the canonical policy intersection")
        if {item.capability for item in unavailable} != self.requested - self.effective:
            raise ValueError("every unavailable requested capability requires one reason")
        if len(unavailable) != len({item.capability for item in unavailable}):
            raise ValueError("unavailable capabilities must be unique")
        object.__setattr__(self, "unavailable", unavailable)

    def to_trace_mapping(self) -> dict[str, object]:
        """Return bounded, credential-free capability diagnostics."""

        return {
            "effective_capabilities": sorted(value.value for value in self.effective),
            "unavailable_capabilities": [
                {"capability": item.capability.value, "reason": item.reason.value}
                for item in self.unavailable
            ],
        }


@dataclass(frozen=True, slots=True)
class EffectiveAgentBinding:
    """Immutable provider/tool/security projection consumed by one runtime."""

    profile: AgentProfile
    provider_name: str
    model_name: str
    reasoning_effort: ReasoningEffort
    capability_resolution: AgentCapabilityResolution
    bound_tool_names: tuple[str, ...]
    execution_budget: ExecutionBudget
    bound_provider_tool_names: tuple[str, ...] = ()
    security_constraints: tuple[str, ...] = ()
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.profile, AgentProfile):
            raise TypeError("effective binding profile must be canonical")
        for name in ("provider_name", "model_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 256:
                raise ValueError(f"{name} must be bounded non-empty text")
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            raise TypeError("reasoning_effort must be canonical")
        if not isinstance(self.capability_resolution, AgentCapabilityResolution):
            raise TypeError("capability resolution must be canonical")
        if not isinstance(self.execution_budget, ExecutionBudget):
            raise TypeError("execution budget must be canonical")
        names = tuple(self.bound_tool_names)
        if len(names) != len(set(names)) or any(
            not isinstance(name, str) or not name or len(name.encode("utf-8")) > 256
            for name in names
        ):
            raise ValueError("bound tool names must be unique bounded identifiers")
        object.__setattr__(self, "bound_tool_names", names)
        provider_names = tuple(self.bound_provider_tool_names)
        if len(provider_names) != len(set(provider_names)) or any(
            not isinstance(name, str) or not name or len(name.encode("utf-8")) > 256
            for name in provider_names
        ):
            raise ValueError("bound provider tool names must be unique bounded identifiers")
        object.__setattr__(self, "bound_provider_tool_names", provider_names)
        constraints = tuple(self.security_constraints)
        if len(constraints) > 32 or any(
            not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128
            for value in constraints
        ):
            raise ValueError("security constraints must be bounded labels")
        object.__setattr__(self, "security_constraints", constraints)
        payload = {
            "profile": {
                "profile_id": self.profile.profile_id,
                "name": self.profile.name,
                "description": self.profile.description,
                "role": self.profile.role.value,
                "behavior": self.profile.behavior.value,
                "system_guidance": self.profile.system_guidance,
                "model_selection": self.profile.model_policy.selection.value,
                "reasoning_effort": (
                    self.profile.reasoning_policy.effort.value
                    if self.profile.reasoning_policy.effort is not None
                    else None
                ),
                "maximum_reasoning_effort": (
                    self.profile.reasoning_policy.maximum_effort.value
                    if self.profile.reasoning_policy.maximum_effort is not None
                    else None
                ),
                "requested_capabilities": sorted(
                    value.value for value in self.capability_resolution.requested
                ),
                "memory_policy": {
                    "read_project_memory": self.profile.memory_policy.read_project_memory,
                    "contribute_to_project_memory": (
                        self.profile.memory_policy.contribute_to_project_memory
                    ),
                },
                "context_policy": {
                    "include_project_memory_snapshot": (
                        self.profile.context_policy.include_project_memory_snapshot
                    ),
                    "include_working_set": self.profile.context_policy.include_working_set,
                    "preserve_stable_prefix": self.profile.context_policy.preserve_stable_prefix,
                },
                "execution_budget_ceiling": (
                    _budget_mapping(self.profile.execution_budget_ceiling)
                    if self.profile.execution_budget_ceiling is not None
                    else None
                ),
                "subagent_policy": {
                    "allow_spawn": self.profile.subagent_policy.allow_spawn,
                    "max_children": self.profile.subagent_policy.max_children,
                    "max_depth": self.profile.subagent_policy.max_depth,
                },
                "workspace_policy": {
                    "read": self.profile.workspace_policy.read,
                    "write_mode": self.profile.workspace_policy.write_mode.value,
                },
                "verification_policy": {
                    "mode": self.profile.verification_policy.mode.value,
                    "verify_workspace_mutations": (
                        self.profile.verification_policy.verify_workspace_mutations
                    ),
                },
            },
            "provider": self.provider_name,
            "model": self.model_name,
            "reasoning_effort": self.reasoning_effort.value,
            "capability_resolution": {
                "requested": sorted(value.value for value in self.capability_resolution.requested),
                "runtime_available": sorted(
                    value.value for value in self.capability_resolution.runtime_available
                ),
                "provider_available": sorted(
                    value.value for value in self.capability_resolution.provider_available
                ),
                "platform_available": sorted(
                    value.value for value in self.capability_resolution.platform_available
                ),
                "profile_override_allowed": sorted(
                    value.value for value in self.capability_resolution.profile_override_allowed
                ),
                "permission_allowed": sorted(
                    value.value for value in self.capability_resolution.permission_allowed
                ),
                "security_allowed": sorted(
                    value.value for value in self.capability_resolution.security_allowed
                ),
                "parent_available": sorted(
                    value.value for value in self.capability_resolution.parent_available
                ),
                "effective": sorted(value.value for value in self.capability_resolution.effective),
            },
            "unavailable": [
                [item.capability.value, item.reason.value]
                for item in self.capability_resolution.unavailable
            ],
            "tools": list(names),
            "provider_tools": list(provider_names),
            "execution_budget": _budget_mapping(self.execution_budget),
            "security_constraints": list(constraints),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "fingerprint", hashlib.sha256(encoded.encode("utf-8")).hexdigest())

    @property
    def effective_capabilities(self) -> frozenset[AgentCapability]:
        return self.capability_resolution.effective

    def to_trace_mapping(self) -> Mapping[str, object]:
        """Return stable metadata only; never expose profile guidance or secrets."""

        return MappingProxyType(
            {
                "profile_id": self.profile.profile_id,
                "role": self.profile.role.value,
                "provider": self.provider_name,
                "model": self.model_name,
                "binding_reasoning_effort": self.reasoning_effort.value,
                "model_policy": self.profile.model_policy.selection.value,
                **self.capability_resolution.to_trace_mapping(),
                "execution_budget": _budget_mapping(self.execution_budget),
                "security_constraints": list(self.security_constraints),
                "binding_fingerprint": self.fingerprint,
            }
        )


def _budget_mapping(budget: ExecutionBudget) -> dict[str, object]:
    return {
        "max_model_calls": budget.max_model_calls,
        "max_tool_rounds": budget.max_tool_rounds,
        "max_tool_calls": budget.max_tool_calls,
        "max_calls_per_tool": budget.max_calls_per_tool,
        "max_wall_seconds": budget.max_wall_seconds,
        "max_input_tokens": budget.max_input_tokens,
        "max_output_tokens": budget.max_output_tokens,
        "max_total_tokens": budget.max_total_tokens,
    }


__all__ = [
    "AgentCapabilityResolution",
    "AgentCapabilityUnavailableReason",
    "EffectiveAgentBinding",
    "UnavailableAgentCapability",
]
