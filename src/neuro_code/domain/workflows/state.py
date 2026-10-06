"""DW2 immutable durable facts. No interpreter, scheduler or execution authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from neuro_code.domain.workflows.definition import ArtifactRef, WorkflowBudget

MAX_STEP_INSTANCES = 256
MAX_BUDGET_RESERVATIONS = 256
COMPILER_VERSION = 1


def identifier(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", value):
        raise ValueError("invalid bounded workflow identity")


def fingerprint(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid SHA-256 fingerprint")


def integer(value: int, maximum: int = 2**63 - 1) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("expected bounded non-negative integer")


def timestamp(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("workflow timestamps must be timezone-aware")


def bounded_text(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1000
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("expected bounded diagnostic text")


class WorkflowStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}

    def permits(self, proposed: WorkflowStatus) -> bool:
        if self.terminal:
            return False
        if proposed is self:
            return True
        if self is self.READY:
            return proposed in {self.RUNNING, self.CANCELLED, self.NEEDS_ATTENTION}
        if self is self.NEEDS_ATTENTION:
            return proposed in {self.RUNNING, self.WAITING, self.FAILED, self.CANCELLED}
        return proposed is not self.READY


@dataclass(frozen=True, slots=True)
class WorkflowFailure:
    code: str
    message: str

    def __post_init__(self) -> None:
        identifier(self.code)
        bounded_text(self.message)


@dataclass(frozen=True, slots=True)
class StepIdentity:
    step_id: str
    iteration: int = 0
    item_key: str | None = None

    def __post_init__(self) -> None:
        identifier(self.step_id)
        integer(self.iteration, 3)
        if self.item_key is not None:
            # Opaque data, never a filesystem path or expression.
            bounded_text(self.item_key)

    @property
    def key(self) -> str:
        wire = json.dumps([self.step_id, self.iteration, self.item_key], ensure_ascii=False)
        return hashlib.sha256(wire.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowStepInstance:
    identity: StepIdentity
    status: WorkflowStatus
    input_fingerprint: str
    result_refs: tuple[ArtifactRef, ...] = ()
    failure: WorkflowFailure | None = None
    waiting_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, StepIdentity) or not isinstance(
            self.status, WorkflowStatus
        ):
            raise ValueError("step identity/status must be canonical")
        fingerprint(self.input_fingerprint)
        if type(self.result_refs) is not tuple or len(self.result_refs) > 16:
            raise ValueError("result references must be a bounded immutable tuple")
        for ref in self.result_refs:
            if not isinstance(ref, ArtifactRef):
                raise ValueError("result reference must be canonical")
            identifier(ref.artifact_id)
            fingerprint(ref.integrity_fingerprint)
            if ref.workspace_identity is not None:
                bounded_text(ref.workspace_identity)
        validate_failure(self.status, self.failure, self.waiting_reason)
        if self.result_refs and self.status is not WorkflowStatus.COMPLETED:
            raise ValueError("only completed steps can store result references")


def validate_failure(
    status: WorkflowStatus, failure: WorkflowFailure | None, waiting_reason: str | None
) -> None:
    if failure is not None and not isinstance(failure, WorkflowFailure):
        raise ValueError("failure must be canonical")
    if status is WorkflowStatus.FAILED and failure is None:
        raise ValueError("failed state requires a failure fact")
    if failure is not None and status not in {
        WorkflowStatus.FAILED,
        WorkflowStatus.NEEDS_ATTENTION,
    }:
        raise ValueError("failure fact requires failed/needs-attention state")
    if waiting_reason is not None:
        bounded_text(waiting_reason)
    if (status is WorkflowStatus.WAITING) != (waiting_reason is not None):
        raise ValueError("waiting state requires exactly one waiting reason")


@dataclass(frozen=True, slots=True)
class BudgetAmounts:
    """Exact known usage or None for explicitly unknown usage; never coerce to zero."""

    generated_tasks: int | None = 0
    model_calls: int | None = 0
    tool_calls: int | None = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    wall_milliseconds: int | None = 0

    def __post_init__(self) -> None:
        for amount in self.values:
            if amount is not None:
                integer(amount)

    @property
    def values(self) -> tuple[int | None, ...]:
        return (
            self.generated_tasks,
            self.model_calls,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.wall_milliseconds,
        )

    @property
    def known(self) -> bool:
        return all(value is not None for value in self.values)

    @classmethod
    def ceiling(cls, budget: WorkflowBudget) -> BudgetAmounts:
        return cls(
            budget.max_generated_tasks,
            budget.max_model_calls,
            budget.max_tool_calls,
            budget.max_input_tokens,
            budget.max_output_tokens,
            budget.max_wall_seconds * 1000,
        )


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    reserved: BudgetAmounts
    created_at: datetime
    consumed: BudgetAmounts | None = None
    settled_at: datetime | None = None

    def __post_init__(self) -> None:
        identifier(self.reservation_id)
        if not isinstance(self.reserved, BudgetAmounts) or not self.reserved.known:
            raise ValueError("reservation must have known upper bounds")
        timestamp(self.created_at)
        if (self.consumed is None) != (self.settled_at is None):
            raise ValueError("budget settlement requires usage and timestamp")
        if self.consumed is not None:
            if not isinstance(self.consumed, BudgetAmounts):
                raise ValueError("usage must be canonical")
            assert self.settled_at is not None
            timestamp(self.settled_at)
            if self.settled_at < self.created_at:
                raise ValueError("settlement time precedes reservation")


@dataclass(frozen=True, slots=True)
class WorkflowBudgetLedger:
    ceiling: BudgetAmounts
    reservations: tuple[BudgetReservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ceiling, BudgetAmounts) or not self.ceiling.known:
            raise ValueError("budget ceilings must be known")
        if type(self.reservations) is not tuple or len(self.reservations) > MAX_BUDGET_RESERVATIONS:
            raise ValueError("budget ledger exceeds bounded reservation limit")
        if not all(isinstance(r, BudgetReservation) for r in self.reservations):
            raise ValueError("budget reservations must be canonical")
        if len({r.reservation_id for r in self.reservations}) != len(self.reservations):
            raise ValueError("duplicate reservation identity")

    @property
    def consumed(self) -> BudgetAmounts:
        return self._total(outstanding=False)

    @property
    def committed(self) -> BudgetAmounts:
        """Consumed + outstanding reservations. Unknown usage propagates."""
        return self._total(outstanding=True)

    def _total(self, *, outstanding: bool) -> BudgetAmounts:
        totals: list[int | None] = [0] * 6
        for entry in self.reservations:
            amount = entry.consumed
            if amount is None:
                # Reservation is capacity, not evidence that consumption was zero.
                amount = entry.reserved if outstanding else BudgetAmounts(*(None for _ in range(6)))
            for i, value in enumerate(amount.values):
                current = totals[i]
                totals[i] = None if current is None or value is None else current + value
        return BudgetAmounts(*totals)


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    run_id: str
    definition_fingerprint: str
    parent_session_id: str
    input_fingerprint: str
    status: WorkflowStatus
    generation: int
    owner_id: str | None
    owner_fence: int
    created_at: datetime
    updated_at: datetime
    ledger: WorkflowBudgetLedger
    position: StepIdentity | None = None
    steps: tuple[WorkflowStepInstance, ...] = ()
    failure: WorkflowFailure | None = None
    waiting_reason: str | None = None

    def __post_init__(self) -> None:
        identifier(self.run_id)
        identifier(self.parent_session_id)
        fingerprint(self.definition_fingerprint)
        fingerprint(self.input_fingerprint)
        if not isinstance(self.status, WorkflowStatus):
            raise ValueError("status must be canonical")
        integer(self.generation)
        integer(self.owner_fence)
        if self.owner_id is not None:
            identifier(self.owner_id)
        if (self.owner_id is None) != (self.owner_fence == 0):
            raise ValueError("owner identity and fence must agree")
        timestamp(self.created_at)
        timestamp(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("update precedes run creation")
        if not isinstance(self.ledger, WorkflowBudgetLedger):
            raise ValueError("ledger must be canonical")
        if self.position is not None and not isinstance(self.position, StepIdentity):
            raise ValueError("position must be canonical")
        if type(self.steps) is not tuple or len(self.steps) > MAX_STEP_INSTANCES:
            raise ValueError("step instances exceed bounded limit")
        if not all(isinstance(s, WorkflowStepInstance) for s in self.steps):
            raise ValueError("step instances must be canonical")
        if len({s.identity.key for s in self.steps}) != len(self.steps):
            raise ValueError("duplicate step identity")
        validate_failure(self.status, self.failure, self.waiting_reason)


class WorkflowEventKind(StrEnum):
    CREATED = "run_created"
    CLAIMED = "claimed"
    RESUMED = "resumed"
    TRANSITION = "run_transition"
    STEP = "step_transition"
    BRANCH = "branch_decision"
    ITERATION = "iteration"
    RESERVED = "budget_reserved"
    CONSUMED = "budget_consumed"
    RECONCILED = "budget_reconciled"
    PUBLISHED = "dag_published"


@dataclass(frozen=True, slots=True)
class WorkflowJournalEvent:
    run_id: str
    generation: int
    request_id: str
    kind: WorkflowEventKind
    payload_fingerprint: str
    payload_json: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowWriteResult:
    run: WorkflowRun
    committed_generation: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowChange:
    """One bounded fact committed with its snapshot; contains no executable payload."""

    kind: WorkflowEventKind
    status: WorkflowStatus | None = None
    position: StepIdentity | None = None
    step: WorkflowStepInstance | None = None
    selected_path: str | None = None
    reservation_id: str | None = None
    amounts: BudgetAmounts | None = None
    failure: WorkflowFailure | None = None
    waiting_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WorkflowEventKind):
            raise ValueError("event kind must be canonical")
        fields = {
            "status": self.status,
            "position": self.position,
            "step": self.step,
            "selected_path": self.selected_path,
            "reservation_id": self.reservation_id,
            "amounts": self.amounts,
            "failure": self.failure,
            "waiting_reason": self.waiting_reason,
        }
        allowed = {
            WorkflowEventKind.TRANSITION: {"status", "position", "failure", "waiting_reason"},
            WorkflowEventKind.STEP: {"step"},
            WorkflowEventKind.BRANCH: {"position", "selected_path"},
            WorkflowEventKind.ITERATION: {"position"},
            WorkflowEventKind.RESERVED: {"reservation_id", "amounts"},
            WorkflowEventKind.CONSUMED: {"reservation_id", "amounts"},
            WorkflowEventKind.RECONCILED: {"reservation_id", "amounts"},
        }.get(self.kind)
        if allowed is None or any(v is not None and k not in allowed for k, v in fields.items()):
            raise ValueError("event contains unsupported fields")
        if self.kind is WorkflowEventKind.TRANSITION:
            if not isinstance(self.status, WorkflowStatus):
                raise ValueError("transition requires status")
            validate_failure(self.status, self.failure, self.waiting_reason)
        if self.position is not None and not isinstance(self.position, StepIdentity):
            raise ValueError("position must be canonical")
        if self.kind is WorkflowEventKind.STEP and not isinstance(self.step, WorkflowStepInstance):
            raise ValueError("step transition requires canonical step")
        if self.kind in {WorkflowEventKind.BRANCH, WorkflowEventKind.ITERATION}:
            if self.position is None:
                raise ValueError("control fact requires position")
            if self.kind is WorkflowEventKind.BRANCH:
                if self.selected_path is None:
                    raise ValueError("branch fact requires selected path")
                identifier(self.selected_path)
        if self.kind in {
            WorkflowEventKind.RESERVED,
            WorkflowEventKind.CONSUMED,
            WorkflowEventKind.RECONCILED,
        }:
            if self.reservation_id is None or not isinstance(self.amounts, BudgetAmounts):
                raise ValueError("budget fact requires identity and amounts")
            identifier(self.reservation_id)
            if (
                self.kind in {WorkflowEventKind.RESERVED, WorkflowEventKind.RECONCILED}
                and not self.amounts.known
            ):
                raise ValueError("reservation/reconciliation must be known")
