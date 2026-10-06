"""Exact result capture shares the existing terminal-node CAS transaction."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from neuro_code.application.ports.task_dag import TaskDagError
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.task_dag import TaskDagNode
from neuro_code.domain.task_dag_result import TaskDagResultEvidence
from neuro_code.domain.workflows.publication import canonical, digest


def persist_result_evidence(
    connection: sqlite3.Connection,
    dag_id: str,
    node: TaskDagNode,
    evidence: TaskDagResultEvidence | None,
) -> None:
    if evidence is not None and (
        not isinstance(evidence, TaskDagResultEvidence)
        or (evidence.parent_task_id, evidence.child_session_id)
        != (node.parent_task_id, node.child_session_id)
    ):
        raise TaskDagError("exact result worker identity mismatch", kind="protocol")
    # Only DW3-bound DAGs need Workflow exact-result retention. Ordinary DAGs
    # retain their existing lifecycle, including session-owned record cleanup.
    if (
        connection.execute(
            "SELECT 1 FROM workflow_expansions WHERE dag_id = ?", (dag_id,)
        ).fetchone()
        is None
    ):
        return
    payload = canonical(None if evidence is None else asdict(evidence))
    connection.execute(
        "INSERT INTO task_dag_result_evidence VALUES (?, ?, ?, ?, ?, ?)",
        (
            dag_id,
            node.node_id,
            node.generation,
            digest(asdict(node)),
            payload,
            digest(json.loads(payload)),
        ),
    )


def load_result_evidence(
    connection: sqlite3.Connection, dag_id: str, node: TaskDagNode
) -> tuple[TaskDagResultEvidence | None, str | None]:
    row = connection.execute(
        "SELECT node_generation, node_fingerprint, payload_json, payload_fingerprint FROM task_dag_result_evidence WHERE dag_id = ? AND node_id = ?",
        (dag_id, node.node_id),
    ).fetchone()
    if row is None:
        return None, None
    value = json.loads(row[2])
    evidence = None if value is None else TaskDagResultEvidence(**value)
    if (
        row[0] != node.generation
        or row[1] != digest(asdict(node))
        or canonical(value) != row[2]
        or digest(value) != row[3]
        or (
            evidence is not None
            and (evidence.parent_task_id, evidence.child_session_id)
            != (node.parent_task_id, node.child_session_id)
        )
    ):
        raise WorkflowStateError("terminal result evidence mismatch", kind="integrity")
    return evidence, row[3]
