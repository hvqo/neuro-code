"""DW3 transaction owner: publish one prepared DAG and its durable Workflow linkage."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.definition import Map, TaskBatch, WorkflowDefinition
from neuro_code.domain.workflows.publication import (
    ExpansionMember,
    WorkflowExpansion,
    WorkflowExpansionIntent,
    WorkflowPublicationResult,
    canonical,
    digest,
    freeze_members,
    publication_request_id,
)
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    StepIdentity,
    WorkflowChange,
    WorkflowEventKind,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepInstance,
    identifier,
    integer,
    timestamp,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_dag import (
    _insert_task_dag,
    _load_task_dag,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _append_event,
    _apply_change,
    _digest,
    _guard,
    _load_definition,
    _load_run,
    _save_run,
    _validate_position,
)
from neuro_code.shared.async_utils import run_blocking


class WorkflowPublicationMixin(_SqliteSessionPersistenceContext):
    async def publish_workflow_expansion(
        self,
        intent: WorkflowExpansionIntent,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowPublicationResult:
        if not isinstance(intent, WorkflowExpansionIntent):
            raise TypeError("publication intent must be canonical")
        integer(expected_generation)
        identifier(owner_id)
        integer(owner_fence)
        timestamp(updated_at)

        def publish() -> WorkflowPublicationResult:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = _load_publication(connection, intent.expansion_id)
                if previous is not None:
                    stored = connection.execute(
                        "SELECT canonical_intent FROM workflow_expansions WHERE expansion_id = ?",
                        (intent.expansion_id,),
                    ).fetchone()
                    if stored is None or stored[0] != intent.canonical_json:
                        raise WorkflowStateError(
                            "expansion identity payload conflict", kind="conflict"
                        )
                    return replace(previous, replayed=True)
                current = _load_run(connection, intent.run_id)
                if current is None:
                    raise WorkflowStateError("workflow run is missing", kind="missing")
                if (
                    current.generation != expected_generation
                    or current.owner_id != owner_id
                    or current.owner_fence != owner_fence
                ):
                    raise WorkflowStateError(
                        "publication owner/generation is stale", kind="concurrent_modification"
                    )
                if current.status not in {WorkflowStatus.RUNNING, WorkflowStatus.WAITING}:
                    raise WorkflowStateError(
                        "run cannot publish work in current state", kind="protocol"
                    )
                if (
                    updated_at < current.updated_at
                    or intent.dag.created_at is None
                    or intent.dag.created_at > updated_at
                ):
                    raise WorkflowStateError("publication time is invalid", kind="protocol")
                if intent.dag.parent_session_id != current.parent_session_id:
                    raise WorkflowStateError("DAG parent does not match run", kind="protocol")
                definition = _load_definition(connection, current.definition_fingerprint)
                if definition is None:
                    raise WorkflowStateError("definition is missing", kind="integrity")
                _validate_members(definition, intent)
                if (
                    connection.execute(
                        "SELECT 1 FROM workflow_expansions WHERE run_id = ? AND step_key = ?",
                        (intent.run_id, intent.step.key),
                    ).fetchone()
                    is not None
                ):
                    raise WorkflowStateError("step instance already published", kind="conflict")
                if _load_task_dag(connection, intent.dag.dag_id) is not None:
                    raise WorkflowStateError(
                        "publication requires a new DAG identity", kind="conflict"
                    )
                request_id = publication_request_id(intent.expansion_id)
                if (
                    connection.execute(
                        "SELECT 1 FROM workflow_transition_journal WHERE run_id = ? AND request_id = ?",
                        (intent.run_id, request_id),
                    ).fetchone()
                    is not None
                ):
                    raise WorkflowStateError(
                        "publication request identity already used", kind="conflict"
                    )
                old_step = next((s for s in current.steps if s.identity == intent.step), None)
                if old_step is not None and (
                    old_step.status is not WorkflowStatus.READY
                    or old_step.input_fingerprint != intent.input_fingerprint
                ):
                    raise WorkflowStateError(
                        "publication step input/state conflict", kind="conflict"
                    )
                amounts = BudgetAmounts(generated_tasks=len(intent.dag.nodes))
                proposed = _apply_change(
                    connection,
                    current,
                    definition,
                    WorkflowChange(
                        WorkflowEventKind.RESERVED, reservation_id=request_id, amounts=amounts
                    ),
                    updated_at,
                )
                proposed = _apply_change(
                    connection,
                    proposed,
                    definition,
                    WorkflowChange(
                        WorkflowEventKind.CONSUMED, reservation_id=request_id, amounts=amounts
                    ),
                    updated_at,
                )
                waiting = WorkflowStepInstance(
                    intent.step,
                    WorkflowStatus.WAITING,
                    intent.input_fingerprint,
                    waiting_reason="Published Task DAG " + intent.dag.dag_id,
                )
                steps = tuple(
                    sorted(
                        (*(s for s in proposed.steps if s.identity != intent.step), waiting),
                        key=lambda s: s.identity.key,
                    )
                )
                proposed = replace(
                    proposed,
                    status=WorkflowStatus.WAITING,
                    position=intent.step,
                    steps=steps,
                    waiting_reason="Published Task DAG " + intent.dag.dag_id,
                    failure=None,
                    generation=current.generation + 1,
                    updated_at=updated_at,
                )
                # Every operation below uses this connection. None opens/commits
                # another transaction or calls the public TaskDagStore API.
                _insert_task_dag(connection, intent.dag)
                _save_run(connection, proposed, expected=current)
                _insert_expansion(connection, intent, proposed)
                _append_event(
                    connection,
                    proposed,
                    request_id,
                    WorkflowEventKind.PUBLISHED,
                    _publication_fact(intent.payload, len(intent.dag.nodes)),
                )
                result = _load_publication(connection, intent.expansion_id)
                if result is None:
                    raise WorkflowStateError("publication disappeared", kind="integrity")
                return result

        async with self._write_lock:
            return await run_blocking(lambda: _guard(publish))

    async def get_workflow_expansion(self, expansion_id: str) -> WorkflowExpansion | None:
        identifier(expansion_id)

        def read() -> WorkflowExpansion | None:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                result = _load_publication(connection, expansion_id)
                return None if result is None else result.expansion

        return await run_blocking(lambda: _guard(read))


def _validate_members(definition: WorkflowDefinition, intent: WorkflowExpansionIntent) -> None:
    declared = _validate_position(definition, intent.step)
    if isinstance(declared, Map):
        batch = declared.batch
        keys = {m.member_key for m in intent.members}
        if len(keys) > declared.max_items:
            raise WorkflowStateError("Map member bound exceeded", kind="bounds")
    elif isinstance(declared, TaskBatch):
        batch = declared
        keys = {"batch"}
        if any(m.member_key != "batch" for m in intent.members):
            raise WorkflowStateError(
                "TaskBatch members require the batch identity", kind="protocol"
            )
    else:
        raise WorkflowStateError("only TaskBatch/Map intents publish DAGs", kind="protocol")
    if intent.dag.max_parallel > batch.max_parallel:
        raise WorkflowStateError("DAG cannot widen declared parallelism", kind="protocol")
    templates = {t.task_id: t for t in batch.tasks}
    bindings = {(m.member_key, m.task_id): m.node_id for m in intent.members}
    if set(bindings) != {(key, task_id) for key in keys for task_id in templates}:
        raise WorkflowStateError("member set does not cover declared tasks", kind="protocol")
    for member in intent.members:
        node = intent.dag.node(member.node_id)
        template = templates[member.task_id]
        dependencies = {bindings[(member.member_key, d)] for d in template.depends_on}
        if node.kind is not template.route or set(node.dependencies) != dependencies:
            raise WorkflowStateError(
                "DAG route/dependencies differ from declaration", kind="protocol"
            )


def _insert_expansion(
    connection: sqlite3.Connection, intent: WorkflowExpansionIntent, run: WorkflowRun
) -> None:
    connection.execute(
        "INSERT INTO workflow_expansions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent.expansion_id,
            intent.run_id,
            intent.step.key,
            intent.dag.dag_id,
            intent.identity_fingerprint,
            _digest(intent.canonical_json),
            intent.canonical_json,
            len(intent.dag.nodes),
            run.generation,
            run.updated_at.astimezone(UTC).isoformat(),
        ),
    )


def _load_publication(
    connection: sqlite3.Connection, expansion_id: str
) -> WorkflowPublicationResult | None:
    row = connection.execute(
        "SELECT run_id, step_key, dag_id, identity_fingerprint, payload_fingerprint, canonical_intent, generated_tasks, created_generation, created_at FROM workflow_expansions WHERE expansion_id = ?",
        (expansion_id,),
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row[5])
    step = StepIdentity(**data["step"])
    members = freeze_members(tuple(ExpansionMember(**m) for m in data["members"]))
    member_fp = digest([asdict(m) for m in members])
    identity_fp = digest(
        {
            "run_id": data["run_id"],
            "step": asdict(step),
            "input_fingerprint": data["input_fingerprint"],
            "member_fingerprint": member_fp,
            "dag_definition_fingerprint": data["dag_definition_fingerprint"],
        }
    )
    if (
        canonical(data) != row[5]
        or _digest(row[5]) != row[4]
        or data["expansion_id"] != expansion_id
        or (data["run_id"], step.key, data["dag_id"], identity_fp) != row[:4]
        or member_fp != data["member_fingerprint"]
        or identity_fp != data["identity_fingerprint"]
    ):
        raise WorkflowStateError("expansion integrity mismatch", kind="integrity")
    run = _load_run(connection, row[0])
    dag = _load_task_dag(connection, row[2])
    request_id = publication_request_id(expansion_id)
    event = connection.execute(
        "SELECT request_id, kind, payload_fingerprint, payload_json, created_at FROM workflow_transition_journal WHERE run_id = ? AND generation = ?",
        (row[0], row[7]),
    ).fetchone()
    journal_payload = _publication_fact(data, row[6])
    created_at = datetime.fromisoformat(row[8])
    expected_amounts = BudgetAmounts(generated_tasks=row[6])
    linked_step = None if run is None else next((s for s in run.steps if s.identity == step), None)
    reservation = (
        None
        if run is None
        else next((r for r in run.ledger.reservations if r.reservation_id == request_id), None)
    )
    if (
        run is None
        or dag is None
        or run.generation < row[7]
        or dag.parent_session_id != run.parent_session_id
        or data["parent_session_id"] != run.parent_session_id
        or dag.definition_fingerprint != data["dag_definition_fingerprint"]
        or [n.definition_payload for n in dag.nodes] != data["nodes"]
        or dag.max_parallel != data["max_parallel"]
        or dag.created_at != datetime.fromisoformat(data["dag_created_at"])
        or len(dag.nodes) != row[6]
        or tuple(m.node_id for m in members) != tuple(n.node_id for n in dag.nodes)
        or linked_step is None
        or linked_step.input_fingerprint != data["input_fingerprint"]
        or reservation is None
        or reservation.reserved != expected_amounts
        or reservation.consumed != expected_amounts
        or reservation.created_at != created_at
        or event
        != (
            request_id,
            WorkflowEventKind.PUBLISHED.value,
            _digest(journal_payload),
            journal_payload,
            row[8],
        )
    ):
        raise WorkflowStateError(
            "expansion DAG/run/budget/journal linkage mismatch", kind="integrity"
        )
    expansion = WorkflowExpansion(
        expansion_id,
        run.run_id,
        step,
        data["input_fingerprint"],
        members,
        member_fp,
        identity_fp,
        dag.dag_id,
        dag.definition_fingerprint,
        row[6],
        row[7],
        created_at,
    )
    return WorkflowPublicationResult(expansion, dag, run)


def _publication_fact(data: dict[str, object], generated_tasks: int) -> str:
    # The journal records bounded linkage facts, not another copy of 8 prompts.
    return canonical(
        {
            **{
                key: data[key]
                for key in (
                    "expansion_id",
                    "run_id",
                    "step",
                    "input_fingerprint",
                    "member_fingerprint",
                    "identity_fingerprint",
                    "dag_id",
                    "dag_definition_fingerprint",
                )
            },
            "generated_tasks": generated_tasks,
            "intent_fingerprint": digest(data),
        }
    )
