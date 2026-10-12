"""Explicit, bounded drive for the trusted VERIFY/REPAIR Workflow subset.

The coordinator is not an interpreter or scheduler. It reloads durable facts
before each call to the existing one-action interpreter and delegates every
side effect to the existing VERIFY and REPAIR adapters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from neuro_code.application.ports.workflow_activity import WorkflowActivityStore
from neuro_code.application.ports.workflow_interpreter import WorkflowInterpreterStore
from neuro_code.application.ports.workflow_state import WorkflowStateError, WorkflowStateStore
from neuro_code.application.workflows.workflow_interpreter import (
    CONTROL_FLOW_EXHAUSTED_REASON,
    DurableWorkflowInterpreter,
)
from neuro_code.application.workflows.workflow_repair import WorkflowRepairActivityAdapter
from neuro_code.application.workflows.workflow_verify import WorkflowVerifyActivityAdapter
from neuro_code.domain.workflows.activity import WorkflowActivityAttempt, WorkflowActivityState
from neuro_code.domain.workflows.definition import (
    Activity,
    ActivityKind,
    Branch,
    ConditionOp,
    Repeat,
    ResultRef,
    Step,
    WorkflowDefinition,
)
from neuro_code.domain.workflows.interpreter import invocation_id
from neuro_code.domain.workflows.state import (
    StepIdentity,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepInstance,
    identifier,
    integer,
)

MAX_LOOP_VERIFICATIONS = 3
MAX_LOOP_REPAIRS = MAX_LOOP_VERIFICATIONS - 1
MAX_DURABLE_ACTIONS_PER_CALL = 96


class WorkflowLoopStatus(StrEnum):
    VERIFIED = "VERIFIED"
    EXHAUSTED = "EXHAUSTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"
    WAITING = "WAITING"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class BoundedVerificationLoopPlan:
    """Step identities in a statically unrolled, fail-closed control chain."""

    verification_step_ids: tuple[str, ...]
    repair_step_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowLoopResult:
    run: WorkflowRun
    status: WorkflowLoopStatus
    terminal_verifications: int
    terminal_repairs: int
    stop_reason: str


class UnsupportedWorkflowLoop(ValueError):
    """The IR is valid DW1 but outside this coordinator's deliberately small subset."""


def _contains_loop_activity(step: Step) -> bool:
    if isinstance(step, Activity):
        return step.activity in {ActivityKind.VERIFY, ActivityKind.REPAIR}
    if isinstance(step, Branch):
        return any(_contains_loop_activity(child) for _, path in step.paths for child in path)
    if isinstance(step, Repeat):
        return any(_contains_loop_activity(child) for child in step.steps)
    return False


def _is_status_ref(value: object, step_id: str) -> bool:
    return (
        isinstance(value, ResultRef)
        and value.step_id == step_id
        and value.field_path == ("status",)
        and value.iteration is None
        and value.item_key is None
    )


def bounded_verification_loop_plan(
    definition: WorkflowDefinition,
) -> BoundedVerificationLoopPlan:
    """Recognize an explicit VERIFY → (PASS | REPAIR → VERIFY)* chain.

    The prefix may contain earlier ADOPT/DAG work, but no VERIFY/REPAIR. The
    loop itself uses existing Activity and Branch nodes, with an empty final
    FAIL path so the last failed verification cannot be followed by an
    unverified repair.
    """

    if len(definition.steps) < 2:
        raise UnsupportedWorkflowLoop("bounded loop needs a final VERIFY and Branch")
    prefix, first, first_branch = definition.steps[:-2], definition.steps[-2], definition.steps[-1]
    if any(_contains_loop_activity(step) for step in prefix):
        raise UnsupportedWorkflowLoop("VERIFY/REPAIR activities must be in the bounded suffix")
    if not isinstance(first, Activity) or first.activity is not ActivityKind.VERIFY:
        raise UnsupportedWorkflowLoop("bounded suffix must start with VERIFY")
    if not isinstance(first_branch, Branch):
        raise UnsupportedWorkflowLoop("each VERIFY must be followed by its result Branch")

    verification_ids: list[str] = []
    repair_ids: list[str] = []
    current_verify = first
    current_branch: Branch = first_branch

    while True:
        verification_ids.append(current_verify.step_id)
        if len(verification_ids) > MAX_LOOP_VERIFICATIONS:
            raise UnsupportedWorkflowLoop("loop exceeds three real VERIFY attempts")
        condition = current_branch.condition
        if not (
            condition.op is ConditionOp.EQ
            and _is_status_ref(condition.ref, current_verify.step_id)
            and condition.value == "PASS"
        ):
            raise UnsupportedWorkflowLoop("Branch must test this VERIFY status for PASS")
        paths = dict(current_branch.paths)
        if (
            set(paths) != {"pass", "fail"}
            or current_branch.then_path != "pass"
            or current_branch.else_path != "fail"
            or paths["pass"]
        ):
            raise UnsupportedWorkflowLoop("PASS must exit this bounded repair chain")
        fail_path = paths["fail"]
        if not fail_path:
            break
        if len(verification_ids) >= MAX_LOOP_VERIFICATIONS or len(fail_path) != 3:
            raise UnsupportedWorkflowLoop(
                "FAIL may contain only one REPAIR, a fresh VERIFY, and its Branch"
            )
        repair, next_verify, next_branch = fail_path
        if not isinstance(repair, Activity) or repair.activity is not ActivityKind.REPAIR:
            raise UnsupportedWorkflowLoop("FAIL path must begin with one REPAIR")
        if not isinstance(next_verify, Activity) or next_verify.activity is not ActivityKind.VERIFY:
            raise UnsupportedWorkflowLoop("every REPAIR must be followed by a new VERIFY")
        if not isinstance(next_branch, Branch):
            raise UnsupportedWorkflowLoop("new VERIFY must receive its own result Branch")
        if not any(_is_status_ref(value, current_verify.step_id) for _, value in repair.inputs):
            raise UnsupportedWorkflowLoop("REPAIR must consume this VERIFY's exact status")
        if not any(_is_status_ref(value, repair.step_id) for _, value in next_verify.inputs):
            raise UnsupportedWorkflowLoop("new VERIFY must bind the preceding REPAIR result")
        repair_ids.append(repair.step_id)
        if len(repair_ids) > MAX_LOOP_REPAIRS:
            raise UnsupportedWorkflowLoop("loop exceeds two REPAIR attempts")
        current_verify = next_verify
        current_branch = next_branch

    all_loop_ids: set[str] = set(verification_ids) | set(repair_ids)
    declared_loop_ids: set[str] = set()

    def collect(steps: tuple[Step, ...]) -> None:
        for step in steps:
            if isinstance(step, Activity) and step.activity in {
                ActivityKind.VERIFY,
                ActivityKind.REPAIR,
            }:
                declared_loop_ids.add(step.step_id)
            elif isinstance(step, Branch):
                for _, path in step.paths:
                    collect(path)
            elif isinstance(step, Repeat):
                collect(step.steps)

    collect(definition.steps)
    if all_loop_ids != declared_loop_ids:
        raise UnsupportedWorkflowLoop("all VERIFY/REPAIR declarations must belong to this chain")
    if len(repair_ids) != len(verification_ids) - 1:
        raise UnsupportedWorkflowLoop("each retry VERIFY requires exactly one preceding REPAIR")
    return BoundedVerificationLoopPlan(tuple(verification_ids), tuple(repair_ids))


class WorkflowLoopCoordinator:
    """One explicit bounded drive call; no background loop or new execution engine."""

    def __init__(
        self,
        *,
        state: WorkflowStateStore,
        facts: WorkflowInterpreterStore,
        activities: WorkflowActivityStore,
        interpreter: DurableWorkflowInterpreter,
        verification: WorkflowVerifyActivityAdapter,
        repair: WorkflowRepairActivityAdapter,
    ) -> None:
        self.state = state
        self.facts = facts
        self.activities = activities
        self.interpreter = interpreter
        self.verification = verification
        self.repair = repair

    async def drive_once(
        self,
        run_id: str,
        *,
        owner_id: str,
        owner_fence: int,
    ) -> WorkflowLoopResult:
        """Advance at most 96 durable actions and return as soon as work must wait."""

        identifier(run_id)
        identifier(owner_id)
        integer(owner_fence)
        for _ in range(MAX_DURABLE_ACTIONS_PER_CALL):
            run = await self.state.get_workflow_run(run_id)
            if run is None:
                raise WorkflowStateError("run is missing", kind="missing")
            if (run.owner_id, run.owner_fence) != (owner_id, owner_fence):
                return await self._result(
                    run, WorkflowLoopStatus.BLOCKED, "workflow owner/fence changed"
                )
            if run.status is WorkflowStatus.CANCELLED:
                return await self._result(run, WorkflowLoopStatus.CANCELLED, "workflow cancelled")
            if run.status is WorkflowStatus.FAILED:
                return await self._result(run, WorkflowLoopStatus.FAILED, "workflow failed")
            if run.status is WorkflowStatus.NEEDS_ATTENTION or not run.ledger.committed.known:
                return await self._result(
                    run,
                    WorkflowLoopStatus.INDETERMINATE,
                    "workflow has unresolved execution or budget facts",
                )
            if run.status is WorkflowStatus.COMPLETED:
                return await self._result(
                    run, WorkflowLoopStatus.WAITING, "loop does not establish Workflow completion"
                )
            definition = await self.state.get_workflow_definition(run.definition_fingerprint)
            if definition is None:
                raise WorkflowStateError("definition missing", kind="integrity")
            plan = bounded_verification_loop_plan(definition)

            stop = await self._drive_waiting_activity(run, definition, plan)
            if stop is not None:
                return stop
            if run.waiting_reason == CONTROL_FLOW_EXHAUSTED_REASON:
                return await self._completion_projection(run, plan)
            try:
                advanced = await self.interpreter.advance_once(
                    run_id,
                    expected_generation=run.generation,
                    owner_id=owner_id,
                    owner_fence=owner_fence,
                    updated_at=self._now(),
                )
            except WorkflowStateError as error:
                if error.kind == "stale_verification":
                    current = await self.state.get_workflow_run(run_id)
                    assert current is not None
                    return await self._result(
                        current,
                        WorkflowLoopStatus.INDETERMINATE,
                        "VERIFY workspace changed before its result could be consumed",
                    )
                if error.kind == "needs_attention":
                    current = await self.state.get_workflow_run(run_id)
                    assert current is not None
                    return await self._result(current, WorkflowLoopStatus.INDETERMINATE, error.kind)
                if error.kind in {"concurrent_modification", "conflict"}:
                    current = await self.state.get_workflow_run(run_id)
                    assert current is not None
                    if (current.owner_id, current.owner_fence) != (owner_id, owner_fence):
                        return await self._result(
                            current, WorkflowLoopStatus.BLOCKED, "workflow owner/fence changed"
                        )
                    # The Interpreter's stable tick request may be committed by
                    # a peer coordinator with a different timestamp. Continue
                    # only when durable generation proves that peer advanced;
                    # an unexplained same-generation conflict remains fatal.
                    if current.generation > run.generation:
                        continue
                    if error.kind == "concurrent_modification":
                        return await self._result(
                            current,
                            WorkflowLoopStatus.WAITING,
                            "Workflow state changed concurrently; resume with a fresh drive",
                        )
                raise
            if advanced.run.status is WorkflowStatus.FAILED:
                return await self._result(advanced.run, WorkflowLoopStatus.FAILED, advanced.action)
            if advanced.run.status is WorkflowStatus.CANCELLED:
                return await self._result(
                    advanced.run, WorkflowLoopStatus.CANCELLED, "workflow cancelled"
                )
            if advanced.run.status is WorkflowStatus.NEEDS_ATTENTION:
                return await self._result(
                    advanced.run, WorkflowLoopStatus.INDETERMINATE, advanced.action
                )
            if advanced.action == "completion_pending":
                return await self._completion_projection(advanced.run, plan)
            if not advanced.progressed:
                return await self._result(
                    advanced.run,
                    WorkflowLoopStatus.WAITING,
                    advanced.action or "no durable progress",
                )
        current = await self.state.get_workflow_run(run_id)
        if current is None:
            raise WorkflowStateError("run is missing", kind="missing")
        return await self._result(
            current, WorkflowLoopStatus.WAITING, "bounded durable action limit reached"
        )

    async def _drive_waiting_activity(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        plan: BoundedVerificationLoopPlan,
    ) -> WorkflowLoopResult | None:
        declarations = _step_declarations(definition.steps)
        pending: list[tuple[WorkflowStepInstance, Activity, WorkflowActivityAttempt]] = []
        for instance in run.steps:
            if instance.status is not WorkflowStatus.WAITING:
                continue
            declaration = declarations.get(instance.identity.step_id)
            if not isinstance(declaration, Activity):
                continue
            attempt_id = invocation_id(run.run_id, instance.identity, instance.input_fingerprint)
            attempt = await self.activities.get_workflow_activity(attempt_id)
            if attempt is None:
                raise WorkflowStateError(
                    "waiting Activity has no durable invocation", kind="integrity"
                )
            pending.append((instance, declaration, attempt))
        if len(pending) > 1:
            raise WorkflowStateError("multiple serialized Activities are waiting", kind="integrity")
        if not pending:
            return None
        instance, declaration, attempt = pending[0]
        if attempt.state.terminal:
            if attempt.state is WorkflowActivityState.COMPLETED:
                return None
            status = {
                WorkflowActivityState.BLOCKED: WorkflowLoopStatus.BLOCKED,
                WorkflowActivityState.FAILED: WorkflowLoopStatus.FAILED,
                WorkflowActivityState.INDETERMINATE: WorkflowLoopStatus.INDETERMINATE,
            }[attempt.state]
            return await self._result(
                run, status, f"{declaration.activity.value} {attempt.state.value}"
            )
        if declaration.activity not in {ActivityKind.VERIFY, ActivityKind.REPAIR}:
            return await self._result(
                run,
                WorkflowLoopStatus.WAITING,
                f"external {declaration.activity.value} Activity must finish before loop drive",
            )
        if declaration.step_id not in (
            plan.verification_step_ids
            if declaration.activity is ActivityKind.VERIFY
            else plan.repair_step_ids
        ):
            raise WorkflowStateError("Activity is outside bounded loop plan", kind="integrity")
        adapter = self.verification if declaration.activity is ActivityKind.VERIFY else self.repair
        try:
            outcome = await adapter.run_once(attempt.invocation.invocation_id)
        except WorkflowStateError as error:
            if error.kind not in {
                "budget",
                "cancelled",
                "needs_attention",
                "stale_verification",
            }:
                raise
            refreshed = await self.state.get_workflow_run(run.run_id)
            if refreshed is None:
                raise WorkflowStateError("run is missing", kind="missing") from error
            status = {
                "budget": WorkflowLoopStatus.BLOCKED,
                "cancelled": WorkflowLoopStatus.CANCELLED,
                "needs_attention": WorkflowLoopStatus.INDETERMINATE,
                "stale_verification": WorkflowLoopStatus.INDETERMINATE,
            }[error.kind]
            return await self._result(refreshed, status, error.kind)
        if outcome.disposition == "busy":
            return await self._result(run, WorkflowLoopStatus.WAITING, "Activity execution is busy")
        if not outcome.attempt.state.terminal:
            return await self._result(
                run, WorkflowLoopStatus.WAITING, f"Activity {outcome.attempt.state.value}"
            )
        if outcome.attempt.state is not WorkflowActivityState.COMPLETED:
            status = {
                WorkflowActivityState.BLOCKED: WorkflowLoopStatus.BLOCKED,
                WorkflowActivityState.FAILED: WorkflowLoopStatus.FAILED,
                WorkflowActivityState.INDETERMINATE: WorkflowLoopStatus.INDETERMINATE,
            }[outcome.attempt.state]
            refreshed = await self.state.get_workflow_run(run.run_id)
            assert refreshed is not None
            return await self._result(
                refreshed, status, f"{declaration.activity.value} {outcome.attempt.state.value}"
            )
        return None

    async def _completion_projection(
        self, run: WorkflowRun, plan: BoundedVerificationLoopPlan
    ) -> WorkflowLoopResult:
        terminal_verify_index = -1
        terminal_status: str | None = None
        for index, step_id in enumerate(plan.verification_step_ids):
            instance = next(
                (step for step in run.steps if step.identity == StepIdentity(step_id)), None
            )
            if instance is None or instance.status is not WorkflowStatus.COMPLETED:
                continue
            output = await self.facts.get_workflow_step_output(run.run_id, instance.identity)
            if output is None or output.source_id != invocation_id(
                run.run_id, instance.identity, instance.input_fingerprint
            ):
                raise WorkflowStateError("consumed VERIFY output binding differs", kind="integrity")
            value = json.loads(output.output_json)
            if not isinstance(value, dict) or value.get("status") not in {"PASS", "FAIL"}:
                raise WorkflowStateError("consumed VERIFY outcome is invalid", kind="integrity")
            terminal_verify_index = index
            terminal_status = cast(str, value["status"])
        if terminal_verify_index < 0 or terminal_status is None:
            return await self._result(
                run, WorkflowLoopStatus.WAITING, "control flow ended without a consumed real VERIFY"
            )
        latest_repair_index = -1
        for index, step_id in enumerate(plan.repair_step_ids):
            instance = next(
                (step for step in run.steps if step.identity == StepIdentity(step_id)), None
            )
            if instance is not None and instance.status is WorkflowStatus.COMPLETED:
                latest_repair_index = index + 1
        # Repairs are indexed after their causal VERIFY. A later repair invalidates
        # any earlier PASS as a final loop result.
        if latest_repair_index >= terminal_verify_index + 1:
            return await self._result(
                run, WorkflowLoopStatus.WAITING, "a later REPAIR has no fresh consumed VERIFY"
            )
        if terminal_status == "PASS":
            return await self._result(
                run,
                WorkflowLoopStatus.VERIFIED,
                "fresh VERIFY PASS consumed; Workflow completion requirements remain pending",
            )
        if terminal_verify_index == len(plan.verification_step_ids) - 1:
            return await self._result(
                run,
                WorkflowLoopStatus.EXHAUSTED,
                "final VERIFY returned FAIL; no unverified REPAIR was dispatched",
            )
        return await self._result(
            run, WorkflowLoopStatus.WAITING, "FAIL branch has not reached its bounded terminal"
        )

    async def _result(
        self, run: WorkflowRun, status: WorkflowLoopStatus, reason: str
    ) -> WorkflowLoopResult:
        definition = await self.state.get_workflow_definition(run.definition_fingerprint)
        if definition is None:
            raise WorkflowStateError("definition missing", kind="integrity")
        plan = bounded_verification_loop_plan(definition)
        verify_count = 0
        repair_count = 0
        declarations = _step_declarations(definition.steps)
        for instance in run.steps:
            declaration = declarations.get(instance.identity.step_id)
            if not isinstance(declaration, Activity):
                continue
            if declaration.activity not in {ActivityKind.VERIFY, ActivityKind.REPAIR}:
                continue
            attempt_id = invocation_id(run.run_id, instance.identity, instance.input_fingerprint)
            attempt = await self.activities.get_workflow_activity(attempt_id)
            if attempt is None:
                raise WorkflowStateError("published loop Activity is missing", kind="integrity")
            if not attempt.state.terminal:
                continue
            if declaration.activity is ActivityKind.VERIFY:
                verify_count += 1
            else:
                repair_count += 1
        if verify_count > len(plan.verification_step_ids) or repair_count > len(
            plan.repair_step_ids
        ):
            raise WorkflowStateError("durable loop count exceeds typed plan", kind="integrity")
        return WorkflowLoopResult(run, status, verify_count, repair_count, reason)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)


def _step_declarations(steps: tuple[Step, ...]) -> dict[str, Step]:
    result: dict[str, Step] = {}

    def walk(items: tuple[Step, ...]) -> None:
        for step in items:
            result[step.step_id] = step
            if isinstance(step, Branch):
                for _, body in step.paths:
                    walk(body)
            elif isinstance(step, Repeat):
                walk(step.steps)

    walk(steps)
    return result
