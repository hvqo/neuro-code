"""Interface-neutral settings for composing one Neuro Code process.

提供组合一个 Neuro Code 进程所需的接口无关设置."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from neuro_code.application.execution_policy import (
    ExecutionBudgetPolicy,
    ExecutionBudgetSource,
    ExecutionProfile,
)
from neuro_code.application.permissions.policy import PermissionMode, PermissionRule
from neuro_code.application.runtime.supervision import ExecutionControlMode
from neuro_code.application.runtime.verification import validate_explicit_verification_command
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import ExecutionBudget
from neuro_code.shared.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """Interface-neutral settings for composing one Neuro Code process.

    提供组合一个 Neuro Code 进程所需的接口无关设置."""

    cwd: Path | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    sandbox: str | None = None
    failover: bool = True
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    permission_rules: tuple[PermissionRule, ...] = ()
    permission_rules_path: Path | None = None
    max_steps: int | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    execution_control_mode: ExecutionControlMode = ExecutionControlMode.FINALIZE_TERMINAL
    resume_id: str | None = None
    execution_profile: ExecutionProfile = ExecutionProfile.NORMAL
    execution_budget_source: ExecutionBudgetSource | None = None
    verification_command: str | None = None
    _execution_budget: ExecutionBudget = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.execution_profile, ExecutionProfile):
            raise TypeError("execution_profile must be an ExecutionProfile")
        source = self.execution_budget_source
        if source is None:
            source = (
                ExecutionBudgetSource.EXPLICIT_MAX_STEPS
                if self.max_steps is not None
                else (
                    ExecutionBudgetSource.EXPLICIT_PROFILE
                    if self.execution_profile is ExecutionProfile.DEEP
                    else ExecutionBudgetSource.IMPLICIT_PROFILE
                )
            )
        if not isinstance(source, ExecutionBudgetSource):
            raise TypeError("execution_budget_source must be an ExecutionBudgetSource or None")
        if source is ExecutionBudgetSource.IMPLICIT_PROFILE and (
            self.execution_profile is not ExecutionProfile.NORMAL or self.max_steps is not None
        ):
            raise ConfigurationError(
                "implicit execution budget source requires the default normal profile"
            )
        if source is ExecutionBudgetSource.EXPLICIT_PROFILE and self.max_steps is not None:
            raise ConfigurationError(
                "explicit execution profile source cannot be combined with max_steps"
            )
        if source is ExecutionBudgetSource.EXPLICIT_MAX_STEPS and self.max_steps is None:
            raise ConfigurationError("explicit max_steps source requires max_steps")
        object.__setattr__(self, "execution_budget_source", source)
        try:
            verification_command = validate_explicit_verification_command(self.verification_command)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"invalid verification command: {error}") from error
        object.__setattr__(self, "verification_command", verification_command)
        budget = ExecutionBudgetPolicy.resolve(
            self.execution_profile,
            max_steps=self.max_steps,
        )
        object.__setattr__(self, "max_steps", budget.max_model_calls)
        object.__setattr__(self, "_execution_budget", budget)

    @property
    def execution_budget(self) -> ExecutionBudget:
        """Return the single resolved ordinary Agent execution budget.

        返回唯一解析后的普通 Agent 执行预算。
        """

        return self._execution_budget
