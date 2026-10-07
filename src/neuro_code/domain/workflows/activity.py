"""Durable Workflow Activity facts; no execution or permission authority."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.interpreter import MAX_VALUE_BYTES, invocation_id
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    StepIdentity,
    fingerprint,
    identifier,
    integer,
    timestamp,
)


class WorkflowActivityState(StrEnum):
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INDETERMINATE = "indeterminate"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.BLOCKED, self.INDETERMINATE}


@dataclass(frozen=True, slots=True)
class WorkflowActivityInvocation:
    invocation_id: str
    run_id: str
    definition_fingerprint: str
    parent_session_id: str
    step: StepIdentity
    activity: ActivityKind
    request_json: str
    request_fingerprint: str
    input_fingerprint: str
    created_at: datetime

    def __post_init__(self) -> None:
        identifier(self.invocation_id)
        identifier(self.run_id)
        identifier(self.parent_session_id)
        fingerprint(self.definition_fingerprint)
        fingerprint(self.request_fingerprint)
        fingerprint(self.input_fingerprint)
        timestamp(self.created_at)
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if not isinstance(self.step, StepIdentity) or not isinstance(self.activity, ActivityKind):
            raise TypeError("activity invocation must be typed")
        if not isinstance(self.request_json, str):
            raise TypeError("activity request must be canonical JSON text")
        if self.invocation_id != invocation_id(self.run_id, self.step, self.input_fingerprint):
            raise ValueError("activity invocation identity mismatch")
        if (
            len(self.request_json.encode("utf-8")) > MAX_VALUE_BYTES
            or canonical(json.loads(self.request_json)) != self.request_json
            or digest(json.loads(self.request_json)) != self.request_fingerprint
            or self.request_fingerprint != self.input_fingerprint
        ):
            raise ValueError("activity request must be bounded canonical input")

    @property
    def fingerprint(self) -> str:
        return digest(
            [
                self.invocation_id,
                self.run_id,
                self.definition_fingerprint,
                self.parent_session_id,
                self.step.key,
                self.activity.value,
                self.request_json,
                self.request_fingerprint,
                self.input_fingerprint,
            ]
        )


@dataclass(frozen=True, slots=True)
class WorkflowActivityResult:
    invocation_id: str
    request_fingerprint: str
    activity: ActivityKind
    state: WorkflowActivityState
    source_id: str
    source_fingerprint: str
    usage: BudgetAmounts
    terminal_at: datetime
    output_json: str | None = None

    def __post_init__(self) -> None:
        identifier(self.invocation_id)
        identifier(self.source_id)
        fingerprint(self.request_fingerprint)
        fingerprint(self.source_fingerprint)
        timestamp(self.terminal_at)
        object.__setattr__(self, "terminal_at", self.terminal_at.astimezone(UTC))
        if (
            not isinstance(self.activity, ActivityKind)
            or not isinstance(self.state, WorkflowActivityState)
            or not self.state.terminal
        ):
            raise ValueError("activity result must have a terminal typed state")
        if not isinstance(self.usage, BudgetAmounts):
            raise TypeError("activity usage must be canonical")
        if self.output_json is not None and not isinstance(self.output_json, str):
            raise TypeError("activity output must be canonical JSON text")
        if (self.output_json is None) == (self.state is WorkflowActivityState.COMPLETED):
            raise ValueError("only completed activity has output")
        if self.output_json is not None and (
            len(self.output_json.encode("utf-8")) > MAX_VALUE_BYTES
            or canonical(json.loads(self.output_json)) != self.output_json
        ):
            raise ValueError("activity output must be bounded canonical JSON")

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "invocation_id": self.invocation_id,
                "request_fingerprint": self.request_fingerprint,
                "activity": self.activity.value,
                "state": self.state.value,
                "source_id": self.source_id,
                "source_fingerprint": self.source_fingerprint,
                "usage": asdict(self.usage),
                "terminal_at": self.terminal_at.isoformat(),
                "output_json": self.output_json,
            }
        )


@dataclass(frozen=True, slots=True)
class WorkflowActivityAttempt:
    invocation: WorkflowActivityInvocation
    state: WorkflowActivityState = WorkflowActivityState.READY
    revision: int = 0
    owner_id: str | None = None
    owner_fence: int = 0
    reservation_id: str | None = None
    reserved: BudgetAmounts | None = None
    result: WorkflowActivityResult | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.invocation, WorkflowActivityInvocation) or not isinstance(
            self.state, WorkflowActivityState
        ):
            raise TypeError("activity attempt must be canonical")
        integer(self.revision)
        integer(self.owner_fence)
        revisions = {
            WorkflowActivityState.READY: {0},
            WorkflowActivityState.CLAIMED: {1},
            WorkflowActivityState.RUNNING: {2},
            WorkflowActivityState.COMPLETED: {3},
        }
        if self.revision not in revisions.get(self.state, {2, 3}):
            raise ValueError("activity revision differs from lifecycle")
        now = self.updated_at or self.invocation.created_at
        timestamp(now)
        if now < self.invocation.created_at:
            raise ValueError("activity time precedes invocation")
        object.__setattr__(self, "updated_at", now.astimezone(UTC))
        if self.owner_id is not None:
            identifier(self.owner_id)
        if self.reservation_id is not None:
            identifier(self.reservation_id)
        if self.state is WorkflowActivityState.READY:
            if (
                self.revision
                or self.owner_fence
                or self.owner_id is not None
                or self.reservation_id is not None
                or self.reserved is not None
            ):
                raise ValueError("ready activity cannot have execution ownership")
        elif (
            self.owner_id is None
            or self.owner_fence != 1
            or self.reservation_id is None
            or self.reserved is None
            or self.revision <= 0
        ):
            raise ValueError("claimed activity requires fenced owner and reservation")
        if self.reserved is not None and (
            not isinstance(self.reserved, BudgetAmounts) or not self.reserved.known
        ):
            raise ValueError("activity reservation must have known bounds")
        if (self.result is None) == self.state.terminal:
            raise ValueError("terminal activity requires exactly one result")
        if self.result is not None and not isinstance(self.result, WorkflowActivityResult):
            raise TypeError("activity result must be canonical")
        if self.result is not None and (
            self.result.invocation_id != self.invocation.invocation_id
            or self.result.request_fingerprint != self.invocation.request_fingerprint
            or self.result.activity is not self.invocation.activity
            or self.result.state is not self.state
            or self.result.terminal_at != now
        ):
            raise ValueError("activity result differs from invocation")
