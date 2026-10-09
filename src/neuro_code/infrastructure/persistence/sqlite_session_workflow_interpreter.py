"""DW5a immutable typed inputs and atomic step result/linkage facts in the existing DB."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.task_dag import TaskDagState
from neuro_code.domain.workflows.activity import WorkflowActivityState
from neuro_code.domain.workflows.definition import Activity, Map, TaskBatch
from neuro_code.domain.workflows.interpreter import (
    OutputKind,
    WorkflowStepOutput,
    expansion_id,
    invocation_id,
    typed_json,
    validate_step_output,
)
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    StepIdentity,
    WorkflowEventKind,
    WorkflowRun,
    WorkflowStatus,
    WorkflowWriteResult,
    identifier,
    integer,
    timestamp,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_activity import _load_attempt
from neuro_code.infrastructure.persistence.sqlite_session_workflow_projection import (
    _project_facts,
    _read_projection,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflow_publication import (
    _load_publication,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _append_event,
    _exceeds_ceiling,
    _guard,
    _load_definition,
    _load_run,
    _save_run,
    _validate_position,
)
from neuro_code.shared.async_utils import run_blocking


class WorkflowInterpreterMixin(_SqliteSessionPersistenceContext):
    async def put_workflow_input(self, run_id: str, input_json: str) -> None:
        identifier(run_id)

        def write() -> None:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                run = _required_run(connection, run_id)
                _input_value(connection, run, input_json)
                old = connection.execute(
                    "SELECT input_json FROM workflow_run_inputs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if old is not None:
                    if old[0] != input_json:
                        raise WorkflowStateError("input identity conflict", kind="conflict")
                    return
                if run.generation != 0 or run.steps:
                    raise WorkflowStateError(
                        "input must be frozen before ownership claim", kind="protocol"
                    )
                connection.execute(
                    "INSERT INTO workflow_run_inputs VALUES (?, ?, ?)",
                    (run_id, input_json, run.input_fingerprint),
                )

        async with self._write_lock:
            await run_blocking(lambda: _guard(write))

    async def get_workflow_input(self, run_id: str) -> str:
        identifier(run_id)

        def read() -> str:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                run = _required_run(connection, run_id)
                row = connection.execute(
                    "SELECT input_json, input_fingerprint FROM workflow_run_inputs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise WorkflowStateError("durable input is missing", kind="missing")
                _input_value(connection, run, row[0])
                if row[1] != run.input_fingerprint:
                    raise WorkflowStateError("input index differs from snapshot", kind="integrity")
                return str(row[0])

        return await run_blocking(lambda: _guard(read))

    async def get_workflow_step_output(
        self, run_id: str, step: StepIdentity
    ) -> WorkflowStepOutput | None:
        identifier(run_id)

        def read() -> WorkflowStepOutput | None:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return _load_output(connection, _required_run(connection, run_id), step)

        return await run_blocking(lambda: _guard(read))

    async def commit_workflow_step_output(
        self,
        output: WorkflowStepOutput,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult:
        integer(expected_generation)
        integer(owner_fence)
        identifier(owner_id)
        timestamp(updated_at)

        real_verify = False
        if output.kind is OutputKind.ACTIVITY:
            from neuro_code.infrastructure.persistence.sqlite_session_workflow_verification_scope import (
                owns_fresh_consumption,
                revalidate_consumption,
            )

            def is_real_verify() -> bool:
                with closing(self._connect()) as connection:
                    return (
                        connection.execute(
                            "SELECT 1 FROM workflow_verification_executions WHERE invocation_id=?",
                            (output.source_id,),
                        ).fetchone()
                        is not None
                    )

            real_verify = await run_blocking(is_real_verify)
            if real_verify and not owns_fresh_consumption(self._database_path, output):
                raise WorkflowStateError(
                    "real VERIFY output needs current workspace evidence", kind="stale_verification"
                )

        def write() -> WorkflowWriteResult:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                run = _required_run(connection, output.run_id)
                # Replay is never proof of current ownership.
                if (run.generation, run.owner_id, run.owner_fence) != (
                    expected_generation,
                    owner_id,
                    owner_fence,
                ):
                    raise WorkflowStateError(
                        "result owner/generation is stale", kind="concurrent_modification"
                    )
                previous = _load_output(connection, run, output.step)
                if previous is not None:
                    if previous != output:
                        raise WorkflowStateError("result payload conflict", kind="conflict")
                    return WorkflowWriteResult(run, run.generation, replayed=True)
                if (
                    run.status not in {WorkflowStatus.RUNNING, WorkflowStatus.WAITING}
                    or updated_at < run.updated_at
                ):
                    raise WorkflowStateError("run cannot consume result", kind="protocol")
                if output.kind is OutputKind.ACTIVITY and (
                    not run.ledger.committed.known
                    or _exceeds_ceiling(run.ledger)
                    or run.waiting_reason != "activity:" + output.source_id
                ):
                    raise WorkflowStateError(
                        "activity consumption budget/waiting differs", kind="needs_attention"
                    )
                _validate_output(connection, run, output)
                old = next((s for s in run.steps if s.identity == output.step), None)
                expected_status = (
                    WorkflowStatus.WAITING
                    if output.kind in {OutputKind.PROJECTION, OutputKind.ACTIVITY}
                    else WorkflowStatus.READY
                )
                if (
                    old is None
                    or old.status is not expected_status
                    or old.input_fingerprint != output.input_fingerprint
                ):
                    raise WorkflowStateError("result step state/input differs", kind="conflict")
                completed = replace(old, status=WorkflowStatus.COMPLETED, waiting_reason=None)
                proposed = replace(
                    run,
                    steps=tuple(
                        sorted(
                            (*(s for s in run.steps if s.identity != output.step), completed),
                            key=lambda s: s.identity.key,
                        )
                    ),
                    generation=run.generation + 1,
                    updated_at=updated_at,
                    status=WorkflowStatus.RUNNING,
                    waiting_reason=None,
                    failure=None,
                    position=output.step,
                )
                _save_run(connection, proposed, expected=run)
                # Journal retains only linkage; potentially large worker response stays in DW4a.
                fact = canonical(
                    {
                        "operation": "consume_typed_output",
                        "step_key": output.step.key,
                        "output_fingerprint": output.fingerprint,
                    }
                )
                _append_event(
                    connection,
                    proposed,
                    "output:" + output.step.key,
                    WorkflowEventKind.ACTIVITY
                    if output.kind is OutputKind.ACTIVITY
                    else WorkflowEventKind.STEP,
                    fact,
                )
                connection.execute(
                    "INSERT INTO workflow_step_outputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.run_id,
                        output.step.key,
                        output.kind.value,
                        output.source_id,
                        output.source_fingerprint,
                        output.input_fingerprint,
                        None if output.kind is OutputKind.PROJECTION else output.output_json,
                        output.fingerprint,
                        proposed.generation,
                    ),
                )
                return WorkflowWriteResult(proposed, proposed.generation)

        async with self._write_lock:
            if real_verify:
                await revalidate_consumption(self._database_path, output)
            return await run_blocking(lambda: _guard(write))


def _required_run(connection: sqlite3.Connection, run_id: str) -> WorkflowRun:
    run = _load_run(connection, run_id)
    if run is None:
        raise WorkflowStateError("run is missing", kind="missing")
    return run


def _input_value(connection: sqlite3.Connection, run: WorkflowRun, value: str) -> None:
    definition = _load_definition(connection, run.definition_fingerprint)
    if (
        definition is None
        or typed_json(definition.input_schema, json.loads(value)) != value
        or digest(json.loads(value)) != run.input_fingerprint
    ):
        raise WorkflowStateError("input fingerprint/schema mismatch", kind="integrity")


def _validate_output(
    connection: sqlite3.Connection, run: WorkflowRun, output: WorkflowStepOutput
) -> None:
    definition = _load_definition(connection, run.definition_fingerprint)
    if definition is None:
        raise WorkflowStateError("definition missing", kind="integrity")
    declared = _validate_position(definition, output.step)
    if not isinstance(declared, Activity | Map | TaskBatch):
        raise WorkflowStateError("step cannot produce this output", kind="protocol")
    validate_step_output(declared, output)
    if output.kind is OutputKind.ACTIVITY:
        if not isinstance(declared, Activity):
            raise WorkflowStateError("activity output requires Activity step", kind="integrity")
        attempt = _load_attempt(connection, output.source_id)
        if (
            attempt is None
            or attempt.state is not WorkflowActivityState.COMPLETED
            or attempt.result is None
            or attempt.invocation.run_id != run.run_id
            or attempt.invocation.step != output.step
            or attempt.invocation.input_fingerprint != output.input_fingerprint
            or attempt.invocation.activity is not declared.activity
            or attempt.result.fingerprint != output.source_fingerprint
            or attempt.result.output_json != output.output_json
        ):
            raise WorkflowStateError("durable activity result linkage mismatch", kind="integrity")
    elif output.kind is OutputKind.PROJECTION:
        publication = _load_publication(connection, expansion_id(run.run_id, output.step))
        if publication is None or publication.dag.state is not TaskDagState.COMPLETED:
            raise WorkflowStateError("exact completed publication missing", kind="integrity")
        source, value = _project_facts(connection, publication)
        projection = _read_projection(connection, publication, source, value)
        if projection is None or (
            output.source_id,
            output.source_fingerprint,
            output.output_json,
            output.input_fingerprint,
        ) != (
            projection.projection_id,
            projection.fingerprint,
            projection.output_json,
            publication.expansion.input_fingerprint,
        ):
            raise WorkflowStateError("exact projection linkage mismatch", kind="integrity")
    else:
        expected_id = (
            invocation_id(run.run_id, output.step, output.input_fingerprint)
            if output.kind is OutputKind.FAKE_ACTIVITY
            else "empty:" + output.step.key
        )
        if output.source_id != expected_id or output.source_fingerprint != digest(
            [expected_id, output.input_fingerprint]
        ):
            raise WorkflowStateError("local output identity mismatch", kind="integrity")


def _load_output(
    connection: sqlite3.Connection, run: WorkflowRun, step: StepIdentity
) -> WorkflowStepOutput | None:
    row = connection.execute(
        "SELECT kind, source_id, source_fingerprint, input_fingerprint, output_json, output_fingerprint, generation FROM workflow_step_outputs WHERE run_id = ? AND step_key = ?",
        (run.run_id, step.key),
    ).fetchone()
    if row is None:
        return None
    kind = OutputKind(row[0])
    value = row[4]
    if kind is OutputKind.PROJECTION:
        if value is not None:
            raise WorkflowStateError("projection link contains duplicate result", kind="integrity")
        publication = _load_publication(connection, expansion_id(run.run_id, step))
        if publication is None:
            raise WorkflowStateError("projection publication missing", kind="integrity")
        source, expected_output = _project_facts(connection, publication)
        projection = _read_projection(connection, publication, source, expected_output)
        if projection is None:
            raise WorkflowStateError("consumed projection missing", kind="integrity")
        value = projection.output_json
    result = WorkflowStepOutput(run.run_id, step, row[3], kind, row[1], row[2], value)
    linked = next((s for s in run.steps if s.identity == step), None)
    event = connection.execute(
        "SELECT kind, payload_json FROM workflow_transition_journal WHERE run_id = ? AND generation = ?",
        (run.run_id, row[6]),
    ).fetchone()
    fact = canonical(
        {
            "operation": "consume_typed_output",
            "step_key": step.key,
            "output_fingerprint": result.fingerprint,
        }
    )
    if (
        result.fingerprint != row[5]
        or linked is None
        or linked.status is not WorkflowStatus.COMPLETED
        or linked.input_fingerprint != result.input_fingerprint
        or event
        != (
            (
                WorkflowEventKind.ACTIVITY
                if kind is OutputKind.ACTIVITY
                else WorkflowEventKind.STEP
            ).value,
            fact,
        )
        or row[6] > run.generation
    ):
        raise WorkflowStateError("output snapshot/journal linkage mismatch", kind="integrity")
    _validate_output(connection, run, result)
    return result
