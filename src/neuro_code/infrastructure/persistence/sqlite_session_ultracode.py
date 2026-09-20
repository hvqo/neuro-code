"""SQLite persistence ultracode owner.

This module owns one cohesive persistence responsibility.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

from neuro_code.application.ports.agent_swarm import ProcessLivenessProbe
from neuro_code.application.ports.ultracode import UltracodeExecutionClaim, UltracodeStoreError
from neuro_code.domain.execution import (
    MAX_REQUIREMENT_SNAPSHOT_BYTES,
    ExecutionBudget,
    ToolCallBudget,
    VerificationRequirementsSnapshot,
)
from neuro_code.domain.ultracode import (
    UltracodeDelegationDecision,
    UltracodeExecution,
    UltracodeExecutionState,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    _SqliteSessionPersistenceContext,
)
from neuro_code.shared.async_utils import run_blocking

_MAX_ULTRACODE_REQUIREMENTS_JSON_BYTES = MAX_REQUIREMENT_SNAPSHOT_BYTES + 128
_MAX_ULTRACODE_BUDGET_JSON_BYTES = 8 * 1024


class UltracodeMixin(_SqliteSessionPersistenceContext):
    """Mixin owning this SQLite persistence slice."""

    async def get_ultracode_execution(
        self,
        execution_id: str,
    ) -> UltracodeExecution | None:
        _validated_ultracode_identifier(execution_id)

        def load() -> UltracodeExecution | None:
            try:
                with closing(self._connect()) as connection:
                    return _load_ultracode_execution(connection, execution_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise UltracodeStoreError(
                    "Ultracode execution record is invalid",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                raise UltracodeStoreError("Ultracode execution could not be loaded") from error

        return await run_blocking(load)

    async def claim_ultracode_execution(
        self,
        execution: UltracodeExecution,
        *,
        now: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> UltracodeExecutionClaim:
        if not isinstance(execution, UltracodeExecution):
            raise TypeError("Ultracode execution must be canonical")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("Ultracode claim time must be timezone-aware")
        if not callable(owner_is_alive):
            raise TypeError("Ultracode owner liveness probe is required")
        _validated_ultracode_identifier(execution.execution_id)
        _validated_ultracode_identifier(execution.parent_session_id)
        _validated_ultracode_identifier(execution.parent_turn_id)
        _validated_ultracode_fingerprint(execution.input_fingerprint)
        _validated_ultracode_fingerprint(execution.context_fingerprint)
        _ultracode_verification_requirements_values(execution.verification_requirements)
        now_utc = now.astimezone(UTC)
        prepared = replace(execution, created_at=now_utc, updated_at=now_utc)

        def claim() -> UltracodeExecutionClaim:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_ultracode_execution(connection, prepared.execution_id)
                if current is None:
                    connection.execute(
                        _ULTRACODE_EXECUTION_INSERT,
                        _ultracode_execution_values(prepared),
                    )
                    connection.commit()
                    return UltracodeExecutionClaim(prepared, True)
                if not current.same_identity(prepared):
                    raise UltracodeStoreError(
                        "Ultracode execution identity is already bound to different input",
                        kind="integrity",
                    )
                if current.terminal:
                    connection.commit()
                    return UltracodeExecutionClaim(current, False)
                if (
                    current.owner_id == prepared.owner_id
                    and current.owner_pid == prepared.owner_pid
                    and current.owner_token == prepared.owner_token
                ):
                    connection.commit()
                    return UltracodeExecutionClaim(current, False)
                if owner_is_alive(current.owner_pid):
                    connection.commit()
                    return UltracodeExecutionClaim(current, False)
                cursor = connection.execute(
                    """
                    UPDATE orchestration_ultracode_executions
                    SET owner_id = ?, owner_pid = ?, owner_token = ?,
                        lease_expires_at = ?, generation = ?, updated_at = ?
                    WHERE execution_id = ? AND generation = ? AND owner_id = ?
                      AND owner_pid = ? AND owner_token = ?
                      AND state NOT IN (?, ?)
                    """,
                    (
                        prepared.owner_id,
                        prepared.owner_pid,
                        prepared.owner_token,
                        prepared.lease_expires_at.astimezone(UTC).isoformat(),
                        current.generation + 1,
                        now_utc.isoformat(),
                        current.execution_id,
                        current.generation,
                        current.owner_id,
                        current.owner_pid,
                        current.owner_token,
                        UltracodeExecutionState.COMPLETED.value,
                        UltracodeExecutionState.INDETERMINATE.value,
                    ),
                )
                if cursor.rowcount == 1:
                    connection.commit()
                    refreshed = _load_ultracode_execution(connection, current.execution_id)
                    if refreshed is None:
                        raise UltracodeStoreError(
                            "Ultracode execution disappeared after takeover",
                            kind="integrity",
                        )
                    return UltracodeExecutionClaim(refreshed, True)
                connection.commit()
                return UltracodeExecutionClaim(current, False)
            except UltracodeStoreError:
                connection.rollback()
                raise
            except (KeyError, TypeError, ValueError) as error:
                connection.rollback()
                raise UltracodeStoreError(
                    "Ultracode execution record is invalid",
                    kind="integrity",
                ) from error
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise UltracodeStoreError(
                    "Ultracode execution could not be claimed",
                    kind="concurrent_modification",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise UltracodeStoreError("Ultracode execution claim failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(claim)

    async def compare_and_transition_ultracode_execution(
        self,
        execution: UltracodeExecution,
        *,
        expected_generation: int,
        expected_state: UltracodeExecutionState,
    ) -> UltracodeExecution:
        if not isinstance(execution, UltracodeExecution):
            raise TypeError("Ultracode execution must be canonical")
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 0
        ):
            raise TypeError("Ultracode expected generation is invalid")
        if not isinstance(expected_state, UltracodeExecutionState):
            raise TypeError("Ultracode expected state is invalid")
        if execution.generation != expected_generation + 1:
            raise UltracodeStoreError(
                "Ultracode transition generation is invalid",
                kind="protocol",
            )
        _validated_ultracode_identifier(execution.execution_id)

        def transition() -> UltracodeExecution:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _load_ultracode_execution(connection, execution.execution_id)
                if current is None:
                    raise UltracodeStoreError(
                        "Ultracode execution is missing",
                        kind="unmanaged",
                    )
                if not current.same_identity(execution):
                    raise UltracodeStoreError(
                        "Ultracode execution identity changed",
                        kind="integrity",
                    )
                if current.state is execution.state and current == execution:
                    connection.commit()
                    return current
                if current.state is not expected_state or current.generation != expected_generation:
                    raise UltracodeStoreError(
                        "Ultracode lifecycle snapshot is stale",
                        kind="concurrent_modification",
                    )
                if not current.state.can_transition_to(execution.state):
                    raise UltracodeStoreError(
                        "Ultracode lifecycle transition is not allowed",
                        kind="protocol",
                    )
                if (
                    current.owner_id != execution.owner_id
                    or current.owner_pid != execution.owner_pid
                    or current.owner_token != execution.owner_token
                ):
                    raise UltracodeStoreError(
                        "Ultracode owner fence does not match",
                        kind="concurrent_modification",
                    )
                cursor = connection.execute(
                    """
                    UPDATE orchestration_ultracode_executions SET
                        state = ?, generation = ?, owner_id = ?, owner_pid = ?,
                        owner_token = ?, lease_expires_at = ?, final_response = ?,
                        final_result_fingerprint = ?, updated_at = ?
                    WHERE execution_id = ? AND state = ? AND generation = ?
                      AND owner_id = ? AND owner_pid = ? AND owner_token = ?
                    """,
                    (
                        execution.state.value,
                        execution.generation,
                        execution.owner_id,
                        execution.owner_pid,
                        execution.owner_token,
                        execution.lease_expires_at.astimezone(UTC).isoformat(),
                        execution.final_response,
                        execution.final_result_fingerprint,
                        execution.updated_at.astimezone(UTC).isoformat(),
                        execution.execution_id,
                        expected_state.value,
                        expected_generation,
                        current.owner_id,
                        current.owner_pid,
                        current.owner_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise UltracodeStoreError(
                        "Ultracode lifecycle transition was lost",
                        kind="concurrent_modification",
                    )
                connection.commit()
                refreshed = _load_ultracode_execution(connection, execution.execution_id)
                if refreshed is None:
                    raise UltracodeStoreError(
                        "Ultracode execution disappeared after transition",
                        kind="integrity",
                    )
                return refreshed
            except UltracodeStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise UltracodeStoreError(
                    "Ultracode lifecycle transition violated durable identity",
                    kind="integrity",
                ) from error
            except sqlite3.Error as error:
                connection.rollback()
                raise UltracodeStoreError("Ultracode lifecycle transition failed") from error
            finally:
                connection.close()

        async with self._write_lock:
            return await run_blocking(transition)

    async def get_agent_ultracode_execution(
        self,
        execution_id: str,
    ) -> UltracodeExecution | None:
        """Compatibility spelling for the single canonical projection."""

        return await self.get_ultracode_execution(execution_id)


_ULTRACODE_EXECUTION_SELECT = """
    SELECT execution_id, parent_session_id, parent_turn_id,
           input_fingerprint, context_fingerprint, decision, downstream_id,
           provider_name, model_name, context_affinity, state, generation,
           owner_id, owner_pid, owner_token, lease_expires_at,
           final_response, final_result_fingerprint, created_at, updated_at,
           verification_requirements_json, verification_requirements_fingerprint,
           main_max_execution_budget_json
    FROM orchestration_ultracode_executions
"""

_ULTRACODE_EXECUTION_INSERT = """
    INSERT INTO orchestration_ultracode_executions(
        execution_id, parent_session_id, parent_turn_id,
        input_fingerprint, context_fingerprint, decision, downstream_id,
        provider_name, model_name, context_affinity, state, generation,
        owner_id, owner_pid, owner_token, lease_expires_at,
        final_response, final_result_fingerprint, created_at, updated_at,
        verification_requirements_json, verification_requirements_fingerprint,
        main_max_execution_budget_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _validated_ultracode_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Ultracode identifier is invalid")


def _validated_ultracode_fingerprint(value: str) -> None:
    _validated_ultracode_identifier(value)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("Ultracode fingerprint is invalid")


def _ultracode_execution_values(execution: UltracodeExecution) -> tuple[object, ...]:
    requirements_json, requirements_fingerprint = _ultracode_verification_requirements_values(
        execution.verification_requirements
    )
    budget_json = _ultracode_execution_budget_value(execution.main_max_execution_budget)
    return (
        execution.execution_id,
        execution.parent_session_id,
        execution.parent_turn_id,
        execution.input_fingerprint,
        execution.context_fingerprint,
        execution.decision.value,
        execution.downstream_id,
        execution.provider_name,
        execution.model_name,
        execution.context_affinity,
        execution.state.value,
        execution.generation,
        execution.owner_id,
        execution.owner_pid,
        execution.owner_token,
        execution.lease_expires_at.astimezone(UTC).isoformat(),
        execution.final_response,
        execution.final_result_fingerprint,
        execution.created_at.astimezone(UTC).isoformat(),
        execution.updated_at.astimezone(UTC).isoformat(),
        requirements_json,
        requirements_fingerprint,
        budget_json,
    )


def _load_ultracode_execution(
    connection: sqlite3.Connection,
    execution_id: str,
) -> UltracodeExecution | None:
    row = connection.execute(
        _ULTRACODE_EXECUTION_SELECT + " WHERE execution_id = ?",
        (execution_id,),
    ).fetchone()
    return _ultracode_execution_from_row(row) if row is not None else None


def _ultracode_execution_from_row(row: Sequence[object]) -> UltracodeExecution:
    if len(row) != 23:
        raise ValueError("Ultracode execution record is malformed")
    (
        execution_id,
        parent_session_id,
        parent_turn_id,
        input_fingerprint,
        context_fingerprint,
        raw_decision,
        downstream_id,
        provider_name,
        model_name,
        context_affinity,
        raw_state,
        generation,
        owner_id,
        owner_pid,
        owner_token,
        lease_expires_at,
        final_response,
        final_result_fingerprint,
        created_at,
        updated_at,
        verification_requirements_json,
        verification_requirements_fingerprint,
        main_max_execution_budget_json,
    ) = row
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool):
        raise ValueError("Ultracode owner PID is invalid")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise ValueError("Ultracode generation is invalid")
    return UltracodeExecution(
        execution_id=str(execution_id),
        parent_session_id=str(parent_session_id),
        parent_turn_id=str(parent_turn_id),
        input_fingerprint=str(input_fingerprint),
        context_fingerprint=str(context_fingerprint),
        decision=UltracodeDelegationDecision(str(raw_decision)),
        downstream_id=str(downstream_id),
        provider_name=str(provider_name),
        model_name=str(model_name),
        context_affinity=(str(context_affinity) if context_affinity is not None else None),
        state=UltracodeExecutionState(str(raw_state)),
        generation=generation,
        owner_id=str(owner_id),
        owner_pid=owner_pid,
        owner_token=str(owner_token),
        lease_expires_at=datetime.fromisoformat(str(lease_expires_at)),
        created_at=datetime.fromisoformat(str(created_at)),
        updated_at=datetime.fromisoformat(str(updated_at)),
        final_response=(str(final_response) if final_response is not None else None),
        final_result_fingerprint=(
            str(final_result_fingerprint) if final_result_fingerprint is not None else None
        ),
        verification_requirements=_ultracode_verification_requirements_from_values(
            verification_requirements_json,
            verification_requirements_fingerprint,
        ),
        main_max_execution_budget=_ultracode_execution_budget_from_value(
            main_max_execution_budget_json
        ),
    )


def _ultracode_execution_budget_value(budget: ExecutionBudget | None) -> str | None:
    if budget is None:
        return None
    if not isinstance(budget, ExecutionBudget):
        raise TypeError("Ultracode execution budget is not canonical")
    payload = {
        "max_model_calls": budget.max_model_calls,
        "max_tool_rounds": budget.max_tool_rounds,
        "max_tool_calls": budget.max_tool_calls,
        "max_calls_per_tool": budget.max_calls_per_tool,
        "max_wall_seconds": budget.max_wall_seconds,
        "max_input_tokens": budget.max_input_tokens,
        "max_output_tokens": budget.max_output_tokens,
        "max_total_tokens": budget.max_total_tokens,
        "per_tool_limits": [
            {"tool_name": limit.tool_name, "max_calls": limit.max_calls}
            for limit in budget.per_tool_limits
        ],
    }
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(value.encode("utf-8")) > _MAX_ULTRACODE_BUDGET_JSON_BYTES:
        raise ValueError("Ultracode execution budget exceeds its byte bound")
    return value


def _ultracode_execution_budget_from_value(raw_value: object) -> ExecutionBudget | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError("Ultracode execution budget projection is invalid")
    if len(raw_value.encode("utf-8")) > _MAX_ULTRACODE_BUDGET_JSON_BYTES:
        raise ValueError("Ultracode execution budget exceeds its byte bound")
    payload = json.loads(raw_value)
    if not isinstance(payload, dict):
        raise ValueError("Ultracode execution budget JSON must be an object")
    expected_keys = {
        "max_model_calls",
        "max_tool_rounds",
        "max_tool_calls",
        "max_calls_per_tool",
        "max_wall_seconds",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
        "per_tool_limits",
    }
    if set(payload) != expected_keys:
        raise ValueError("Ultracode execution budget JSON shape is invalid")
    raw_limits = payload["per_tool_limits"]
    if not isinstance(raw_limits, list):
        raise ValueError("Ultracode execution budget per-tool limits are invalid")
    limits: list[ToolCallBudget] = []
    for raw_limit in raw_limits:
        if not isinstance(raw_limit, dict) or set(raw_limit) != {"tool_name", "max_calls"}:
            raise ValueError("Ultracode execution budget per-tool limit is invalid")
        limits.append(ToolCallBudget(raw_limit["tool_name"], raw_limit["max_calls"]))
    budget = ExecutionBudget(
        max_model_calls=payload["max_model_calls"],
        max_tool_rounds=payload["max_tool_rounds"],
        max_tool_calls=payload["max_tool_calls"],
        max_calls_per_tool=payload["max_calls_per_tool"],
        max_wall_seconds=payload["max_wall_seconds"],
        max_input_tokens=payload["max_input_tokens"],
        max_output_tokens=payload["max_output_tokens"],
        max_total_tokens=payload["max_total_tokens"],
        per_tool_limits=tuple(limits),
    )
    if _ultracode_execution_budget_value(budget) != raw_value:
        raise ValueError("Ultracode execution budget JSON is not canonical")
    return budget


def _ultracode_verification_requirements_values(
    requirements: VerificationRequirementsSnapshot | None,
) -> tuple[str | None, str | None]:
    if requirements is None:
        return None, None
    if not isinstance(requirements, VerificationRequirementsSnapshot):
        raise TypeError("Ultracode verification requirements are not canonical")
    payload = json.dumps(
        requirements.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > _MAX_ULTRACODE_REQUIREMENTS_JSON_BYTES:
        raise ValueError("Ultracode verification requirements exceed their byte bound")
    return payload, requirements.fingerprint


def _ultracode_verification_requirements_from_values(
    raw_json: object,
    raw_fingerprint: object,
) -> VerificationRequirementsSnapshot | None:
    if raw_json is None and raw_fingerprint is None:
        return None
    if not isinstance(raw_json, str) or not isinstance(raw_fingerprint, str):
        raise ValueError("Ultracode verification requirements projection is incomplete")
    if len(raw_json.encode("utf-8")) > _MAX_ULTRACODE_REQUIREMENTS_JSON_BYTES:
        raise ValueError("Ultracode verification requirements exceed their byte bound")
    payload = json.loads(raw_json)
    if not isinstance(payload, dict):
        raise ValueError("Ultracode verification requirements JSON must be an object")
    requirements = VerificationRequirementsSnapshot.from_dict(payload)
    canonical_json, canonical_fingerprint = _ultracode_verification_requirements_values(
        requirements
    )
    if canonical_json != raw_json or canonical_fingerprint != raw_fingerprint:
        raise ValueError("Ultracode verification requirements projection is not canonical")
    return requirements
