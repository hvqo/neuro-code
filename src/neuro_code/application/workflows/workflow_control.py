"""Bounded next-action selection from DW1 order and DW2 durable facts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.definition import Branch, Map, Repeat, Step, WorkflowDefinition
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    StepIdentity,
    WorkflowEventKind,
    WorkflowJournalEvent,
    WorkflowRun,
    WorkflowStatus,
)


class ActionKind(StrEnum):
    INITIALIZE = "initialize_step"
    EXECUTE = "execute_ready_step"
    CONSUME = "consume_projection"
    BRANCH = "record_branch"
    ITERATION = "begin_iteration"
    UNTIL = "evaluate_until"
    END = "control_flow_exhausted"


@dataclass(frozen=True, slots=True)
class ControlAction:
    kind: ActionKind
    step: Step | None = None
    identity: StepIdentity | None = None


class ControlFacts:
    def __init__(
        self,
        definition: WorkflowDefinition,
        run: WorkflowRun,
        events: tuple[WorkflowJournalEvent, ...],
    ) -> None:
        self.definition = definition
        self.run = run
        self.steps = {s.identity: s for s in run.steps}
        self.branches: dict[StepIdentity, str] = {}
        self.iterations: dict[str, int] = {}
        for event in events:
            if event.generation > run.generation:
                raise WorkflowStateError("control snapshot changed", kind="concurrent_modification")
            if event.kind not in {WorkflowEventKind.BRANCH, WorkflowEventKind.ITERATION}:
                continue
            payload = json.loads(event.payload_json)
            if (
                canonical(payload) != event.payload_json
                or digest(payload) != event.payload_fingerprint
            ):
                raise WorkflowStateError("control fact integrity mismatch", kind="integrity")
            change = payload["change"]
            identity = StepIdentity(**change["position"])
            if event.kind is WorkflowEventKind.BRANCH:
                if identity in self.branches:
                    raise WorkflowStateError("duplicate branch fact", kind="integrity")
                self.branches[identity] = change["selected_path"]
            else:
                if identity.iteration != self.iterations.get(identity.step_id, 0) + 1:
                    raise WorkflowStateError("iteration continuity mismatch", kind="integrity")
                self.iterations[identity.step_id] = identity.iteration
        self.visits = 0

    def next_action(self) -> ControlAction:
        return self._walk(self.definition.steps, 0) or ControlAction(ActionKind.END)

    def _walk(self, steps: tuple[Step, ...], iteration: int) -> ControlAction | None:
        for step in steps:
            self.visits += 1
            if self.visits > 256:
                raise WorkflowStateError("bounded control traversal exhausted", kind="bounds")
            identity = StepIdentity(step.step_id, iteration)
            old = self.steps.get(identity)
            if isinstance(step, Branch):
                selected = self.branches.get(identity)
                if selected is None:
                    return ControlAction(ActionKind.BRANCH, step, identity)
                if selected not in dict(step.paths):
                    raise WorkflowStateError("unknown durable branch path", kind="integrity")
                action = self._walk(dict(step.paths)[selected], iteration)
                if action:
                    return action
            elif isinstance(step, Repeat):
                if old is None:
                    return ControlAction(ActionKind.INITIALIZE, step, identity)
                if old.status is WorkflowStatus.COMPLETED:
                    continue
                if old.status is WorkflowStatus.READY:
                    return ControlAction(ActionKind.EXECUTE, step, identity)
                if old.status is not WorkflowStatus.RUNNING:
                    raise WorkflowStateError("invalid Repeat control state", kind="integrity")
                current = self.iterations.get(step.step_id, 0)
                if not 0 <= current <= step.max_iterations:
                    raise WorkflowStateError("iteration outside bound", kind="integrity")
                if current == 0:
                    return ControlAction(ActionKind.ITERATION, step, StepIdentity(step.step_id, 1))
                action = self._walk(step.steps, current)
                if action:
                    return action
                return ControlAction(ActionKind.UNTIL, step, StepIdentity(step.step_id, current))
            elif old is None:
                return ControlAction(ActionKind.INITIALIZE, step, identity)
            elif old.status is WorkflowStatus.READY:
                return ControlAction(ActionKind.EXECUTE, step, identity)
            elif old.status is WorkflowStatus.WAITING:
                return ControlAction(ActionKind.CONSUME, step, identity)
            elif old.status is not WorkflowStatus.COMPLETED:
                raise WorkflowStateError(
                    "unresolved step requires reconciliation", kind="needs_attention"
                )
        return None


def declared_steps(definition: WorkflowDefinition) -> dict[str, Step]:
    result: dict[str, Step] = {}

    def walk(steps: tuple[Step, ...]) -> None:
        for step in steps:
            result[step.step_id] = step
            if isinstance(step, Branch):
                for _, body in step.paths:
                    walk(body)
            elif isinstance(step, Repeat):
                walk(step.steps)
            elif isinstance(step, Map):
                walk((step.batch,))

    walk(definition.steps)
    return result


def control_input(run: WorkflowRun, identity: StepIdentity) -> str:
    return digest(
        {"run_id": run.run_id, "definition": run.definition_fingerprint, "step": asdict(identity)}
    )
