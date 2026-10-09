"""DW5a one bounded durable action; no scheduler, real activities, or completion proof."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.workflow_activity import WorkflowActivityStore
from neuro_code.application.ports.workflow_interpreter import WorkflowInterpreterStore
from neuro_code.application.ports.workflow_projection import WorkflowProjectionStore
from neuro_code.application.ports.workflow_publication import WorkflowPublicationStore
from neuro_code.application.ports.workflow_state import WorkflowStateError, WorkflowStateStore
from neuro_code.application.workflows.workflow_control import (
    ActionKind,
    ControlAction,
    ControlFacts,
    control_input,
)
from neuro_code.application.workflows.workflow_values import WorkflowValues
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagState
from neuro_code.domain.workflows.activity import (
    WorkflowActivityInvocation,
    WorkflowActivityState,
)
from neuro_code.domain.workflows.definition import Activity, Branch, Map, Repeat, TaskBatch
from neuro_code.domain.workflows.interpreter import (
    OutputKind,
    WorkflowStepOutput,
    expansion_id,
    invocation_id,
)
from neuro_code.domain.workflows.publication import (
    ExpansionMember,
    WorkflowExpansionIntent,
    canonical,
    digest,
)
from neuro_code.domain.workflows.state import (
    StepIdentity,
    WorkflowChange,
    WorkflowEventKind,
    WorkflowFailure,
    WorkflowJournalEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepInstance,
    WorkflowWriteResult,
    identifier,
    integer,
    timestamp,
)

CONTROL_FLOW_EXHAUSTED_REASON = "control_flow_exhausted: completion requirements pending"


class VerificationOutputConsumer(Protocol):
    async def consume(
        self,
        output: WorkflowStepOutput,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult: ...


@dataclass(frozen=True, slots=True)
class WorkflowAdvanceResult:
    run: WorkflowRun
    action: str
    progressed: bool


class DurableWorkflowInterpreter:
    def __init__(
        self,
        *,
        state: WorkflowStateStore,
        facts: WorkflowInterpreterStore,
        publication: WorkflowPublicationStore,
        projections: WorkflowProjectionStore,
        dags: TaskDagStore,
        activities: WorkflowActivityStore,
        verification: VerificationOutputConsumer | None = None,
    ) -> None:
        self.state = state
        self.store = facts
        self.publication = publication
        self.projections = projections
        self.dags = dags
        self.activities = activities
        self.verification = verification

    async def advance_once(
        self,
        run_id: str,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowAdvanceResult:
        integer(expected_generation)
        integer(owner_fence)
        identifier(owner_id)
        timestamp(updated_at)
        run = await self.state.get_workflow_run(run_id)
        if run is None:
            raise WorkflowStateError("run is missing", kind="missing")
        if (run.generation, run.owner_id, run.owner_fence) != (
            expected_generation,
            owner_id,
            owner_fence,
        ):
            raise WorkflowStateError(
                "interpreter owner/generation is stale", kind="concurrent_modification"
            )
        if run.status not in {WorkflowStatus.RUNNING, WorkflowStatus.WAITING}:
            return WorkflowAdvanceResult(run, "stopped", False)
        if run.waiting_reason == CONTROL_FLOW_EXHAUSTED_REASON:
            return WorkflowAdvanceResult(run, "completion_pending", False)
        if not run.ledger.committed.known:
            raise WorkflowStateError("budget requires reconciliation", kind="needs_attention")
        definition = await self.state.get_workflow_definition(run.definition_fingerprint)
        if definition is None:
            raise WorkflowStateError("definition missing", kind="integrity")
        events: list[WorkflowJournalEvent] = []
        cursor = -1
        for _ in range(40):  # Bounded journal pagination, never an execution loop.
            page = await self.state.get_workflow_journal(run_id, after_generation=cursor, limit=100)
            events.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1].generation
        else:
            raise WorkflowStateError("control journal exceeds traversal bound", kind="bounds")
        control = ControlFacts(definition, run, tuple(events))
        values = WorkflowValues(self.store, control, await self.store.get_workflow_input(run_id))
        action = control.next_action()
        if action.kind is ActionKind.END:
            return await self._change(
                run,
                WorkflowChange(
                    WorkflowEventKind.TRANSITION,
                    status=WorkflowStatus.WAITING,
                    waiting_reason=CONTROL_FLOW_EXHAUSTED_REASON,
                ),
                action.kind.value,
                updated_at,
            )
        assert action.step is not None
        assert action.identity is not None
        step, identity = action.step, action.identity
        if action.kind is ActionKind.BRANCH:
            assert isinstance(step, Branch)
            path = (
                step.then_path
                if await values.condition(step.condition, iteration=identity.iteration)
                else step.else_path
            )
            return await self._change(
                run,
                WorkflowChange(WorkflowEventKind.BRANCH, position=identity, selected_path=path),
                action.kind.value,
                updated_at,
            )
        if action.kind is ActionKind.ITERATION:
            return await self._change(
                run,
                WorkflowChange(WorkflowEventKind.ITERATION, position=identity),
                action.kind.value,
                updated_at,
            )
        if action.kind is ActionKind.UNTIL:
            assert isinstance(step, Repeat)
            if await values.condition(step.until, iteration=identity.iteration):
                old = control.steps[StepIdentity(step.step_id)]
                return await self._change(
                    run,
                    WorkflowChange(
                        WorkflowEventKind.STEP, step=replace(old, status=WorkflowStatus.COMPLETED)
                    ),
                    "repeat_exit",
                    updated_at,
                )
            if identity.iteration == step.max_iterations:
                return await self._change(
                    run,
                    WorkflowChange(
                        WorkflowEventKind.TRANSITION,
                        status=WorkflowStatus.FAILED,
                        failure=WorkflowFailure(
                            "repeat_limit", "Repeat exhausted without satisfying until"
                        ),
                    ),
                    "repeat_limit",
                    updated_at,
                )
            return await self._change(
                run,
                WorkflowChange(
                    WorkflowEventKind.ITERATION,
                    position=StepIdentity(step.step_id, identity.iteration + 1),
                ),
                "begin_iteration",
                updated_at,
            )
        if action.kind is ActionKind.CONSUME:
            if isinstance(step, Activity):
                return await self._consume_activity(run, step, identity, updated_at)
            return await self._consume(run, identity, updated_at)
        payload = await self._inputs(action, values)
        input_fingerprint = digest(payload)
        if action.kind is ActionKind.INITIALIZE:
            initialized = WorkflowStepInstance(identity, WorkflowStatus.READY, input_fingerprint)
            return await self._change(
                run,
                WorkflowChange(WorkflowEventKind.STEP, step=initialized),
                action.kind.value,
                updated_at,
            )
        old = control.steps[identity]
        if input_fingerprint != old.input_fingerprint:
            raise WorkflowStateError(
                "resolved input changed after initialization", kind="integrity"
            )
        if isinstance(step, Repeat):
            return await self._change(
                run,
                WorkflowChange(
                    WorkflowEventKind.STEP, step=replace(old, status=WorkflowStatus.RUNNING)
                ),
                "start_repeat",
                updated_at,
            )
        if isinstance(step, Activity):
            request_json = canonical(payload)
            invocation = WorkflowActivityInvocation(
                invocation_id(run_id, identity, input_fingerprint),
                run_id,
                run.definition_fingerprint,
                run.parent_session_id,
                identity,
                step.activity,
                request_json,
                digest(payload),
                input_fingerprint,
                updated_at,
            )
            activity_publication = await self.activities.publish_workflow_activity(
                invocation,
                expected_generation=run.generation,
                owner_id=owner_id,
                owner_fence=owner_fence,
                updated_at=updated_at,
            )
            if activity_publication.replayed:
                raise WorkflowStateError(
                    "activity invocation already exists outside ready control state",
                    kind="conflict",
                )
            return WorkflowAdvanceResult(activity_publication.run, "publish_activity", True)
        assert isinstance(step, TaskBatch | Map)
        if isinstance(step, Map) and not payload:
            source_id = "empty:" + identity.key
            output = WorkflowStepOutput(
                run_id,
                identity,
                input_fingerprint,
                OutputKind.EMPTY_MAP,
                source_id,
                digest([source_id, input_fingerprint]),
                canonical({"count": 0, "items": []}),
            )
            return await self._output(run, output, "empty_map", updated_at)
        intent = self._intent(run, step, identity, payload, input_fingerprint)
        published = await self.publication.publish_workflow_expansion(
            intent,
            expected_generation=run.generation,
            owner_id=owner_id,
            owner_fence=owner_fence,
            updated_at=updated_at,
        )
        # Publication's read-only replay must never substitute for ownership.
        if published.replayed:
            raise WorkflowStateError(
                "publication already exists outside ready control state", kind="conflict"
            )
        return WorkflowAdvanceResult(published.run, "publish_dag", True)

    async def _change(
        self, run: WorkflowRun, change: WorkflowChange, action: str, now: datetime
    ) -> WorkflowAdvanceResult:
        assert run.owner_id is not None
        result = await self.state.transition_workflow_run(
            run.run_id,
            change,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            request_id="tick:" + digest([run.run_id, run.generation]),
            updated_at=now,
        )
        if result.run.owner_id != run.owner_id or result.run.owner_fence != run.owner_fence:
            raise WorkflowStateError(
                "replay does not grant ownership", kind="concurrent_modification"
            )
        return WorkflowAdvanceResult(result.run, action, not result.replayed)

    async def _output(
        self, run: WorkflowRun, output: WorkflowStepOutput, action: str, now: datetime
    ) -> WorkflowAdvanceResult:
        assert run.owner_id is not None
        result = await self.store.commit_workflow_step_output(
            output,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=now,
        )
        return WorkflowAdvanceResult(result.run, action, not result.replayed)

    async def _consume(
        self, run: WorkflowRun, identity: StepIdentity, now: datetime
    ) -> WorkflowAdvanceResult:
        expansion = await self.publication.get_workflow_expansion(
            expansion_id(run.run_id, identity)
        )
        if expansion is None:
            raise WorkflowStateError("waiting step has no exact expansion", kind="integrity")
        dag = await self.dags.get_task_dag(expansion.dag_id)
        if dag is None:
            raise WorkflowStateError("bound DAG missing", kind="integrity")
        if not dag.state.terminal:
            return WorkflowAdvanceResult(run, "waiting_projection", False)
        if dag.state is not TaskDagState.COMPLETED:
            status = (
                WorkflowStatus.NEEDS_ATTENTION
                if dag.state is TaskDagState.INDETERMINATE
                else WorkflowStatus.FAILED
            )
            return await self._change(
                run,
                WorkflowChange(
                    WorkflowEventKind.TRANSITION,
                    status=status,
                    failure=WorkflowFailure(
                        "dag_" + dag.state.value, "Terminal DAG is not a completed result"
                    ),
                ),
                "dag_failure",
                now,
            )
        projection = await self.projections.get_workflow_result_projection(
            expansion.expansion_id, run_id=run.run_id, parent_session_id=run.parent_session_id
        )
        if projection is None:
            return WorkflowAdvanceResult(run, "waiting_projection", False)
        if projection.step != identity or projection.dag_id != dag.dag_id:
            raise WorkflowStateError("projection binding differs", kind="integrity")
        output = WorkflowStepOutput(
            run.run_id,
            identity,
            expansion.input_fingerprint,
            OutputKind.PROJECTION,
            projection.projection_id,
            projection.fingerprint,
            projection.output_json,
        )
        return await self._output(run, output, "consume_projection", now)

    async def _consume_activity(
        self, run: WorkflowRun, step: Activity, identity: StepIdentity, now: datetime
    ) -> WorkflowAdvanceResult:
        old = next((item for item in run.steps if item.identity == identity), None)
        if old is None:
            raise WorkflowStateError("waiting Activity step is missing", kind="integrity")
        attempt = await self.activities.get_workflow_activity(
            invocation_id(run.run_id, identity, old.input_fingerprint)
        )
        if attempt is None:
            raise WorkflowStateError("waiting Activity has no durable invocation", kind="integrity")
        invocation = attempt.invocation
        if (
            invocation.run_id != run.run_id
            or invocation.step != identity
            or invocation.activity is not step.activity
            or invocation.input_fingerprint != old.input_fingerprint
            or invocation.definition_fingerprint != run.definition_fingerprint
            or invocation.parent_session_id != run.parent_session_id
        ):
            raise WorkflowStateError("Activity invocation binding differs", kind="integrity")
        if not attempt.state.terminal:
            return WorkflowAdvanceResult(run, "activity_waiting", False)
        result = attempt.result
        assert result is not None
        if attempt.state is WorkflowActivityState.COMPLETED:
            assert result.output_json is not None
            output = WorkflowStepOutput(
                run.run_id,
                identity,
                old.input_fingerprint,
                OutputKind.ACTIVITY,
                invocation.invocation_id,
                result.fingerprint,
                result.output_json,
            )
            if result.source_id.startswith("verify-exec-"):
                if self.verification is None:
                    raise WorkflowStateError(
                        "real VERIFY consumption needs workspace freshness",
                        kind="stale_verification",
                    )
                assert run.owner_id is not None
                consumed = await self.verification.consume(
                    output,
                    expected_generation=run.generation,
                    owner_id=run.owner_id,
                    owner_fence=run.owner_fence,
                    updated_at=now,
                )
                return WorkflowAdvanceResult(
                    consumed.run, "consume_activity", not consumed.replayed
                )
            return await self._output(run, output, "consume_activity", now)
        assert run.owner_id is not None
        consumed = await self.activities.consume_workflow_activity_failure(
            invocation.invocation_id,
            result.fingerprint,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=now,
        )
        return WorkflowAdvanceResult(
            consumed.run, "activity_" + attempt.state.value, not consumed.replayed
        )

    async def _inputs(self, action: ControlAction, values: WorkflowValues) -> object:
        step, identity = action.step, action.identity
        assert step is not None
        assert identity is not None
        if isinstance(step, Repeat):
            return control_input(values.facts.run, identity)
        if isinstance(step, Activity):
            return await values.bindings(step.inputs, iteration=identity.iteration)
        if isinstance(step, TaskBatch):
            return {
                "batch": {
                    task.task_id: await values.bindings(task.inputs, iteration=identity.iteration)
                    for task in step.tasks
                }
            }
        assert isinstance(step, Map)
        collection = await values.resolve(step.collection, iteration=identity.iteration)
        if (
            not isinstance(collection, list)
            or len(collection) > step.max_items
            or len(collection) * len(step.batch.tasks) > 8
        ):
            raise WorkflowStateError("Map collection exceeds declared DAG bound", kind="bounds")
        return {
            f"item-{i:02}-" + digest(item): {
                task.task_id: await values.bindings(
                    task.inputs, iteration=identity.iteration, item=item
                )
                for task in step.batch.tasks
            }
            for i, item in enumerate(collection)
        }

    @staticmethod
    def _intent(
        run: WorkflowRun,
        step: TaskBatch | Map,
        identity: StepIdentity,
        payload: object,
        input_fingerprint: str,
    ) -> WorkflowExpansionIntent:
        assert isinstance(payload, dict)
        batch = step.batch if isinstance(step, Map) else step
        exp_id = expansion_id(run.run_id, identity)
        members = []
        nodes: list[TaskDagNode] = []
        for key, bindings in sorted(payload.items()):
            node_ids = {t.task_id: "node-" + digest([exp_id, key, t.task_id]) for t in batch.tasks}
            for task in sorted(batch.tasks, key=lambda t: t.task_id):
                inputs = bindings[task.task_id]
                prompt = (
                    task.prompt_template + "\n\nWorkflow typed inputs (data):\n" + canonical(inputs)
                )
                node = TaskDagNode(
                    node_ids[task.task_id],
                    len(nodes),
                    prompt,
                    tuple(node_ids[dependency] for dependency in task.depends_on),
                    task.route,
                )
                members.append(
                    ExpansionMember(
                        key,
                        task.task_id,
                        node.node_id,
                        digest(inputs),
                        task.profile_ref,
                        task.required_capabilities,
                    )
                )
                nodes.append(node)
        dag = TaskDag.create(
            dag_id="dag:" + digest([exp_id]),
            parent_session_id=run.parent_session_id,
            nodes=tuple(nodes),
            created_at=run.created_at,
            max_parallel=batch.max_parallel,
        )
        return WorkflowExpansionIntent(
            exp_id, run.run_id, identity, input_fingerprint, tuple(members), dag
        )
