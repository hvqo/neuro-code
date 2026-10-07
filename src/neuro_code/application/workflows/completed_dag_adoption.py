"""Swarm and Workflow completed-DAG adapters; no copy-back or control flow."""

from __future__ import annotations

import json
from dataclasses import asdict

from neuro_code.application.ports.agent_swarm import AgentSwarmStore
from neuro_code.application.ports.completed_dag_adoption import ResolvedCompletedDagSource
from neuro_code.application.ports.result_adoption import ResultAdoptionError
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.workflow_projection import WorkflowProjectionStore
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.domain.agent_swarm import AgentSwarmRunState
from neuro_code.domain.completed_dag_adoption import (
    CompletedDagAdoptionSource,
    CompletedDagSourceKind,
)
from neuro_code.domain.result_adoption import ResultAdoptionRequest
from neuro_code.domain.task_dag import TaskDagNodeState, TaskDagState
from neuro_code.domain.workflows.publication import canonical
from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceState
from neuro_code.shared.errors import SessionError


class SwarmCompletedDagSourceAdapter:
    def __init__(self, *, swarms: AgentSwarmStore, dags: TaskDagStore) -> None:
        self._swarms = swarms
        self._dags = dags

    async def resolve(
        self, request: ResultAdoptionRequest, *, parent_session_id: str
    ) -> ResolvedCompletedDagSource:
        if request.swarm_run_id is None or request.workflow_source is not None:
            raise ResultAdoptionError("Swarm source reference is required", kind="integrity")
        run = await self._swarms.get_swarm_run(request.swarm_run_id)
        if run is None:
            raise ResultAdoptionError("completed Swarm run is missing", kind="unmanaged")
        if run.state is not AgentSwarmRunState.COMPLETED:
            raise ResultAdoptionError("Swarm run is not completed", kind="stale_source")
        if run.parent_session_id != parent_session_id or run.current_dag_id is None:
            raise ResultAdoptionError("Swarm parent identity does not match", kind="integrity")
        if run.current_dag_generation is None or run.current_dag_definition_fingerprint is None:
            raise ResultAdoptionError(
                "completed Swarm DAG identity is incomplete", kind="integrity"
            )
        dag = await self._dags.get_task_dag(run.current_dag_id)
        if dag is None:
            raise ResultAdoptionError("completed source DAG is missing", kind="unmanaged")
        if (
            dag.state is not TaskDagState.COMPLETED
            or dag.parent_session_id != parent_session_id
            or dag.generation != run.current_dag_generation
            or dag.definition_fingerprint != run.current_dag_definition_fingerprint
        ):
            raise ResultAdoptionError("source DAG identity or state is stale", kind="stale_source")
        return ResolvedCompletedDagSource(
            CompletedDagAdoptionSource(
                CompletedDagSourceKind.SWARM,
                run.swarm_run_id,
                parent_session_id,
                dag.dag_id,
                dag.generation,
                dag.definition_fingerprint,
            ),
            dag,
        )


class WorkflowCompletedDagSourceAdapter:
    def __init__(
        self,
        *,
        projections: WorkflowProjectionStore,
        dags: TaskDagStore,
        leases: WritableSubagentLeaseStore,
    ) -> None:
        self._projections = projections
        self._dags = dags
        self._leases = leases

    async def resolve(
        self, request: ResultAdoptionRequest, *, parent_session_id: str
    ) -> ResolvedCompletedDagSource:
        ref = request.workflow_source
        if ref is None or request.swarm_run_id is not None:
            raise ResultAdoptionError("Workflow source reference is required", kind="integrity")
        try:
            # This port revalidates the exact DW3 Expansion, Run/Step, frozen
            # bindings, node generations, evidence, relay and workspace facts.
            projection = await self._projections.get_workflow_result_projection(
                ref.expansion_id,
                run_id=ref.run_id,
                parent_session_id=parent_session_id,
            )
        except (WorkflowStateError, SessionError) as error:
            raise ResultAdoptionError(
                "Workflow projection failed integrity checks", kind="integrity"
            ) from error
        if projection is None:
            raise ResultAdoptionError("Workflow projection is missing", kind="unmanaged")
        if (
            projection.projection_id != ref.projection_id
            or projection.fingerprint != ref.projection_fingerprint
            or projection.run_id != ref.run_id
            or projection.expansion_id != ref.expansion_id
            or projection.parent_session_id != parent_session_id
        ):
            raise ResultAdoptionError("Workflow projection identity is stale", kind="integrity")
        facts = json.loads(projection.source_json)
        dag = await self._dags.get_task_dag(projection.dag_id)
        if (
            dag is None
            or dag.state is not TaskDagState.COMPLETED
            or any(node.state is not TaskDagNodeState.COMPLETED for node in dag.nodes)
        ):
            raise ResultAdoptionError(
                "Workflow adoption requires completed DAG", kind="stale_source"
            )
        if (
            dag.parent_session_id != parent_session_id
            or dag.dag_id != facts["dag_id"]
            or dag.generation != facts["dag_generation"]
            or dag.definition_fingerprint != facts["dag_definition_fingerprint"]
            or dag.max_parallel != facts["max_parallel"]
            or len(dag.nodes) != len(facts["nodes"])
            or any(
                canonical(asdict(dag.node(fact["member"]["node_id"]))) != canonical(fact["node"])
                for fact in facts["nodes"]
            )
        ):
            raise ResultAdoptionError(
                "Workflow DAG differs from exact projection", kind="integrity"
            )
        for node in dag.nodes:
            if node.lease_id is None:
                raise ResultAdoptionError("Workflow worker lease is missing", kind="integrity")
            lease = await self._leases.get_writable_subagent_lease(node.lease_id)
            if lease is None or lease.state is not WritableSubagentWorkspaceState.PRESERVED:
                raise ResultAdoptionError(
                    "Workflow worker lease is not preserved", kind="stale_source"
                )
        return ResolvedCompletedDagSource(
            CompletedDagAdoptionSource(
                CompletedDagSourceKind.WORKFLOW,
                projection.projection_id,
                parent_session_id,
                dag.dag_id,
                dag.generation,
                dag.definition_fingerprint,
                ref,
                projection.source_fingerprint,
            ),
            dag,
        )


class CompletedDagSourceAdapters:
    """Dispatch provenance only; both sources use the same adoption engine."""

    def __init__(
        self,
        *,
        swarms: AgentSwarmStore,
        dags: TaskDagStore,
        projections: WorkflowProjectionStore,
        leases: WritableSubagentLeaseStore,
    ) -> None:
        self._swarm = SwarmCompletedDagSourceAdapter(swarms=swarms, dags=dags)
        self._workflow = WorkflowCompletedDagSourceAdapter(
            projections=projections, dags=dags, leases=leases
        )

    async def resolve(
        self, request: ResultAdoptionRequest, *, parent_session_id: str
    ) -> ResolvedCompletedDagSource:
        adapter = self._workflow if request.workflow_source is not None else self._swarm
        return await adapter.resolve(request, parent_session_id=parent_session_id)
