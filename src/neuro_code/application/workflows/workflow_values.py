"""Typed references resolve durable exact facts, never previews or transient caller data."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from neuro_code.application.ports.workflow_interpreter import WorkflowInterpreterStore
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.workflows.workflow_control import ControlFacts, declared_steps
from neuro_code.domain.workflows.definition import (
    Activity,
    ArtifactRef,
    Bindings,
    Condition,
    ConditionOp,
    InputRef,
    InputValue,
    ItemRef,
    Literal,
    Map,
    Repeat,
    ResultRef,
    TaskBatch,
)
from neuro_code.domain.workflows.state import StepIdentity, WorkflowStatus


class WorkflowValues:
    def __init__(
        self, store: WorkflowInterpreterStore, facts: ControlFacts, input_json: str
    ) -> None:
        self.store = store
        self.facts = facts
        self.input: Any = json.loads(input_json)
        self.declarations = declared_steps(facts.definition)

    async def resolve(
        self, ref: InputValue, *, iteration: int = 0, item: object | None = None
    ) -> Any:
        if isinstance(ref, Literal):
            return ref.value
        if isinstance(ref, ArtifactRef):
            return asdict(ref)
        if isinstance(ref, InputRef):
            value = self.input
        elif isinstance(ref, ItemRef):
            if item is None:
                raise WorkflowStateError("ItemRef outside Map scope", kind="protocol")
            value = item
        elif isinstance(ref, ResultRef):
            declared = self.declarations[ref.step_id]
            if ref.item_key is not None:
                raise WorkflowStateError("unproven item selector", kind="protocol")
            if isinstance(declared, Repeat):
                current = self.facts.iterations.get(ref.step_id, 0)
                completed = self.facts.steps.get(StepIdentity(ref.step_id))
                if (
                    completed is None
                    or completed.status is not WorkflowStatus.COMPLETED
                    or current == 0
                ):
                    raise WorkflowStateError("Repeat result is not final", kind="protocol")
                if ref.iteration is not None and ref.iteration != 1:
                    raise WorkflowStateError("unproven iteration selector", kind="protocol")
                body = await self._body(declared, ref.iteration or current)
                value = body if ref.iteration else {"iterations": current, "last": body}
            else:
                if ref.iteration is not None:
                    raise WorkflowStateError("invalid direct iteration selector", kind="protocol")
                identity = StepIdentity(ref.step_id, iteration)
                # An outer dominating step remains iteration zero inside a Repeat.
                if identity not in self.facts.steps:
                    identity = StepIdentity(ref.step_id)
                result = await self.store.get_workflow_step_output(self.facts.run.run_id, identity)
                if result is None:
                    raise WorkflowStateError("exact typed result missing", kind="missing")
                value = json.loads(result.output_json)
        else:
            raise WorkflowStateError("unknown typed reference", kind="protocol")
        for part in ref.field_path:
            if not isinstance(value, dict) or part not in value:
                raise WorkflowStateError("typed field path is missing", kind="integrity")
            value = value[part]
        return value

    async def _body(self, repeat: Repeat, iteration: int) -> dict[str, object]:
        # DW1 promotes only directly dominating body outputs, not branch-local ones.
        result = {}
        for step in repeat.steps:
            if step.step_id in self.facts.iterations:  # Nested Repeat is rejected by DW1.
                raise WorkflowStateError("nested Repeat unsupported", kind="protocol")

            if isinstance(step, Activity | TaskBatch | Map):
                output = await self.store.get_workflow_step_output(
                    self.facts.run.run_id, StepIdentity(step.step_id, iteration)
                )
                if output is None:
                    raise WorkflowStateError("Repeat body result missing", kind="missing")
                result[step.step_id] = json.loads(output.output_json)
        return result

    async def bindings(
        self, bindings: Bindings, *, iteration: int, item: object | None = None
    ) -> dict[str, object]:
        return {
            name: await self.resolve(ref, iteration=iteration, item=item) for name, ref in bindings
        }

    async def condition(self, condition: Condition, *, iteration: int) -> bool:
        value = await self.resolve(condition.ref, iteration=iteration)
        if condition.op is ConditionOp.EXISTS:
            return True  # DW1 only admits required typed paths; missing facts fail closed.
        assert condition.value is not None
        return type(value) is type(condition.value) and value == condition.value
