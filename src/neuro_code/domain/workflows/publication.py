"""DW3 canonical publication intent; no expansion interpreter or execution authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from neuro_code.domain.agents.profile import AgentCapability
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
    # None only represents legacy, non-executable publication facts.
    profile_ref: str | None = None
    required_capabilities: tuple[AgentCapability, ...] = ()

    def __post_init__(self) -> None:
        bounded_text(self.member_key)
        identifier(self.task_id)
        bounded_text(self.node_id)
        fingerprint(self.input_fingerprint)
        if self.profile_ref is not None:
            identifier(self.profile_ref)
        if (
            type(self.required_capabilities) is not tuple
            or not all(isinstance(c, AgentCapability) for c in self.required_capabilities)
            or len(set(self.required_capabilities)) != len(self.required_capabilities)
        ):
            raise ValueError("execution capabilities must be a unique immutable typed tuple")
        if self.profile_ref is None and self.required_capabilities:
            raise ValueError("execution capabilities require an exact profile")
        object.__setattr__(self, "required_capabilities", tuple(sorted(self.required_capabilities)))

    @property
    def payload(self) -> dict[str, object]:
        data: dict[str, object] = {
            "member_key": self.member_key,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "input_fingerprint": self.input_fingerprint,
        }
        if self.profile_ref is not None:
            data.update(
                profile_ref=self.profile_ref,
                required_capabilities=[c.value for c in self.required_capabilities],
            )
        return data


@dataclass(frozen=True, slots=True)
class WorkflowNodeExecutionIntent:
    """Exact durable provenance and intent; never a permission/capability grant."""

    run_id: str
    expansion_id: str
    dag_id: str
    step: StepIdentity
    member: ExpansionMember
    parent_session_id: str

    def __post_init__(self) -> None:
        identifier(self.run_id)
        identifier(self.expansion_id)
        bounded_text(self.dag_id)
        bounded_text(self.parent_session_id)
        if not isinstance(self.step, StepIdentity) or not isinstance(self.member, ExpansionMember):
            raise ValueError("execution intent requires canonical step/member")
        if self.member.profile_ref is None:
            raise ValueError("Workflow node has no durable execution intent")

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "run_id": self.run_id,
                "expansion_id": self.expansion_id,
                "dag_id": self.dag_id,
                "step": asdict(self.step),
                "member": self.member.payload,
                "parent_session_id": self.parent_session_id,
            }
        )


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
        return digest([m.payload for m in self.members])

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
            "members": [m.payload for m in self.members],
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
