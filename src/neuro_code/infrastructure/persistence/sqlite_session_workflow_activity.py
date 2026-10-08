"""Atomic Activity facts in the existing Workflow session database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import (
    WorkflowActivityAttempt,
    WorkflowActivityInvocation,
    WorkflowActivityResult,
    WorkflowActivityState,
    validate_activity_budget,
)
from neuro_code.domain.workflows.definition import (
    Activity,
    ActivityKind,
    ArtifactRef,
    Literal,
    WorkflowDefinition,
    activity_output_schema,
    activity_reference_schemas,
)
from neuro_code.domain.workflows.interpreter import typed_json
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    StepIdentity,
    WorkflowChange,
    WorkflowEventKind,
    WorkflowFailure,
    WorkflowRun,
    WorkflowStatus,
    WorkflowWriteResult,
    fingerprint,
    identifier,
    integer,
    timestamp,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
    _append_event,
    _apply_change,
    _exceeds_ceiling,
    _guard,
    _json,
    _load_definition,
    _load_run,
    _save_run,
    _validate_position,
)
from neuro_code.shared.async_utils import run_blocking


def _run(connection: sqlite3.Connection, run_id: str) -> WorkflowRun:
    run = _load_run(connection, run_id)
    if run is None:
        raise WorkflowStateError("activity run is missing", kind="missing")
    return run


def _invocation(data: dict[str, Any]) -> WorkflowActivityInvocation:
    step = data["step"]
    if not isinstance(step, dict):
        raise ValueError("activity step is invalid")
    return WorkflowActivityInvocation(
        data["invocation_id"],
        data["run_id"],
        data["definition_fingerprint"],
        data["parent_session_id"],
        StepIdentity(**step),
        ActivityKind(data["activity"]),
        data["request_json"],
        data["request_fingerprint"],
        data["input_fingerprint"],
        datetime.fromisoformat(data["created_at"]),
    )


def _result(data: dict[str, Any]) -> WorkflowActivityResult:
    usage = data["usage"]
    if not isinstance(usage, dict):
        raise ValueError("activity usage is invalid")
    return WorkflowActivityResult(
        data["invocation_id"],
        data["request_fingerprint"],
        ActivityKind(data["activity"]),
        WorkflowActivityState(data["state"]),
        data["source_id"],
        data["source_fingerprint"],
        BudgetAmounts(**usage),
        datetime.fromisoformat(data["terminal_at"]),
        data["output_json"],
    )


def _load_attempt(
    connection: sqlite3.Connection, invocation_id: str
) -> WorkflowActivityAttempt | None:
    row = connection.execute(
        "SELECT run_id, step_key, state, revision, snapshot_json, snapshot_fingerprint, created_generation "
        "FROM workflow_activity_attempts WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()
    if row is None:
        return None
    run = _run(connection, row[0])
    data = json.loads(row[4])
    if not isinstance(data, dict) or _json(data) != row[4] or digest(data) != row[5]:
        raise WorkflowStateError("activity snapshot fingerprint mismatch", kind="integrity")
    invocation = _invocation(data["invocation"])
    reserved = data["reserved"]
    attempt = WorkflowActivityAttempt(
        invocation,
        WorkflowActivityState(data["state"]),
        data["revision"],
        data["owner_id"],
        data["owner_fence"],
        data["reservation_id"],
        BudgetAmounts(**reserved) if reserved is not None else None,
        _result(data["result"]) if data["result"] is not None else None,
        datetime.fromisoformat(data["updated_at"]),
    )
    if (
        attempt.invocation.invocation_id != invocation_id
        or attempt.invocation.run_id != run.run_id
        or attempt.invocation.definition_fingerprint != run.definition_fingerprint
        or attempt.invocation.parent_session_id != run.parent_session_id
        or attempt.invocation.step.key != row[1]
        or attempt.state.value != row[2]
        or attempt.revision != row[3]
        or row[6] > run.generation
    ):
        raise WorkflowStateError("activity indexed identity differs", kind="integrity")
    step = next((s for s in run.steps if s.identity == attempt.invocation.step), None)
    if (
        step is None
        or step.input_fingerprint != invocation.input_fingerprint
        or step.status
        not in {
            WorkflowStatus.WAITING,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.NEEDS_ATTENTION,
        }
    ):
        raise WorkflowStateError("activity step linkage differs", kind="integrity")
    definition = _load_definition(connection, run.definition_fingerprint)
    if definition is None:
        raise WorkflowStateError("activity definition is missing", kind="integrity")
    declared = _validate_position(definition, invocation.step)
    if not isinstance(declared, Activity) or declared.activity is not invocation.activity:
        raise WorkflowStateError("activity kind differs from definition", kind="integrity")
    _validate_request(definition, declared, invocation)
    if attempt.result is not None and attempt.result.output_json is not None:
        typed_json(
            activity_output_schema(declared.activity), json.loads(attempt.result.output_json)
        )
    expected_publish = canonical(
        {
            "operation": "activity_published",
            "invocation_id": invocation_id,
            "invocation_fingerprint": invocation.fingerprint,
            "step_key": invocation.step.key,
        }
    )
    publication = connection.execute(
        "SELECT kind, payload_json FROM workflow_transition_journal "
        "WHERE run_id = ? AND generation = ?",
        (run.run_id, row[6]),
    ).fetchone()
    if publication != (WorkflowEventKind.ACTIVITY.value, expected_publish):
        raise WorkflowStateError("activity publication journal differs", kind="integrity")
    events = connection.execute(
        "SELECT revision, kind, payload_json, payload_fingerprint FROM workflow_activity_events "
        "WHERE invocation_id = ? ORDER BY revision",
        (invocation_id,),
    ).fetchall()
    if len(events) != attempt.revision + 1 or [event[0] for event in events] != list(
        range(attempt.revision + 1)
    ):
        raise WorkflowStateError("activity event continuity differs", kind="integrity")
    for revision, kind, payload, payload_fingerprint in events:
        fact = json.loads(payload)
        if (
            not isinstance(fact, dict)
            or canonical(fact) != payload
            or digest(fact) != payload_fingerprint
            or fact.get("revision") != revision
            or fact.get("state") != kind
        ):
            raise WorkflowStateError("activity event integrity mismatch", kind="integrity")
    states = [WorkflowActivityState.READY.value]
    if attempt.revision:
        states.append(WorkflowActivityState.CLAIMED.value)
    if attempt.revision >= 2:
        states.append(
            WorkflowActivityState.RUNNING.value
            if attempt.revision == 3 or not attempt.state.terminal
            else attempt.state.value
        )
    if attempt.revision == 3:
        states.append(attempt.state.value)
    if [event[1] for event in events] != states:
        raise WorkflowStateError("activity state progression differs", kind="integrity")
    assert attempt.updated_at is not None
    if (
        events[-1][1] != attempt.state.value
        or json.loads(events[-1][2]).get("snapshot_fingerprint") != row[5]
        or json.loads(events[-1][2]).get("occurred_at") != attempt.updated_at.isoformat()
    ):
        raise WorkflowStateError("activity event/snapshot differs", kind="integrity")
    result_row = connection.execute(
        "SELECT payload_json, payload_fingerprint FROM workflow_activity_results WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()
    if attempt.result is None:
        if result_row is not None:
            raise WorkflowStateError("nonterminal activity has result", kind="integrity")
    elif result_row != (_json(asdict(attempt.result)), attempt.result.fingerprint):
        raise WorkflowStateError("activity terminal result differs", kind="integrity")
    if attempt.reservation_id is not None:
        reservation = next(
            (r for r in run.ledger.reservations if r.reservation_id == attempt.reservation_id),
            None,
        )
        if (
            reservation is None
            or attempt.reservation_id != "activity-budget:" + invocation_id
            or reservation.reserved != attempt.reserved
            or reservation.created_at
            != datetime.fromisoformat(json.loads(events[1][2])["occurred_at"])
            or (attempt.result is None and reservation.consumed is not None)
            or (
                attempt.result is not None
                and (
                    reservation.consumed is None
                    or any(
                        old is not None and old != new
                        for old, new in zip(
                            attempt.result.usage.values, reservation.consumed.values, strict=True
                        )
                    )
                )
            )
        ):
            raise WorkflowStateError("activity budget linkage differs", kind="integrity")
    if (
        attempt.result is not None
        and declared.activity is ActivityKind.ADOPT
        and "source" in dict(declared.inputs)
    ):
        from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_facts import (
            _verify_terminal_adopt,
        )

        _verify_terminal_adopt(connection, attempt)
    return attempt


def _validate_request(
    definition: WorkflowDefinition, declared: Activity, invocation: WorkflowActivityInvocation
) -> None:
    value = json.loads(invocation.request_json)
    if not isinstance(value, dict) or set(value) != {name for name, _ in declared.inputs}:
        raise WorkflowStateError("activity request fields differ", kind="integrity")
    schemas = activity_reference_schemas(definition, declared.step_id)
    for name, binding in declared.inputs:
        if isinstance(binding, Literal):
            if type(value[name]) is not type(binding.value) or value[name] != binding.value:
                raise WorkflowStateError("activity literal input differs", kind="integrity")
        elif isinstance(binding, ArtifactRef):
            if value[name] != asdict(binding):
                raise WorkflowStateError("activity artifact input differs", kind="integrity")
        else:
            typed_json(schemas[name], value[name])


def _save_attempt(
    connection: sqlite3.Connection,
    attempt: WorkflowActivityAttempt,
    *,
    created_generation: int | None = None,
    expected_revision: int | None = None,
) -> None:
    payload = _json(asdict(attempt))
    value = digest(json.loads(payload))
    if created_generation is not None:
        connection.execute(
            "INSERT INTO workflow_activity_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.invocation.invocation_id,
                attempt.invocation.run_id,
                attempt.invocation.step.key,
                attempt.state.value,
                attempt.revision,
                payload,
                value,
                created_generation,
            ),
        )
    else:
        cursor = connection.execute(
            "UPDATE workflow_activity_attempts SET state = ?, revision = ?, snapshot_json = ?, "
            "snapshot_fingerprint = ? WHERE invocation_id = ? AND revision = ?",
            (
                attempt.state.value,
                attempt.revision,
                payload,
                value,
                attempt.invocation.invocation_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise WorkflowStateError("activity revision CAS failed", kind="concurrent_modification")
    event = canonical(
        {
            "revision": attempt.revision,
            "state": attempt.state.value,
            "snapshot_fingerprint": value,
            "occurred_at": attempt.updated_at.isoformat()
            if attempt.updated_at is not None
            else None,
        }
    )
    connection.execute(
        "INSERT INTO workflow_activity_events VALUES (?, ?, ?, ?, ?)",
        (
            attempt.invocation.invocation_id,
            attempt.revision,
            attempt.state.value,
            event,
            digest(json.loads(event)),
        ),
    )


def _budget_change(
    connection: sqlite3.Connection,
    run: WorkflowRun,
    change: WorkflowChange,
    updated_at: datetime,
    request_id: str,
) -> WorkflowRun:
    if updated_at < run.updated_at:
        raise WorkflowStateError("activity time precedes run", kind="protocol")
    assert change.amounts is not None
    definition = _load_definition(connection, run.definition_fingerprint)
    if definition is None:
        raise WorkflowStateError("activity definition is missing", kind="integrity")
    changed = _apply_change(connection, run, definition, change, updated_at)
    proposed = replace(changed, generation=run.generation + 1, updated_at=updated_at)
    _save_run(connection, proposed, expected=run)
    _append_event(
        connection,
        proposed,
        request_id,
        change.kind,
        canonical(
            {
                "operation": "activity_budget",
                "reservation_id": change.reservation_id,
                "amounts": asdict(change.amounts),
            }
        ),
    )
    return proposed


def _settle_terminal(
    connection: sqlite3.Connection,
    current: WorkflowActivityAttempt,
    result: WorkflowActivityResult,
) -> WorkflowActivityAttempt:
    """Shared atomic settlement, after caller-specific authority checks."""
    run = _run(connection, current.invocation.run_id)
    assert current.reservation_id is not None
    _budget_change(
        connection,
        run,
        WorkflowChange(
            WorkflowEventKind.CONSUMED,
            reservation_id=current.reservation_id,
            amounts=result.usage,
        ),
        result.terminal_at,
        "activity-settle:" + result.invocation_id,
    )
    terminal = replace(
        current,
        state=result.state,
        revision=current.revision + 1,
        result=result,
        updated_at=result.terminal_at,
    )
    _save_attempt(connection, terminal, expected_revision=current.revision)
    connection.execute(
        "INSERT INTO workflow_activity_results VALUES (?, ?, ?)",
        (result.invocation_id, _json(asdict(result)), result.fingerprint),
    )
    return terminal


def _start_activity(
    connection: sqlite3.Connection, current: WorkflowActivityAttempt, updated_at: datetime
) -> WorkflowActivityAttempt:
    run = _run(connection, current.invocation.run_id)
    if (
        run.status is not WorkflowStatus.WAITING
        or run.waiting_reason != "activity:" + current.invocation.invocation_id
        or updated_at < run.updated_at
        or updated_at < (current.updated_at or current.invocation.created_at)
    ):
        raise WorkflowStateError("activity cannot cross start boundary", kind="protocol")
    started = replace(
        current,
        state=WorkflowActivityState.RUNNING,
        revision=current.revision + 1,
        updated_at=updated_at,
    )
    _save_attempt(connection, started, expected_revision=current.revision)
    return started


class WorkflowActivityMixin(_SqliteSessionPersistenceContext):
    async def consume_workflow_activity_failure(
        self,
        invocation_id: str,
        result_fingerprint: str,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult:
        identifier(invocation_id)
        fingerprint(result_fingerprint)
        integer(expected_generation)
        identifier(owner_id)
        integer(owner_fence)
        timestamp(updated_at)

        def consume() -> WorkflowWriteResult:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                attempt = _load_attempt(connection, invocation_id)
                if (
                    attempt is None
                    or attempt.result is None
                    or attempt.state is WorkflowActivityState.COMPLETED
                    or attempt.result.fingerprint != result_fingerprint
                ):
                    raise WorkflowStateError("activity failure result differs", kind="integrity")
                run = _run(connection, attempt.invocation.run_id)
                if (run.generation, run.owner_id, run.owner_fence) != (
                    expected_generation,
                    owner_id,
                    owner_fence,
                ):
                    raise WorkflowStateError(
                        "activity consumer owner is stale", kind="concurrent_modification"
                    )
                fact = canonical(
                    {
                        "operation": "activity_result_consumed",
                        "invocation_id": invocation_id,
                        "result_fingerprint": result_fingerprint,
                        "state": attempt.state.value,
                    }
                )
                previous = connection.execute(
                    "SELECT payload_json FROM workflow_transition_journal WHERE run_id = ? AND request_id = ?",
                    (run.run_id, "activity-failure:" + invocation_id),
                ).fetchone()
                if previous is not None:
                    if previous[0] != fact:
                        raise WorkflowStateError("activity consumption conflict", kind="conflict")
                    return WorkflowWriteResult(run, run.generation, replayed=True)
                if (
                    run.status is not WorkflowStatus.WAITING
                    or run.waiting_reason != "activity:" + invocation_id
                    or updated_at < run.updated_at
                ):
                    raise WorkflowStateError("run cannot consume Activity failure", kind="protocol")
                status = (
                    WorkflowStatus.FAILED
                    if attempt.state is WorkflowActivityState.FAILED
                    else WorkflowStatus.NEEDS_ATTENTION
                )
                failure = WorkflowFailure(
                    "activity_" + attempt.state.value,
                    "Durable Activity ended without a completed result",
                )
                proposed = replace(
                    run,
                    status=status,
                    failure=failure,
                    waiting_reason=None,
                    steps=tuple(
                        replace(s, status=status, failure=failure, waiting_reason=None)
                        if s.identity == attempt.invocation.step
                        else s
                        for s in run.steps
                    ),
                    generation=run.generation + 1,
                    updated_at=updated_at,
                )
                _save_run(connection, proposed, expected=run)
                _append_event(
                    connection,
                    proposed,
                    "activity-failure:" + invocation_id,
                    WorkflowEventKind.ACTIVITY,
                    fact,
                )
                return WorkflowWriteResult(proposed, proposed.generation)

        async with self._write_lock:
            return await run_blocking(lambda: _guard(consume))

    async def publish_workflow_activity(
        self,
        invocation: WorkflowActivityInvocation,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowWriteResult:
        integer(expected_generation)
        identifier(owner_id)
        integer(owner_fence)
        timestamp(updated_at)

        def publish() -> WorkflowWriteResult:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                run = _run(connection, invocation.run_id)
                old = _load_attempt(connection, invocation.invocation_id)
                if old is not None:
                    if old.invocation.fingerprint != invocation.fingerprint:
                        raise WorkflowStateError(
                            "activity invocation identity conflict", kind="conflict"
                        )
                    return WorkflowWriteResult(run, run.generation, replayed=True)
                if (run.generation, run.owner_id, run.owner_fence) != (
                    expected_generation,
                    owner_id,
                    owner_fence,
                ):
                    raise WorkflowStateError(
                        "activity interpreter owner is stale", kind="concurrent_modification"
                    )
                definition = _load_definition(connection, run.definition_fingerprint)
                if definition is None:
                    raise WorkflowStateError("activity definition is missing", kind="integrity")
                declared = _validate_position(definition, invocation.step)
                step = next((s for s in run.steps if s.identity == invocation.step), None)
                if (
                    run.status is not WorkflowStatus.RUNNING
                    or not isinstance(declared, Activity)
                    or declared.activity is not invocation.activity
                    or invocation.definition_fingerprint != run.definition_fingerprint
                    or invocation.parent_session_id != run.parent_session_id
                    or step is None
                    or step.status is not WorkflowStatus.READY
                    or step.input_fingerprint != invocation.input_fingerprint
                    or updated_at < run.updated_at
                ):
                    raise WorkflowStateError(
                        "activity is not ready for publication", kind="protocol"
                    )
                _validate_request(definition, declared, invocation)
                if (
                    invocation.created_at != updated_at
                    or not run.ledger.committed.known
                    or _exceeds_ceiling(run.ledger)
                ):
                    raise WorkflowStateError(
                        "activity publication time/budget differs", kind="protocol"
                    )
                proposed = replace(
                    run,
                    steps=tuple(
                        sorted(
                            (
                                *(s for s in run.steps if s.identity != invocation.step),
                                replace(
                                    step,
                                    status=WorkflowStatus.WAITING,
                                    waiting_reason="activity:" + invocation.invocation_id,
                                ),
                            ),
                            key=lambda s: s.identity.key,
                        )
                    ),
                    status=WorkflowStatus.WAITING,
                    waiting_reason="activity:" + invocation.invocation_id,
                    generation=run.generation + 1,
                    updated_at=updated_at,
                    position=invocation.step,
                )
                _save_run(connection, proposed, expected=run)
                _append_event(
                    connection,
                    proposed,
                    "activity:" + invocation.invocation_id,
                    WorkflowEventKind.ACTIVITY,
                    canonical(
                        {
                            "operation": "activity_published",
                            "invocation_id": invocation.invocation_id,
                            "invocation_fingerprint": invocation.fingerprint,
                            "step_key": invocation.step.key,
                        }
                    ),
                )
                _save_attempt(
                    connection,
                    WorkflowActivityAttempt(invocation),
                    created_generation=proposed.generation,
                )
                return WorkflowWriteResult(proposed, proposed.generation)

        async with self._write_lock:
            return await run_blocking(lambda: _guard(publish))

    async def get_workflow_activity(self, invocation_id: str) -> WorkflowActivityAttempt | None:
        identifier(invocation_id)

        def read() -> WorkflowActivityAttempt | None:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return _load_attempt(connection, invocation_id)

        return await run_blocking(lambda: _guard(read))

    async def claim_workflow_activity(
        self,
        invocation_id: str,
        *,
        expected_revision: int,
        owner_id: str,
        reserved: BudgetAmounts,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt:
        identifier(invocation_id)
        integer(expected_revision)
        identifier(owner_id)
        timestamp(updated_at)
        if not isinstance(reserved, BudgetAmounts) or not reserved.known:
            raise ValueError("activity claim requires known upper bounds")
        validate_activity_budget(reserved)

        def claim() -> WorkflowActivityAttempt:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_attempt(connection, invocation_id)
                if current is None:
                    raise WorkflowStateError("activity invocation missing", kind="missing")
                if current.state is WorkflowActivityState.CLAIMED and (
                    current.owner_id,
                    current.reserved,
                ) == (owner_id, reserved):
                    return current
                if (
                    current.state is not WorkflowActivityState.READY
                    or current.revision != expected_revision
                ):
                    raise WorkflowStateError(
                        "activity is already owned", kind="concurrent_modification"
                    )
                run = _run(connection, current.invocation.run_id)
                if run.status is not WorkflowStatus.WAITING or run.waiting_reason != (
                    "activity:" + invocation_id
                ):
                    raise WorkflowStateError("activity run is not waiting", kind="protocol")
                reservation_id = "activity-budget:" + invocation_id
                _budget_change(
                    connection,
                    run,
                    WorkflowChange(
                        WorkflowEventKind.RESERVED, reservation_id=reservation_id, amounts=reserved
                    ),
                    updated_at,
                    "activity-reserve:" + invocation_id,
                )
                claimed = replace(
                    current,
                    state=WorkflowActivityState.CLAIMED,
                    revision=current.revision + 1,
                    owner_id=owner_id,
                    owner_fence=current.owner_fence + 1,
                    reservation_id=reservation_id,
                    reserved=reserved,
                    updated_at=updated_at,
                )
                _save_attempt(connection, claimed, expected_revision=current.revision)
                return claimed

        async with self._write_lock:
            return await run_blocking(lambda: _guard(claim))

    async def start_workflow_activity(
        self,
        invocation_id: str,
        *,
        expected_revision: int,
        owner_id: str,
        owner_fence: int,
        updated_at: datetime,
    ) -> WorkflowActivityAttempt:
        identifier(invocation_id)
        integer(expected_revision)
        identifier(owner_id)
        integer(owner_fence)
        timestamp(updated_at)

        def start() -> WorkflowActivityAttempt:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_attempt(connection, invocation_id)
                if current is None:
                    raise WorkflowStateError("activity invocation missing", kind="missing")
                if current.state is WorkflowActivityState.RUNNING and (
                    current.owner_id,
                    current.owner_fence,
                ) == (owner_id, owner_fence):
                    return current
                if (
                    current.state is not WorkflowActivityState.CLAIMED
                    or current.revision != expected_revision
                    or (current.owner_id, current.owner_fence) != (owner_id, owner_fence)
                ):
                    raise WorkflowStateError(
                        "activity owner/fence is stale", kind="concurrent_modification"
                    )
                return _start_activity(connection, current, updated_at)

        async with self._write_lock:
            return await run_blocking(lambda: _guard(start))

    async def finish_workflow_activity(
        self,
        result: WorkflowActivityResult,
        *,
        expected_revision: int,
        owner_id: str,
        owner_fence: int,
    ) -> WorkflowActivityAttempt:
        integer(expected_revision)
        identifier(owner_id)
        integer(owner_fence)

        def finish() -> WorkflowActivityAttempt:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_attempt(connection, result.invocation_id)
                if current is None:
                    raise WorkflowStateError("activity invocation missing", kind="missing")
                if current.state.terminal:
                    if current.result != result:
                        raise WorkflowStateError(
                            "activity terminal result conflict", kind="conflict"
                        )
                    return current
                if (
                    current.state
                    not in {WorkflowActivityState.CLAIMED, WorkflowActivityState.RUNNING}
                    or current.revision != expected_revision
                    or (current.owner_id, current.owner_fence) != (owner_id, owner_fence)
                    or result.request_fingerprint != current.invocation.request_fingerprint
                    or result.activity is not current.invocation.activity
                ):
                    raise WorkflowStateError(
                        "activity result owner/source differs", kind="concurrent_modification"
                    )
                run = _run(connection, current.invocation.run_id)
                definition = _load_definition(connection, run.definition_fingerprint)
                if definition is None:
                    raise WorkflowStateError("activity definition is missing", kind="integrity")
                declared = _validate_position(definition, current.invocation.step)
                if not isinstance(declared, Activity):
                    raise WorkflowStateError("result step is not Activity", kind="integrity")
                if declared.activity is ActivityKind.ADOPT and "source" in dict(declared.inputs):
                    raise WorkflowStateError(
                        "bound ADOPT requires terminal-proof reconciliation", kind="protocol"
                    )
                if result.output_json is not None:
                    typed_json(
                        activity_output_schema(declared.activity), json.loads(result.output_json)
                    )
                if (
                    result.state is WorkflowActivityState.COMPLETED
                    and current.state is not WorkflowActivityState.RUNNING
                ):
                    raise WorkflowStateError(
                        "completed result requires start boundary", kind="protocol"
                    )
                validate_activity_budget(
                    result.usage, pre_dispatch=current.state is WorkflowActivityState.CLAIMED
                )
                if result.terminal_at < run.updated_at or result.terminal_at < (
                    current.updated_at or current.invocation.created_at
                ):
                    raise WorkflowStateError("activity terminal time precedes run", kind="protocol")
                return _settle_terminal(connection, current, result)

        async with self._write_lock:
            return await run_blocking(lambda: _guard(finish))
