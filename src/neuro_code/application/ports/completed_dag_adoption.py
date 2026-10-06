"""Narrow provenance seam; adapters resolve facts, not mutation authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neuro_code.domain.completed_dag_adoption import CompletedDagAdoptionSource
from neuro_code.domain.result_adoption import ResultAdoptionRequest
from neuro_code.domain.task_dag import TaskDag, TaskDagState


@dataclass(frozen=True, slots=True)
class ResolvedCompletedDagSource:
    source: CompletedDagAdoptionSource
    dag: TaskDag

    def __post_init__(self) -> None:
        if not isinstance(self.source, CompletedDagAdoptionSource) or not isinstance(
            self.dag, TaskDag
        ):
            raise TypeError("resolved completed DAG requires canonical source and DAG")
        if (
            self.dag.state is not TaskDagState.COMPLETED
            or self.source.parent_session_id != self.dag.parent_session_id
            or self.source.dag_id != self.dag.dag_id
            or self.source.dag_generation != self.dag.generation
            or self.source.dag_definition_fingerprint != self.dag.definition_fingerprint
        ):
            raise ValueError("resolved completed DAG does not match source provenance")


class CompletedDagAdoptionSourceAdapter(Protocol):
    async def resolve(
        self, request: ResultAdoptionRequest, *, parent_session_id: str
    ) -> ResolvedCompletedDagSource: ...
