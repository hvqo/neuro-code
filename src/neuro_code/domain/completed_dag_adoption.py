"""Typed completed-DAG provenance, without adoption or Workflow authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from neuro_code.domain.task_dag import _safe_identifier
from neuro_code.domain.workflows.state import fingerprint, identifier


class CompletedDagSourceKind(StrEnum):
    SWARM = "swarm"
    WORKFLOW = "workflow"


@dataclass(frozen=True, slots=True)
class WorkflowAdoptionSourceRef:
    """Caller pins an existing projection; it cannot supply a DAG or result text."""

    run_id: str
    expansion_id: str
    projection_id: str
    projection_fingerprint: str

    def __post_init__(self) -> None:
        for value in (self.run_id, self.expansion_id, self.projection_id):
            identifier(value)
        fingerprint(self.projection_fingerprint)


@dataclass(frozen=True, slots=True)
class CompletedDagAdoptionSource:
    """Durable exact provenance shared by Swarm and Workflow adapters.

    Workflow projection source digest also binds step/iteration/item, frozen
    members, node generations and worker/workspace facts. No ownership is granted.
    """

    kind: CompletedDagSourceKind
    source_id: str
    parent_session_id: str
    dag_id: str
    dag_generation: int
    dag_definition_fingerprint: str
    workflow: WorkflowAdoptionSourceRef | None = None
    projection_source_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CompletedDagSourceKind):
            raise TypeError("completed DAG source kind must be canonical")
        for value in (self.source_id, self.parent_session_id, self.dag_id):
            _safe_identifier(value, field_name="completed DAG source identity", limit=512)
        if type(self.dag_generation) is not int or self.dag_generation < 0:
            raise ValueError("completed DAG generation must be non-negative")
        fingerprint(self.dag_definition_fingerprint)
        if self.kind is CompletedDagSourceKind.WORKFLOW:
            if not isinstance(self.workflow, WorkflowAdoptionSourceRef):
                raise TypeError("Workflow source requires an exact projection reference")
            if self.source_id != self.workflow.projection_id:
                raise ValueError("Workflow source identity must be its projection identity")
            if self.projection_source_fingerprint is None:
                raise ValueError("Workflow source digest is required")
            fingerprint(self.projection_source_fingerprint)
        elif self.workflow is not None or self.projection_source_fingerprint is not None:
            raise ValueError("Swarm source cannot carry Workflow provenance")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> CompletedDagAdoptionSource:
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "source_id",
            "parent_session_id",
            "dag_id",
            "dag_generation",
            "dag_definition_fingerprint",
            "workflow",
            "projection_source_fingerprint",
        }:
            raise ValueError("completed DAG source fields are invalid")
        workflow = raw["workflow"]
        ref = None
        if workflow is not None:
            if not isinstance(workflow, dict) or set(workflow) != {
                "run_id",
                "expansion_id",
                "projection_id",
                "projection_fingerprint",
            }:
                raise ValueError("Workflow source reference fields are invalid")
            ref = WorkflowAdoptionSourceRef(**workflow)
        return cls(
            kind=CompletedDagSourceKind(raw["kind"]),
            source_id=raw["source_id"],
            parent_session_id=raw["parent_session_id"],
            dag_id=raw["dag_id"],
            dag_generation=raw["dag_generation"],
            dag_definition_fingerprint=raw["dag_definition_fingerprint"],
            workflow=ref,
            projection_source_fingerprint=raw["projection_source_fingerprint"],
        )
