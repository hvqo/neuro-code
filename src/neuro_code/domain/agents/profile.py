"""Declarative Agent profile values.

Profiles describe intent only. They are not runtime grants and carry no
provider, platform, session, or mutable execution state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import ExecutionBudget

_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
MAX_PROFILE_TEXT_BYTES = 4096


class AgentRole(StrEnum):
    MAIN = "main"
    EXPLORER = "explorer"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    WRITABLE_WORKER = "writable_worker"
    LEADER = "leader"


class AgentCapability(StrEnum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    SHELL_EXECUTE = "shell.execute"
    GIT_INSPECT = "git.inspect"
    WEB_SEARCH = "web.search"
    WEB_FETCH = "web.fetch"
    LSP = "lsp"
    PROJECT_MEMORY_READ = "memory.project.read"
    PROJECT_MEMORY_WRITE = "memory.project.write"
    SUBAGENT_SPAWN = "subagent.spawn"
    TASK_PLAN = "task.plan"
    VERIFICATION_RUN = "verification.run"
    USER_INTERACT = "user.interact"
    SESSION_HISTORY_READ = "session.history.read"
    WORKING_SET_MANAGE = "working_set.manage"
    CONTEXT_ROLLOVER = "context.rollover"
    BACKGROUND_MANAGE = "background.manage"
    TERMINAL_INTERACT = "terminal.interact"
    EXTENSION_INVOKE = "extension.invoke"


class AgentBehaviorPolicy(StrEnum):
    STANDARD = "standard"
    READ_ONLY_EXPLORATION = "read_only_exploration"
    PLANNING = "planning"
    REVIEW = "review"
    IMPLEMENTATION = "implementation"
    ORCHESTRATION = "orchestration"


class AgentModelSelection(StrEnum):
    INHERIT = "inherit"
    PARENT = "parent"
    ROLE_ROUTE = "role_route"


class WorkspaceWriteMode(StrEnum):
    NONE = "none"
    SCOPED = "scoped"
    MANAGED_WORKTREE = "managed_worktree"


class VerificationMode(StrEnum):
    INHERIT = "inherit"
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True, slots=True)
class AgentModelPolicy:
    """Provider-neutral model selection intent."""

    selection: AgentModelSelection = AgentModelSelection.INHERIT

    def __post_init__(self) -> None:
        if not isinstance(self.selection, AgentModelSelection):
            raise TypeError("model selection must be an AgentModelSelection")


@dataclass(frozen=True, slots=True)
class AgentReasoningPolicy:
    """Optional provider-neutral effort request and maximum ceiling."""

    effort: ReasoningEffort | None = None
    maximum_effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        if self.effort is not None and not isinstance(self.effort, ReasoningEffort):
            raise TypeError("reasoning effort must be a ReasoningEffort or None")
        if self.maximum_effort is not None and not isinstance(self.maximum_effort, ReasoningEffort):
            raise TypeError("maximum reasoning effort must be a ReasoningEffort or None")


@dataclass(frozen=True, slots=True)
class AgentMemoryPolicy:
    """Context access intent; memory writes remain application-owned."""

    read_project_memory: bool = False
    contribute_to_project_memory: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.read_project_memory, bool) or not isinstance(
            self.contribute_to_project_memory, bool
        ):
            raise TypeError("memory policy flags must be bools")


@dataclass(frozen=True, slots=True)
class AgentContextPolicy:
    """Stable context projection preferences for one binding."""

    include_project_memory_snapshot: bool = False
    include_working_set: bool = True
    preserve_stable_prefix: bool = True

    def __post_init__(self) -> None:
        if any(
            not isinstance(getattr(self, name), bool)
            for name in (
                "include_project_memory_snapshot",
                "include_working_set",
                "preserve_stable_prefix",
            )
        ):
            raise TypeError("context policy flags must be bools")
        if not self.preserve_stable_prefix:
            raise ValueError("Agent profiles cannot disable stable-prefix preservation")


@dataclass(frozen=True, slots=True)
class AgentSubagentPolicy:
    allow_spawn: bool = False
    max_children: int = 0
    max_depth: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.allow_spawn, bool):
            raise TypeError("allow_spawn must be a bool")
        for name in ("max_children", "max_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 16:
                raise ValueError(f"{name} must be between 0 and 16")
        if not self.allow_spawn and (self.max_children or self.max_depth):
            raise ValueError("disabled subagent spawning cannot have non-zero limits")
        if self.allow_spawn and (self.max_children < 1 or self.max_depth < 1):
            raise ValueError("enabled subagent spawning requires bounded positive limits")


@dataclass(frozen=True, slots=True)
class AgentWorkspacePolicy:
    read: bool = True
    write_mode: WorkspaceWriteMode = WorkspaceWriteMode.NONE

    def __post_init__(self) -> None:
        if not isinstance(self.read, bool):
            raise TypeError("workspace read policy must be a bool")
        if not isinstance(self.write_mode, WorkspaceWriteMode):
            raise TypeError("workspace write mode must be canonical")


@dataclass(frozen=True, slots=True)
class AgentVerificationPolicy:
    mode: VerificationMode = VerificationMode.INHERIT
    verify_workspace_mutations: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.mode, VerificationMode):
            raise TypeError("verification mode must be canonical")
        if not isinstance(self.verify_workspace_mutations, bool):
            raise TypeError("verify_workspace_mutations must be a bool")


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """Immutable declarative description of an Agent role."""

    profile_id: str
    name: str
    description: str
    role: AgentRole
    behavior: AgentBehaviorPolicy
    capability_policy: frozenset[AgentCapability]
    system_guidance: str = ""
    model_policy: AgentModelPolicy = field(default_factory=AgentModelPolicy)
    reasoning_policy: AgentReasoningPolicy = field(default_factory=AgentReasoningPolicy)
    memory_policy: AgentMemoryPolicy = field(default_factory=AgentMemoryPolicy)
    context_policy: AgentContextPolicy = field(default_factory=AgentContextPolicy)
    execution_budget_ceiling: ExecutionBudget | None = None
    subagent_policy: AgentSubagentPolicy = field(default_factory=AgentSubagentPolicy)
    workspace_policy: AgentWorkspacePolicy = field(default_factory=AgentWorkspacePolicy)
    verification_policy: AgentVerificationPolicy = field(default_factory=AgentVerificationPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _PROFILE_ID.fullmatch(self.profile_id):
            raise ValueError("profile_id must be a bounded canonical identifier")
        for name in ("name", "description", "system_guidance"):
            value = getattr(self, name)
            if not isinstance(value, str) or "\x00" in value:
                raise TypeError(f"{name} must be bounded text")
            if len(value.encode("utf-8")) > MAX_PROFILE_TEXT_BYTES:
                raise ValueError(f"{name} exceeds its byte limit")
        if not self.name.strip() or not self.description.strip():
            raise ValueError("profile name and description must not be empty")
        for name, value, expected in (
            ("role", self.role, AgentRole),
            ("behavior", self.behavior, AgentBehaviorPolicy),
            ("model_policy", self.model_policy, AgentModelPolicy),
            ("reasoning_policy", self.reasoning_policy, AgentReasoningPolicy),
            ("memory_policy", self.memory_policy, AgentMemoryPolicy),
            ("context_policy", self.context_policy, AgentContextPolicy),
            ("subagent_policy", self.subagent_policy, AgentSubagentPolicy),
            ("workspace_policy", self.workspace_policy, AgentWorkspacePolicy),
            ("verification_policy", self.verification_policy, AgentVerificationPolicy),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} must be a {expected.__name__}")
        capabilities = frozenset(self.capability_policy)
        if not all(isinstance(value, AgentCapability) for value in capabilities):
            raise TypeError("profile capabilities must be AgentCapability values")
        object.__setattr__(self, "capability_policy", capabilities)
        if self.execution_budget_ceiling is not None and not isinstance(
            self.execution_budget_ceiling, ExecutionBudget
        ):
            raise TypeError("execution budget ceiling must be an ExecutionBudget or None")
        if (self.workspace_policy.write_mode is WorkspaceWriteMode.NONE) == (
            AgentCapability.WORKSPACE_WRITE in capabilities
        ):
            raise ValueError("workspace write capability and write policy must agree")
        if not self.workspace_policy.read and AgentCapability.WORKSPACE_READ in capabilities:
            raise ValueError("workspace read capability requires workspace read policy")
        if self.memory_policy.read_project_memory and (
            AgentCapability.PROJECT_MEMORY_READ not in capabilities
        ):
            raise ValueError("project memory read policy requires memory.project.read")
        if self.memory_policy.contribute_to_project_memory and (
            AgentCapability.PROJECT_MEMORY_WRITE not in capabilities
        ):
            raise ValueError("project memory contribution policy requires memory.project.write")
        if self.context_policy.include_project_memory_snapshot and not (
            self.memory_policy.read_project_memory
        ):
            raise ValueError("project memory snapshot requires project memory read policy")
        if self.subagent_policy.allow_spawn and (
            AgentCapability.SUBAGENT_SPAWN not in capabilities
        ):
            raise ValueError("subagent spawning requires subagent.spawn capability")


@dataclass(frozen=True, slots=True)
class AgentProfileOverride:
    """Explicit narrowing-only override for one profile binding."""

    disabled_capabilities: frozenset[AgentCapability] = field(default_factory=frozenset)
    execution_budget_ceiling: ExecutionBudget | None = None

    def __post_init__(self) -> None:
        disabled = frozenset(self.disabled_capabilities)
        if not all(isinstance(value, AgentCapability) for value in disabled):
            raise TypeError("disabled capabilities must be AgentCapability values")
        object.__setattr__(self, "disabled_capabilities", disabled)
        if self.execution_budget_ceiling is not None and not isinstance(
            self.execution_budget_ceiling, ExecutionBudget
        ):
            raise TypeError("override budget ceiling must be an ExecutionBudget or None")


__all__ = [
    "MAX_PROFILE_TEXT_BYTES",
    "AgentBehaviorPolicy",
    "AgentCapability",
    "AgentContextPolicy",
    "AgentMemoryPolicy",
    "AgentModelPolicy",
    "AgentModelSelection",
    "AgentProfile",
    "AgentProfileOverride",
    "AgentReasoningPolicy",
    "AgentRole",
    "AgentSubagentPolicy",
    "AgentVerificationPolicy",
    "AgentWorkspacePolicy",
    "VerificationMode",
    "WorkspaceWriteMode",
]
