"""Pure durable Activity decoding shared by independent fact validators."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from neuro_code.domain.workflows.activity import (
    WorkflowActivityAttempt,
    WorkflowActivityInvocation,
    WorkflowActivityResult,
    WorkflowActivityState,
)
from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.state import BudgetAmounts, StepIdentity


def _invocation(data: dict[str, Any]) -> WorkflowActivityInvocation:
    step = data["step"]
    if not isinstance(step, dict):
        raise ValueError("activity step is invalid")
    return WorkflowActivityInvocation(
        data["invocation_id"],
        data["run_id"],
        data["definition_fingerprint"],
        data["parent_session_id"],
        StepIdentity(**step),
        ActivityKind(data["activity"]),
        data["request_json"],
        data["request_fingerprint"],
        data["input_fingerprint"],
        datetime.fromisoformat(data["created_at"]),
    )


def _result(data: dict[str, Any]) -> WorkflowActivityResult:
    usage = data["usage"]
    if not isinstance(usage, dict):
        raise ValueError("activity usage is invalid")
    return WorkflowActivityResult(
        data["invocation_id"],
        data["request_fingerprint"],
        ActivityKind(data["activity"]),
        WorkflowActivityState(data["state"]),
        data["source_id"],
        data["source_fingerprint"],
        BudgetAmounts(**usage),
        datetime.fromisoformat(data["terminal_at"]),
        data["output_json"],
    )


def decode_attempt(data: dict[str, Any]) -> WorkflowActivityAttempt:
    reserved = data["reserved"]
    return WorkflowActivityAttempt(
        _invocation(data["invocation"]),
        WorkflowActivityState(data["state"]),
        data["revision"],
        data["owner_id"],
        data["owner_fence"],
        data["reservation_id"],
        BudgetAmounts(**reserved) if reserved is not None else None,
        _result(data["result"]) if data["result"] is not None else None,
        datetime.fromisoformat(data["updated_at"]),
    )
