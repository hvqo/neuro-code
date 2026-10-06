"""DW3 canonical publication intent; no expansion interpreter or execution authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from neuro_code.domain.task_dag import MAX_TASK_DAG_NODES, TaskDag, TaskDagNode, TaskDagState
from neuro_code.domain.workflows.state import (
    StepIdentity,
    WorkflowRun,
    bounded_text,
    fingerprint,
    identifier,
)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExpansionMember:
    """One frozen task binding. A Map item may contain several declared tasks."""

    member_key: str
    task_id: str
    node_id: str
    input_fingerprint: str

    def __post_init__(self) -> None:
        bounded_text(self.member_key)
        identifier(self.task_id)
        bounded_text(self.node_id)
        fingerprint(self.input_fingerprint)


def freeze_members(members: tuple[ExpansionMember, ...]) -> tuple[ExpansionMember, ...]:
    if type(members) is not tuple or not 1 <= len(members) <= MAX_TASK_DAG_NODES:
        raise ValueError("publication members must be an immutable bounded tuple")
    if not all(isinstance(m, ExpansionMember) for m in members):
        raise ValueError("publication member must be canonical")
    if len({(m.member_key, m.task_id) for m in members}) != len(members):
        raise ValueError("duplicate member/task identity")
    if len({m.node_id for m in members}) != len(members):
        raise ValueError("duplicate member node identity")
    return tuple(sorted(members, key=lambda m: (m.member_key, m.task_id)))


@dataclass(frozen=True, slots=True)
class WorkflowExpansionIntent:
    expansion_id: str
    run_id: str
    step: StepIdentity
    input_fingerprint: str
    members: tuple[ExpansionMember, ...]
    dag: TaskDag

    def __post_init__(self) -> None:
        identifier(self.expansion_id)
        identifier(self.run_id)
        fingerprint(self.input_fingerprint)
        if not isinstance(self.step, StepIdentity) or not isinstance(self.dag, TaskDag):
            raise ValueError("publication requires canonical step and DAG")
        object.__setattr__(self, "members", freeze_members(self.members))
        if tuple(m.node_id for m in self.members) != tuple(n.node_id for n in self.dag.nodes):
            raise ValueError("DAG declaration order must match canonical member order")
        if self.dag.created_at is None:
            raise ValueError("publication DAG requires creation time")
        # Rebuild the fresh projection to reject *all* execution/result metadata.
        fresh = TaskDag.create(
            dag_id=self.dag.dag_id,
            parent_session_id=self.dag.parent_session_id,
            nodes=tuple(
                TaskDagNode(n.node_id, n.ordinal, n.prompt, n.dependencies, n.kind)
                for n in self.dag.nodes
            ),
            created_at=self.dag.created_at,
            max_parallel=self.dag.max_parallel,
        )
        if self.dag != fresh or self.dag.state is not TaskDagState.READY:
            raise ValueError("publication DAG must be a fresh immutable definition")

    @property
    def member_fingerprint(self) -> str:
        return digest([asdict(m) for m in self.members])

    @property
    def identity_fingerprint(self) -> str:
        return digest(
            {
                "run_id": self.run_id,
                "step": asdict(self.step),
                "input_fingerprint": self.input_fingerprint,
                "member_fingerprint": self.member_fingerprint,
                "dag_definition_fingerprint": self.dag.definition_fingerprint,
            }
        )

    @property
    def payload(self) -> dict[str, object]:
        assert self.dag.created_at is not None
        return {
            "expansion_id": self.expansion_id,
            "run_id": self.run_id,
            "step": asdict(self.step),
            "input_fingerprint": self.input_fingerprint,
            "members": [asdict(m) for m in self.members],
            "member_fingerprint": self.member_fingerprint,
            "identity_fingerprint": self.identity_fingerprint,
            "dag_id": self.dag.dag_id,
            "parent_session_id": self.dag.parent_session_id,
            "dag_definition_fingerprint": self.dag.definition_fingerprint,
            "nodes": [n.definition_payload for n in self.dag.nodes],
            "max_parallel": self.dag.max_parallel,
            "dag_created_at": self.dag.created_at.astimezone(UTC).isoformat(),
        }

    @property
    def canonical_json(self) -> str:
        return canonical(self.payload)


@dataclass(frozen=True, slots=True)
class WorkflowExpansion:
    expansion_id: str
    run_id: str
    step: StepIdentity
    input_fingerprint: str
    members: tuple[ExpansionMember, ...]
    member_fingerprint: str
    identity_fingerprint: str
    dag_id: str
    dag_definition_fingerprint: str
    generated_tasks: int
    created_generation: int
    created_at: datetime

    @property
    def reservation_id(self) -> str:
        return publication_request_id(self.expansion_id)


def publication_request_id(expansion_id: str) -> str:
    identifier(expansion_id)
    return "publication:" + hashlib.sha256(expansion_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowPublicationResult:
    expansion: WorkflowExpansion
    dag: TaskDag
    run: WorkflowRun
    replayed: bool = False
