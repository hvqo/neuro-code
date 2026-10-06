"""DW2 atomic SQLite facts. No interpreter or side-effect execution entry."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.definition import (
    ArtifactRef,
    Branch,
    Map,
    Repeat,
    Step,
    WorkflowDefinition,
    compile_workflow,
)
from neuro_code.domain.workflows.state import (
    COMPILER_VERSION,
    BudgetAmounts,
    BudgetReservation,
    StepIdentity,
    WorkflowBudgetLedger,
    WorkflowChange,
    WorkflowEventKind,
    WorkflowFailure,
    WorkflowJournalEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepInstance,
    WorkflowWriteResult,
    fingerprint,
    identifier,
    integer,
    timestamp,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.shared.async_utils import run_blocking


class WorkflowsMixin(_SqliteSessionPersistenceContext):
    """One repository owner for durable Workflow snapshots and facts."""

    async def insert_workflow_definition(
        self, definition: WorkflowDefinition, *, compiler_version: int = COMPILER_VERSION
    ) -> WorkflowDefinition:
        if not isinstance(definition, WorkflowDefinition):
            raise TypeError("definition must be canonical")
        if type(compiler_version) is not int or compiler_version != COMPILER_VERSION:
            raise ValueError("unsupported workflow compiler version")

        def insert() -> WorkflowDefinition:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT canonical_ir, schema_version, compiler_version FROM workflow_definitions WHERE fingerprint = ?",
                    (definition.fingerprint,),
                ).fetchone()
                values = (definition.canonical_json, definition.version, compiler_version)
                if row is not None:
                    if row != values:
                        raise WorkflowStateError("definition identity conflict", kind="conflict")
                else:
                    connection.execute(
                        "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?)",
                        (definition.fingerprint, *values),
                    )
                return definition

        async with self._write_lock:
            return await run_blocking(lambda: _guard(insert))

    async def get_workflow_definition(self, fingerprint: str) -> WorkflowDefinition | None:
        _validate_fingerprint(fingerprint)

        def load() -> WorkflowDefinition | None:
            with closing(self._connect()) as connection:
                return _load_definition(connection, fingerprint)

        return await run_blocking(lambda: _guard(load))

    async def create_workflow_run(
        self,
        run_id: str,
        *,
        definition_fingerprint: str,
        parent_session_id: str,
        input_fingerprint: str,
        request_id: str,
        created_at: datetime,
        ceiling: BudgetAmounts | None = None,
    ) -> WorkflowWriteResult:
        identifier(run_id)
        identifier(parent_session_id)
        _validate_fingerprint(definition_fingerprint)
        _validate_fingerprint(input_fingerprint)
        identifier(request_id)
        timestamp(created_at)
        if run_id == definition_fingerprint:
            raise ValueError("run identity must be independent of definition identity")
        if ceiling is not None and not isinstance(ceiling, BudgetAmounts):
            raise ValueError("ceiling must be canonical")
        payload = _json(
            {
                "run_id": run_id,
                "definition_fingerprint": definition_fingerprint,
                "parent_session_id": parent_session_id,
                "input_fingerprint": input_fingerprint,
                "created_at": created_at,
                "ceiling": asdict(ceiling) if ceiling else None,
            }
        )

        def create() -> WorkflowWriteResult:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = _replay(connection, run_id, request_id, payload)
                if replay is not None:
                    return replay
                if _load_run(connection, run_id) is not None:
                    raise WorkflowStateError("run identity already exists", kind="conflict")
                definition = _load_definition(connection, definition_fingerprint)
                if definition is None:
                    raise WorkflowStateError("definition is missing", kind="missing")
                declared = BudgetAmounts.ceiling(definition.budget)
                actual = declared if ceiling is None else ceiling
                if not actual.known or any(
                    a is None or b is None or a > b
                    for a, b in zip(actual.values, declared.values, strict=True)
                ):
                    raise WorkflowStateError(
                        "run budget cannot widen definition ceiling", kind="protocol"
                    )
                run = WorkflowRun(
                    run_id,
                    definition_fingerprint,
                    parent_session_id,
                    input_fingerprint,
                    WorkflowStatus.READY,
                    0,
                    None,
                    0,
                    created_at,
                    created_at,
                    WorkflowBudgetLedger(actual),
                )
                _save_run(connection, run, creating=True)
                _append_event(connection, run, request_id, WorkflowEventKind.CREATED, payload)
                return WorkflowWriteResult(run, 0)

        async with self._write_lock:
            return await run_blocking(lambda: _guard(create))

    async def get_workflow_run(self, run_id: str) -> WorkflowRun | None:
        identifier(run_id)

        def load() -> WorkflowRun | None:
            # Multiple table reads need the same WAL snapshot.
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return _load_run(connection, run_id)

        return await run_blocking(lambda: _guard(load))

    async def claim_workflow_run(
        self,
        run_id: str,
        *,
        expected_generation: int,
        expected_owner_fence: int,
        owner_id: str,
        request_id: str,
        updated_at: datetime,
    ) -> WorkflowWriteResult:
        integer(expected_owner_fence)
        payload = _json(
            {
                "operation": "claim",
                "expected_generation": expected_generation,
                "expected_owner_fence": expected_owner_fence,
                "owner_id": owner_id,
                "updated_at": updated_at,
            }
        )

        def claim(
            connection: sqlite3.Connection, current: WorkflowRun
        ) -> tuple[WorkflowRun, WorkflowEventKind]:
            if current.owner_fence != expected_owner_fence:
                raise WorkflowStateError("owner fence changed", kind="concurrent_modification")
            if current.owner_id is None:
                status, failure, wait = WorkflowStatus.RUNNING, None, None
            else:
                # A restart is not proof that old work did not execute. Do not
                # reset steps/reservations or authorize their replay.
                status = WorkflowStatus.NEEDS_ATTENTION
                failure = WorkflowFailure(
                    "owner_replaced", "Prior owner work requires reconciliation"
                )
                wait = None
            return replace(
                current,
                status=status,
                failure=failure,
                waiting_reason=wait,
                owner_id=owner_id,
                owner_fence=current.owner_fence + 1,
            ), WorkflowEventKind.CLAIMED if current.owner_id is None else WorkflowEventKind.RESUMED

        return await self._write_workflow(
            run_id,
            request_id,
            payload,
            expected_generation,
            owner_id,
            updated_at,
            claim,
        )

    async def transition_workflow_run(
        self,
        run_id: str,
        change: WorkflowChange,
        *,
        expected_generation: int,
        owner_id: str,
        owner_fence: int,
        request_id: str,
        updated_at: datetime,
    ) -> WorkflowWriteResult:
        if not isinstance(change, WorkflowChange):
            raise TypeError("change must be canonical")
        integer(owner_fence)
        payload = _json(
            {
                "operation": "transition",
                "expected_generation": expected_generation,
                "owner_id": owner_id,
                "owner_fence": owner_fence,
                "updated_at": updated_at,
                "change": asdict(change),
            }
        )

        def transition(
            connection: sqlite3.Connection, current: WorkflowRun
        ) -> tuple[WorkflowRun, WorkflowEventKind]:
            if current.owner_id != owner_id or current.owner_fence != owner_fence:
                raise WorkflowStateError("owner is fenced out", kind="concurrent_modification")
            definition = _load_definition(connection, current.definition_fingerprint)
            if definition is None:
                raise WorkflowStateError("definition disappeared", kind="integrity")
            return _apply_change(connection, current, definition, change, updated_at), change.kind

        return await self._write_workflow(
            run_id,
            request_id,
            payload,
            expected_generation,
            owner_id,
            updated_at,
            transition,
            allow_terminal=change.kind
            in {WorkflowEventKind.CONSUMED, WorkflowEventKind.RECONCILED},
        )

    async def _write_workflow(
        self,
        run_id: str,
        request_id: str,
        payload: str,
        expected_generation: int,
        owner_id: str,
        updated_at: datetime,
        apply: Callable[[sqlite3.Connection, WorkflowRun], tuple[WorkflowRun, WorkflowEventKind]],
        *,
        allow_terminal: bool = False,
    ) -> WorkflowWriteResult:
        identifier(run_id)
        identifier(request_id)
        identifier(owner_id)
        integer(expected_generation)
        timestamp(updated_at)

        def write() -> WorkflowWriteResult:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = _replay(connection, run_id, request_id, payload)
                if replay is not None:
                    return replay
                current = _load_run(connection, run_id)
                if current is None:
                    raise WorkflowStateError("run is missing", kind="missing")
                if current.generation != expected_generation:
                    raise WorkflowStateError("generation changed", kind="concurrent_modification")
                if current.status.terminal and not allow_terminal:
                    raise WorkflowStateError("terminal run cannot advance", kind="protocol")
                if updated_at < current.updated_at:
                    raise WorkflowStateError("update time moved backwards", kind="protocol")
                try:
                    proposed, kind = apply(connection, current)
                    proposed = replace(
                        proposed, generation=current.generation + 1, updated_at=updated_at
                    )
                except ValueError as error:
                    raise WorkflowStateError(
                        "mutation violates bounded state contract", kind="protocol"
                    ) from error
                _save_run(connection, proposed, expected=current)
                _append_event(connection, proposed, request_id, kind, payload)
                return WorkflowWriteResult(proposed, proposed.generation)

        async with self._write_lock:
            return await run_blocking(lambda: _guard(write))

    async def get_workflow_journal(
        self,
        run_id: str,
        *,
        after_generation: int = -1,
        limit: int = 100,
    ) -> tuple[WorkflowJournalEvent, ...]:
        identifier(run_id)
        if type(after_generation) is not int or after_generation < -1:
            raise ValueError("invalid journal cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("journal page limit must be 1..100")

        def load() -> tuple[WorkflowJournalEvent, ...]:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT generation, request_id, kind, payload_fingerprint, payload_json, created_at FROM workflow_transition_journal WHERE run_id = ? AND generation > ? ORDER BY generation LIMIT ?",
                    (run_id, after_generation, limit),
                ).fetchall()
                return tuple(
                    WorkflowJournalEvent(
                        run_id,
                        row[0],
                        row[1],
                        WorkflowEventKind(row[2]),
                        row[3],
                        row[4],
                        datetime.fromisoformat(row[5]),
                    )
                    for row in rows
                )

        return await run_blocking(lambda: _guard(load))


def _validate_fingerprint(value: str) -> None:
    fingerprint(value)


def _guard[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except WorkflowStateError:
        raise
    except sqlite3.Error as error:
        raise WorkflowStateError("workflow storage operation failed") from error
    except (ValueError, TypeError, KeyError) as error:
        raise WorkflowStateError("invalid persisted workflow fact", kind="integrity") from error


def _json(value: object) -> str:
    def encode(item: Any) -> str:
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat()
        if isinstance(item, Enum):
            return str(item.value)
        raise TypeError("unsupported durable fact")

    return json.dumps(
        value, default=encode, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_definition(connection: sqlite3.Connection, key: str) -> WorkflowDefinition | None:
    row = connection.execute(
        "SELECT canonical_ir, schema_version, compiler_version FROM workflow_definitions WHERE fingerprint = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    definition = compile_workflow(row[0])
    if (
        definition.fingerprint != key
        or definition.canonical_json != row[0]
        or definition.version != row[1]
        or row[2] != COMPILER_VERSION
    ):
        raise WorkflowStateError("definition integrity mismatch", kind="integrity")
    return definition


def _step(data: dict[str, Any]) -> WorkflowStepInstance:
    return WorkflowStepInstance(
        StepIdentity(**data["identity"]),
        WorkflowStatus(data["status"]),
        data["input_fingerprint"],
        tuple(ArtifactRef(**ref) for ref in data["result_refs"]),
        WorkflowFailure(**data["failure"]) if data["failure"] else None,
        data["waiting_reason"],
    )


def _reservation(data: dict[str, Any]) -> BudgetReservation:
    return BudgetReservation(
        data["reservation_id"],
        BudgetAmounts(**data["reserved"]),
        datetime.fromisoformat(data["created_at"]),
        BudgetAmounts(**data["consumed"]) if data["consumed"] is not None else None,
        datetime.fromisoformat(data["settled_at"]) if data["settled_at"] else None,
    )


def _load_run(connection: sqlite3.Connection, run_id: str) -> WorkflowRun | None:
    row = connection.execute(
        "SELECT snapshot_json, generation, status, owner_id, owner_fence, definition_fingerprint, parent_session_id FROM workflow_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row[0])
    step_rows = connection.execute(
        "SELECT instance_key, snapshot_json FROM workflow_step_instances WHERE run_id = ? ORDER BY instance_key",
        (run_id,),
    ).fetchall()
    steps = tuple(_step(json.loads(s[1])) for s in step_rows)
    reservation_rows = connection.execute(
        "SELECT reservation_id, snapshot_json FROM workflow_budget_reservations WHERE run_id = ? ORDER BY reservation_id",
        (run_id,),
    ).fetchall()
    reservations = tuple(_reservation(json.loads(r[1])) for r in reservation_rows)
    if any(s.identity.key != row[0] for s, row in zip(steps, step_rows, strict=True)) or any(
        r.reservation_id != row[0] for r, row in zip(reservations, reservation_rows, strict=True)
    ):
        raise WorkflowStateError("child fact identity mismatch", kind="integrity")
    run = WorkflowRun(
        data["run_id"],
        data["definition_fingerprint"],
        data["parent_session_id"],
        data["input_fingerprint"],
        WorkflowStatus(data["status"]),
        data["generation"],
        data["owner_id"],
        data["owner_fence"],
        datetime.fromisoformat(data["created_at"]),
        datetime.fromisoformat(data["updated_at"]),
        WorkflowBudgetLedger(BudgetAmounts(**data["ledger"]["ceiling"]), reservations),
        StepIdentity(**data["position"]) if data["position"] else None,
        steps,
        WorkflowFailure(**data["failure"]) if data["failure"] else None,
        data["waiting_reason"],
    )
    if (
        run.run_id != run_id
        or (
            run.generation,
            run.status.value,
            run.owner_id,
            run.owner_fence,
            run.definition_fingerprint,
            run.parent_session_id,
        )
        != row[1:]
    ):
        raise WorkflowStateError("run snapshot integrity mismatch", kind="integrity")
    return run


def _save_run(
    connection: sqlite3.Connection,
    run: WorkflowRun,
    *,
    creating: bool = False,
    expected: WorkflowRun | None = None,
) -> None:
    core = replace(run, steps=(), ledger=WorkflowBudgetLedger(run.ledger.ceiling))
    payload = _json(asdict(core))
    if creating:
        connection.execute(
            "INSERT INTO workflow_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.definition_fingerprint,
                run.parent_session_id,
                run.generation,
                run.status.value,
                run.owner_id,
                run.owner_fence,
                payload,
            ),
        )
    else:
        assert expected is not None
        cursor = connection.execute(
            "UPDATE workflow_runs SET generation = ?, status = ?, owner_id = ?, owner_fence = ?, snapshot_json = ? WHERE run_id = ? AND generation = ? AND owner_id IS ? AND owner_fence = ?",
            (
                run.generation,
                run.status.value,
                run.owner_id,
                run.owner_fence,
                payload,
                run.run_id,
                expected.generation,
                expected.owner_id,
                expected.owner_fence,
            ),
        )
        if cursor.rowcount != 1:
            raise WorkflowStateError("snapshot CAS failed", kind="concurrent_modification")
    for step in run.steps:
        connection.execute(
            "INSERT INTO workflow_step_instances VALUES (?, ?, ?) ON CONFLICT(run_id, instance_key) DO UPDATE SET snapshot_json = excluded.snapshot_json",
            (run.run_id, step.identity.key, _json(asdict(step))),
        )
    for entry in run.ledger.reservations:
        connection.execute(
            "INSERT INTO workflow_budget_reservations VALUES (?, ?, ?) ON CONFLICT(run_id, reservation_id) DO UPDATE SET snapshot_json = excluded.snapshot_json",
            (run.run_id, entry.reservation_id, _json(asdict(entry))),
        )


def _replay(
    connection: sqlite3.Connection, run_id: str, request_id: str, payload: str
) -> WorkflowWriteResult | None:
    row = connection.execute(
        "SELECT generation, payload_fingerprint, payload_json FROM workflow_transition_journal WHERE run_id = ? AND request_id = ?",
        (run_id, request_id),
    ).fetchone()
    if row is None:
        return None
    if row[1] != _digest(payload) or row[2] != payload:
        raise WorkflowStateError("request identity payload conflict", kind="conflict")
    run = _load_run(connection, run_id)
    if run is None:
        raise WorkflowStateError("journal has no snapshot", kind="integrity")
    return WorkflowWriteResult(run, row[0], replayed=True)


def _append_event(
    connection: sqlite3.Connection,
    run: WorkflowRun,
    request_id: str,
    kind: WorkflowEventKind,
    payload: str,
) -> None:
    if len(payload.encode("utf-8")) > 32768:
        raise WorkflowStateError("journal fact exceeds byte bound", kind="bounds")
    connection.execute(
        "INSERT INTO workflow_transition_journal VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run.run_id,
            run.generation,
            request_id,
            kind.value,
            _digest(payload),
            payload,
            run.updated_at.astimezone(UTC).isoformat(),
        ),
    )


def _positions(definition: WorkflowDefinition) -> dict[str, tuple[Step, int, bool]]:
    result: dict[str, tuple[Step, int, bool]] = {}

    def walk(steps: tuple[Step, ...], repeat_max: int = 0, mapped: bool = False) -> None:
        for step in steps:
            result[step.step_id] = (step, repeat_max, mapped)
            if isinstance(step, Branch):
                for _, children in step.paths:
                    walk(children, repeat_max, mapped)
            elif isinstance(step, Repeat):
                walk(step.steps, step.max_iterations, mapped)
            elif isinstance(step, Map):
                walk((step.batch,), repeat_max, True)

    walk(definition.steps)
    return result


def _validate_position(
    definition: WorkflowDefinition, identity: StepIdentity, *, iteration_fact: bool = False
) -> Step:
    entry = _positions(definition).get(identity.step_id)
    if entry is None:
        raise WorkflowStateError("step is not declared in definition", kind="protocol")
    step, maximum, mapped = entry
    if iteration_fact and isinstance(step, Repeat):
        maximum = step.max_iterations
    if (maximum == 0 and identity.iteration != 0) or (
        maximum > 0 and not 1 <= identity.iteration <= maximum
    ):
        raise WorkflowStateError("step iteration outside definition", kind="protocol")
    if mapped != (identity.item_key is not None):
        raise WorkflowStateError("item key does not match Map scope", kind="protocol")
    return step


def _apply_change(
    connection: sqlite3.Connection,
    run: WorkflowRun,
    definition: WorkflowDefinition,
    change: WorkflowChange,
    now: datetime,
) -> WorkflowRun:
    if change.kind is WorkflowEventKind.TRANSITION:
        assert change.status is not None
        if not run.status.permits(change.status):
            raise WorkflowStateError("invalid run state transition", kind="protocol")
        if change.status in {WorkflowStatus.RUNNING, WorkflowStatus.COMPLETED} and (
            not run.ledger.committed.known or _exceeds_ceiling(run.ledger)
        ):
            raise WorkflowStateError(
                "budget must be reconciled before progress", kind="needs_attention"
            )
        if change.position:
            _validate_position(
                definition, change.position, iteration_fact=change.position.iteration > 0
            )
        if change.status is WorkflowStatus.COMPLETED and (
            any(s.status is not WorkflowStatus.COMPLETED for s in run.steps)
            or any(r.consumed is None or not r.consumed.known for r in run.ledger.reservations)
        ):
            raise WorkflowStateError("completion has unresolved durable work", kind="protocol")
        return replace(
            run,
            status=change.status,
            position=change.position if change.position is not None else run.position,
            failure=change.failure,
            waiting_reason=change.waiting_reason,
        )
    if change.kind is WorkflowEventKind.STEP:
        assert change.step is not None
        _validate_position(definition, change.step.identity)
        old = next((s for s in run.steps if s.identity == change.step.identity), None)
        if old is None:
            if change.step.status is not WorkflowStatus.READY:
                raise WorkflowStateError("new step must start READY", kind="protocol")
        elif (
            old.input_fingerprint != change.step.input_fingerprint
            or not old.status.permits(change.step.status)
            or old.status is change.step.status
        ):
            raise WorkflowStateError("step identity/input/state conflict", kind="conflict")
        steps = (*(s for s in run.steps if s.identity != change.step.identity), change.step)
        return replace(run, steps=tuple(sorted(steps, key=lambda s: s.identity.key)))
    if change.kind in {WorkflowEventKind.BRANCH, WorkflowEventKind.ITERATION}:
        assert change.position is not None
        step = _validate_position(
            definition, change.position, iteration_fact=change.kind is WorkflowEventKind.ITERATION
        )
        rows = connection.execute(
            "SELECT payload_json FROM workflow_transition_journal WHERE run_id = ? AND kind = ?",
            (run.run_id, change.kind.value),
        ).fetchall()
        previous = [json.loads(row[0])["change"] for row in rows]
        if change.kind is WorkflowEventKind.BRANCH:
            if not isinstance(step, Branch) or change.selected_path not in dict(step.paths):
                raise WorkflowStateError("branch selection is not declared", kind="protocol")
            if any(p["position"] == asdict(change.position) for p in previous):
                raise WorkflowStateError("branch decision already recorded", kind="conflict")
        else:
            last = max(
                (
                    p["position"]["iteration"]
                    for p in previous
                    if p["position"]["step_id"] == change.position.step_id
                ),
                default=0,
            )
            if not isinstance(step, Repeat) or change.position.iteration != last + 1:
                raise WorkflowStateError(
                    "iteration must advance one declared round", kind="protocol"
                )
        return replace(run, position=change.position)
    assert change.reservation_id is not None
    assert change.amounts is not None
    entries = list(run.ledger.reservations)
    index = next(
        (i for i, r in enumerate(entries) if r.reservation_id == change.reservation_id), None
    )
    if change.kind is WorkflowEventKind.RESERVED:
        if index is not None:
            raise WorkflowStateError("reservation identity already exists", kind="conflict")
        if not run.ledger.committed.known:
            raise WorkflowStateError(
                "unknown usage requires reconciliation", kind="needs_attention"
            )
        entries.append(BudgetReservation(change.reservation_id, change.amounts, now))
    else:
        if index is None:
            raise WorkflowStateError("reservation is missing", kind="missing")
        entry = entries[index]
        if change.kind is WorkflowEventKind.RECONCILED:
            if entry.consumed is None or entry.consumed.known:
                raise WorkflowStateError(
                    "only unknown consumption can be reconciled", kind="protocol"
                )
            if any(
                old is not None and old != new
                for old, new in zip(entry.consumed.values, change.amounts.values, strict=True)
            ):
                raise WorkflowStateError(
                    "reconciliation changed known consumption", kind="conflict"
                )
        elif entry.consumed is not None:
            raise WorkflowStateError("reservation already consumed", kind="conflict")
        entries[index] = replace(entry, consumed=change.amounts, settled_at=now)
    ledger = WorkflowBudgetLedger(
        run.ledger.ceiling, tuple(sorted(entries, key=lambda r: r.reservation_id))
    )
    exceeded = _exceeds_ceiling(ledger)
    if change.kind is WorkflowEventKind.RESERVED and exceeded:
        raise WorkflowStateError("budget ceiling exceeded", kind="budget_exceeded")
    if run.status.terminal:
        # Late accounting remains possible after cancel/failure; it cannot reopen
        # a terminal run or reserve new work.
        return replace(run, ledger=ledger)
    if exceeded or not ledger.committed.known:
        # Record the truth even when real measured usage exceeds its reservation.
        # Rejecting accounting would lose durable evidence and falsely free budget.
        return replace(
            run,
            ledger=ledger,
            status=WorkflowStatus.NEEDS_ATTENTION,
            failure=WorkflowFailure(
                "budget_uncertain" if not ledger.committed.known else "budget_exceeded",
                "Budget requires reconciliation",
            ),
            waiting_reason=None,
        )
    return replace(run, ledger=ledger)


def _exceeds_ceiling(ledger: WorkflowBudgetLedger) -> bool:
    return any(
        value is not None and cap is not None and value > cap
        for value, cap in zip(ledger.committed.values, ledger.ceiling.values, strict=True)
    )
