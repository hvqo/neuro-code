"""Bounded execution metadata projections for the TUI.

TUI 使用的有界执行 metadata 投影.

This module converts untrusted event data into an existing domain status. It
does not choose a recovery action, access persistence, or render widgets.

本模块把不可信事件数据转换为已有领域状态,不选择恢复动作、不访问持久化、也不渲染组件.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from neuro_code.domain.execution import (
    AgentExecutionStatus,
    SupervisorReasonCode,
)

__all__ = [
    "BudgetUsageProjection",
    "budget_limited_reason",
    "recoverable_terminal_status",
]


_RECOVERABLE_TERMINAL_STATUSES = frozenset(
    {AgentExecutionStatus.STUCK, AgentExecutionStatus.BUDGET_LIMITED}
)
_BUDGET_REASON_CODES = frozenset(
    {
        SupervisorReasonCode.MODEL_CALL_RESERVE,
        SupervisorReasonCode.MODEL_CALL_BUDGET,
        SupervisorReasonCode.MODEL_STEP_LIMIT,
        SupervisorReasonCode.TOOL_ROUND_BUDGET,
        SupervisorReasonCode.TOOL_CALL_BUDGET,
        SupervisorReasonCode.PER_TOOL_CALL_BUDGET,
        SupervisorReasonCode.WALL_TIME_BUDGET,
        SupervisorReasonCode.INPUT_TOKEN_BUDGET,
        SupervisorReasonCode.OUTPUT_TOKEN_BUDGET,
        SupervisorReasonCode.TOTAL_TOKEN_BUDGET,
        SupervisorReasonCode.CONTEXT_WINDOW_BUDGET,
    }
)


def _bounded_number(value: object, *, positive: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        return None
    return value


def _bounded_pair(
    data: Mapping[str, object],
    used_key: str,
    limit_key: str,
) -> tuple[int | float, int | float] | None:
    used = _bounded_number(data.get(used_key))
    limit = _bounded_number(data.get(limit_key), positive=True)
    if used is None or limit is None:
        return None
    return used, limit


@dataclass(frozen=True, slots=True)
class BudgetUsageProjection:
    """Typed, non-sensitive budget telemetry retained for one TUI turn."""

    model_calls: tuple[int | float, int | float]
    tool_rounds: tuple[int | float, int | float]
    tool_calls: tuple[int | float, int | float]
    wall_seconds: tuple[int | float, int | float] | None = None
    input_tokens: tuple[int | float, int | float] | None = None
    output_tokens: tuple[int | float, int | float] | None = None
    total_tokens: tuple[int | float, int | float] | None = None

    @classmethod
    def from_event_data(cls, data: Mapping[str, object]) -> BudgetUsageProjection | None:
        model_calls = _bounded_pair(data, "model_calls_used", "model_calls_limit")
        tool_rounds = _bounded_pair(data, "tool_rounds_used", "tool_rounds_limit")
        tool_calls = _bounded_pair(data, "tool_calls_used", "tool_calls_limit")
        if model_calls is None or tool_rounds is None or tool_calls is None:
            return None
        return cls(
            model_calls=model_calls,
            tool_rounds=tool_rounds,
            tool_calls=tool_calls,
            wall_seconds=_bounded_pair(data, "wall_seconds_used", "wall_seconds_limit"),
            input_tokens=_bounded_pair(data, "input_tokens_used", "input_tokens_limit"),
            output_tokens=_bounded_pair(data, "output_tokens_used", "output_tokens_limit"),
            total_tokens=_bounded_pair(data, "total_tokens_used", "total_tokens_limit"),
        )

    def for_reason(
        self,
        reason: SupervisorReasonCode,
    ) -> tuple[int | float, int | float] | None:
        if reason in {
            SupervisorReasonCode.MODEL_CALL_RESERVE,
            SupervisorReasonCode.MODEL_CALL_BUDGET,
            SupervisorReasonCode.MODEL_STEP_LIMIT,
        }:
            return self.model_calls
        if reason is SupervisorReasonCode.TOOL_ROUND_BUDGET:
            return self.tool_rounds
        if reason is SupervisorReasonCode.TOOL_CALL_BUDGET:
            return self.tool_calls
        return {
            SupervisorReasonCode.WALL_TIME_BUDGET: self.wall_seconds,
            SupervisorReasonCode.INPUT_TOKEN_BUDGET: self.input_tokens,
            SupervisorReasonCode.OUTPUT_TOKEN_BUDGET: self.output_tokens,
            SupervisorReasonCode.TOTAL_TOKEN_BUDGET: self.total_tokens,
            SupervisorReasonCode.CONTEXT_WINDOW_BUDGET: self.total_tokens,
        }.get(reason)


def budget_limited_reason(data: Mapping[str, object]) -> SupervisorReasonCode | None:
    """Return a recognized typed reason for a recoverable budget terminal."""

    if recoverable_terminal_status(data) is not AgentExecutionStatus.BUDGET_LIMITED:
        return None
    raw_reason = data.get("execution_reason")
    if not isinstance(raw_reason, str):
        return None
    try:
        reason = SupervisorReasonCode(raw_reason)
    except ValueError:
        return None
    return reason if reason in _BUDGET_REASON_CODES else None


def recoverable_terminal_status(
    data: Mapping[str, object],
) -> AgentExecutionStatus | None:
    """Return a supported recoverable terminal status from event metadata.

    从事件 metadata 中返回受支持的可恢复终态,未知或不安全值返回 None.
    """

    if data.get("recoverable") is not True:
        return None
    raw_status = data.get("execution_status")
    if not isinstance(raw_status, str):
        return None
    try:
        status = AgentExecutionStatus(raw_status)
    except ValueError:
        return None
    return status if status in _RECOVERABLE_TERMINAL_STATUSES else None
