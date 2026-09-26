"""Metadata-only, bounded Agent runtime trace collection.

The collector is a read-only projection over existing lifecycle events and a
small number of ephemeral timing facts. It never persists, changes prompts, or
retains event bodies, arguments, output, paths, or reasoning.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from types import MappingProxyType
from typing import Any, cast

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.shared.redaction import redact_sensitive_text

MAX_TRACE_TURNS = 32
MAX_TRACE_RECORDS = 8192
MAX_RECORDS_PER_TURN = 2048
MAX_RECORD_NAME_CHARS = 96
MAX_TRACE_EXPORT_BYTES = 4 * 1024 * 1024
TRACE_LEDGER_PAGE_SIZE = 48


class TraceKind(StrEnum):
    TURN = "turn"
    STEP = "step"
    MODEL = "model"
    PROVIDER_ATTEMPT = "provider_attempt"
    TOOL_BATCH = "tool_batch"
    TOOL = "tool"
    CONTEXT = "context"
    REPLAN = "replan"
    VERIFICATION = "verification"
    FINALIZER = "finalizer"
    SUBAGENT = "subagent"
    RUNTIME = "runtime"


class TraceStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TraceRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    turn_id: str
    step: int | None
    request_id: str | None
    tool_call_id: str | None
    kind: TraceKind
    name: str
    status: TraceStatus
    started_at: str
    start_offset_ms: float
    started_monotonic: float
    duration_ms: float | None = None
    ttft_ms: float | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_reuse_ratio: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "turn_id": self.turn_id,
            "step": self.step,
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
            "kind": self.kind.value,
            "name": self.name,
            "status": self.status.value,
            "started_at": self.started_at,
            "start_offset_ms": round(self.start_offset_ms, 3),
            "duration_ms": _round_optional(self.duration_ms),
            "ttft_ms": _round_optional(self.ttft_ms),
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_reuse_ratio": _round_optional(self.cache_reuse_ratio),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TraceSummary:
    duration_ms: float | None
    user_visible_ttft_ms: float | None
    model_steps: int
    model_requests: int
    tool_calls: int
    tool_batches: int
    parallel_tool_ratio: float | None
    provider_time_ms: float
    tool_time_ms: float
    permission_wait_ms: float
    context_time_ms: float
    runtime_other_ms: float | None
    weighted_cache_reuse: float | None
    average_ttft_ms: float | None
    max_ttft_ms: float | None
    cache_miss_per_request: float | None
    retries: int
    failovers: int
    replans: int
    compactions: int
    finalizer_calls: int
    longest_model_requests: tuple[tuple[str, float], ...]
    longest_tools: tuple[tuple[str, float], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "duration_ms": _round_optional(self.duration_ms),
            "user_visible_ttft_ms": _round_optional(self.user_visible_ttft_ms),
            "model_steps": self.model_steps,
            "model_requests": self.model_requests,
            "tool_calls": self.tool_calls,
            "tool_batches": self.tool_batches,
            "parallel_tool_ratio": _round_optional(self.parallel_tool_ratio),
            "provider_time_ms": round(self.provider_time_ms, 3),
            "tool_time_ms": round(self.tool_time_ms, 3),
            "permission_wait_ms": round(self.permission_wait_ms, 3),
            "context_time_ms": round(self.context_time_ms, 3),
            "runtime_other_ms": _round_optional(self.runtime_other_ms),
            "weighted_cache_reuse": _round_optional(self.weighted_cache_reuse),
            "average_ttft_ms": _round_optional(self.average_ttft_ms),
            "max_ttft_ms": _round_optional(self.max_ttft_ms),
            "cache_miss_per_request": _round_optional(self.cache_miss_per_request),
            "retries": self.retries,
            "failovers": self.failovers,
            "replans": self.replans,
            "compactions": self.compactions,
            "finalizer_calls": self.finalizer_calls,
            "longest_model_requests": [
                {"name": name, "duration_ms": round(duration, 3)}
                for name, duration in self.longest_model_requests
            ],
            "longest_tools": [
                {"name": name, "duration_ms": round(duration, 3)}
                for name, duration in self.longest_tools
            ],
        }


@dataclass(frozen=True, slots=True)
class TraceSnapshot:
    trace_id: str
    turn_id: str
    source: str
    parent_trace_id: str | None
    status: TraceStatus
    started_at: str
    duration_ms: float | None
    records: tuple[TraceRecord, ...]
    summary: TraceSummary
    dropped_records: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "trace_id": self.trace_id,
            "turn_id": self.turn_id,
            "source": self.source,
            "parent_trace_id": self.parent_trace_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "duration_ms": _round_optional(self.duration_ms),
            "summary": self.summary.to_dict(),
            "dropped_records": self.dropped_records,
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(slots=True)
class _ToolTiming:
    span_id: str
    requested_at: float
    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: float | None = None


@dataclass(slots=True)
class _Turn:
    trace_id: str
    turn_id: str
    source: str
    parent_trace_id: str | None
    started_monotonic: float
    started_at: str
    turn_span_id: str
    user_visible_ttft_ms: float | None = None
    records: list[TraceRecord] = field(default_factory=list)
    status: TraceStatus = TraceStatus.RUNNING
    ended_monotonic: float | None = None
    active_step_id: str | None = None
    active_step_number: int | None = None
    active_batch_id: str | None = None
    batch_started_at: float | None = None
    tool_timings: dict[str, _ToolTiming] = field(default_factory=dict)
    request_spans: dict[str, str] = field(default_factory=dict)
    pending_compaction_id: str | None = None
    dropped_records: int = 0


def _round_optional(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _reconstruct_child_start(
    parent: TraceRecord,
    *,
    ended_monotonic: float,
    duration_ms: float | None,
) -> tuple[float, datetime, float | None]:
    if duration_ms is None:
        started_monotonic = max(parent.started_monotonic, ended_monotonic)
        effective_duration_ms = None
    else:
        started_monotonic = max(
            parent.started_monotonic,
            ended_monotonic - duration_ms / 1000,
        )
        effective_duration_ms = max(0.0, (ended_monotonic - started_monotonic) * 1000)
    parent_started_at = datetime.fromisoformat(parent.started_at)
    started_at = parent_started_at + timedelta(seconds=started_monotonic - parent.started_monotonic)
    return started_monotonic, started_at, effective_duration_ms


def _safe_label(value: object, *, fallback: str = "unknown") -> str:
    if not isinstance(value, str) or not value:
        return fallback
    redacted = redact_sensitive_text(value)
    normalized = re.sub(r"[^A-Za-z0-9_.:/-]+", "_", redacted).strip("_./:")
    return normalized[:MAX_RECORD_NAME_CHARS] or fallback


def _int_value(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _float_value(data: Mapping[str, Any], key: str) -> float | None:
    value = data.get(key)
    return (
        value
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
        else None
    )


def _status_value(value: object) -> TraceStatus:
    if isinstance(value, str) and value in {"completed", "succeeded", "success"}:
        return TraceStatus.SUCCEEDED
    if isinstance(value, str) and value in {"cancelled", "canceled", "abandoned"}:
        return TraceStatus.CANCELLED
    if isinstance(value, str) and value in {"failed", "failure", "error"}:
        return TraceStatus.FAILED
    return TraceStatus.UNKNOWN


class TraceCollector:
    """In-memory bounded trace collector with best-effort event reduction."""

    __slots__ = (
        "_active",
        "_clock",
        "_max_per_turn",
        "_max_records",
        "_max_turns",
        "_total_records",
        "_turns",
    )

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        max_turns: int = MAX_TRACE_TURNS,
        max_records: int = MAX_TRACE_RECORDS,
        max_records_per_turn: int = MAX_RECORDS_PER_TURN,
    ) -> None:
        if min(max_turns, max_records, max_records_per_turn) <= 0:
            raise ValueError("trace retention bounds must be positive")
        self._clock = clock
        self._turns: deque[_Turn] = deque()
        self._active: _Turn | None = None
        self._max_turns = max_turns
        self._max_records = max_records
        self._max_per_turn = max_records_per_turn
        self._total_records = 0

    @property
    def has_turns(self) -> bool:
        return bool(self._turns)

    def begin_turn(
        self,
        *,
        turn_id: str | None = None,
        source: str = "user",
        parent_trace_id: str | None = None,
    ) -> str:
        now = self._clock()
        if self._active is not None:
            self._finish_turn(self._active, TraceStatus.CANCELLED, now=now)
        trace_id = uuid.uuid4().hex
        resolved_turn_id = _safe_label(turn_id, fallback=f"turn-{uuid.uuid4().hex[:12]}")
        turn_span_id = uuid.uuid4().hex
        turn = _Turn(
            trace_id=trace_id,
            turn_id=resolved_turn_id,
            source=_safe_label(source, fallback="runtime"),
            parent_trace_id=_safe_label(parent_trace_id) if parent_trace_id else None,
            started_monotonic=now,
            started_at=datetime.now(UTC).isoformat(),
            turn_span_id=turn_span_id,
        )
        self._turns.append(turn)
        self._active = turn
        self._append(
            turn,
            TraceKind.TURN,
            turn.source,
            span_id=turn_span_id,
            parent_span_id=None,
            status=TraceStatus.RUNNING,
            now=now,
        )
        self._trim()
        return trace_id

    def end_turn(self, status: str = "succeeded") -> None:
        turn = self._active
        if turn is not None:
            self._finish_turn(turn, _status_value(status), now=self._clock())

    def observe(self, event: AgentEvent) -> None:
        """Consume one event without letting tracing affect its producer."""

        try:
            self._observe(event)
        except Exception:
            # Deliberately fail open. No raw event data is logged.
            if self._active is not None:
                self._active.dropped_records += 1

    def _observe(self, event: AgentEvent) -> None:
        now = self._clock()
        if self._active is None and event.kind in {
            AgentEventKind.MODEL_STEP_STARTED,
            AgentEventKind.MODEL_REQUEST_SNAPSHOT,
            AgentEventKind.TOOL_REQUESTED,
        }:
            self.begin_turn(source="runtime")
        turn = self._active
        if turn is None:
            return
        data = event.data
        if event.kind is AgentEventKind.MODEL_STEP_STARTED:
            self._finish_step(turn, now=now)
            step_number = _int_value(data, "step") or (
                1 if turn.active_step_number is None else turn.active_step_number + 1
            )
            turn.active_step_number = step_number
            turn.active_step_id = self._append(
                turn,
                TraceKind.STEP,
                f"Step {step_number}",
                parent_span_id=turn.turn_span_id,
                step=step_number,
                now=now,
            )
            return
        if event.kind is AgentEventKind.TEXT_DELTA:
            text = data.get("text")
            if isinstance(text, str) and text and turn.user_visible_ttft_ms is None:
                turn.user_visible_ttft_ms = max(0.0, (now - turn.started_monotonic) * 1000)
            return
        if event.kind is AgentEventKind.MODEL_REQUEST_SNAPSHOT:
            request_id = _safe_label(data.get("request_id"))
            if request_id == "unknown":
                return
            provider = _safe_label(data.get("provider"))
            model = _safe_label(data.get("model"))
            span_id = self._append(
                turn,
                TraceKind.MODEL,
                f"{provider}/{model}",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=_int_value(data, "step") or turn.active_step_number,
                request_id=request_id,
                provider=provider,
                model=model,
                metadata={
                    "message_count": _int_value(data, "message_count"),
                    "tool_count": _int_value(data, "tool_count"),
                },
                now=now,
            )
            turn.request_spans[request_id] = span_id
            return
        if event.kind is AgentEventKind.MODEL_REQUEST_TRAJECTORY:
            request_id = _safe_label(data.get("request_id"), fallback="")
            request_span_id = turn.request_spans.get(request_id)
            if request_span_id is not None:
                metadata = self._find(turn, request_span_id).metadata
                self._update(
                    turn,
                    request_span_id,
                    metadata={
                        **metadata,
                        "request_source": _safe_label(data.get("source")),
                        "context_generation": _int_value(data, "context_generation"),
                        "cache_epoch": _int_value(data, "cache_epoch"),
                        "cache_boundary_reason": _safe_label(
                            data.get("boundary_reason"), fallback="none"
                        ),
                        "append_only": data.get("append_only")
                        if isinstance(data.get("append_only"), bool)
                        else None,
                        "common_prefix_messages": _int_value(data, "common_prefix_messages"),
                        "message_count": _int_value(data, "message_count"),
                        "tool_count": _int_value(data, "tool_count"),
                    },
                )
            return
        if event.kind is AgentEventKind.RUNTIME_TRACE_MODEL_REQUEST:
            self._record_model_trace(turn, data, now=now)
            return
        if event.kind is AgentEventKind.PROVIDER_ATTEMPT_FAILED:
            self._record_provider_failure(turn, data, now=now)
            return
        if event.kind is AgentEventKind.TOOL_REQUESTED:
            self._tool_requested(turn, data, now=now)
            return
        if event.kind is AgentEventKind.TOOL_STARTED:
            self._tool_started(turn, data, now=now)
            return
        if event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}:
            self._tool_finished(
                turn, data, now=now, failed=event.kind is AgentEventKind.TOOL_FAILED
            )
            return
        if event.kind is AgentEventKind.RUNTIME_TRACE_CONTEXT_BUILD:
            self._append(
                turn,
                TraceKind.CONTEXT,
                "Context build",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=turn.active_step_number,
                status=TraceStatus.SUCCEEDED,
                duration_ms=_duration_ms(data),
                metadata=_safe_metadata(
                    data,
                    allow={
                        "item_count",
                        "estimated_tokens",
                        "context_generation",
                        "cache_epoch",
                        "request_source",
                    },
                ),
                now=now,
            )
            return
        if event.kind is AgentEventKind.RUNTIME_TRACE_CONTEXT_ROLLOVER:
            self._append(
                turn,
                TraceKind.CONTEXT,
                "Fresh Context Rollover",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=turn.active_step_number,
                status=TraceStatus.SUCCEEDED,
                metadata=_safe_metadata(data, allow={"context_generation", "reason"}),
                now=now,
            )
            return
        if event.kind is AgentEventKind.CONTEXT_PREFLIGHT:
            micro = data.get("microcompaction")
            metadata = _safe_metadata(
                data,
                allow={
                    "status",
                    "estimated_input_tokens",
                    "estimated_total_tokens",
                    "capacity_tokens",
                    "request_source",
                    "context_generation",
                    "cache_epoch",
                    "automatic_rollover_attempted",
                    "automatic_rollover_succeeded",
                    "automatic_rollover_eligible",
                },
            )
            if isinstance(micro, Mapping):
                metadata["microcompaction"] = _safe_metadata(
                    micro,
                    allow={
                        "trigger_reason",
                        "groups_compacted",
                        "results_compacted",
                        "estimated_bytes_before",
                        "estimated_bytes_after",
                        "estimated_bytes_saved",
                        "estimated_tokens_before",
                        "estimated_tokens_after",
                        "estimated_tokens_saved",
                        "noop_reason",
                        "estimates_saturated",
                    },
                )
            self._append(
                turn,
                TraceKind.CONTEXT,
                "Context preflight",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=turn.active_step_number,
                status=TraceStatus.SUCCEEDED,
                metadata=metadata,
                now=now,
            )
            if data.get("automatic_rollover_succeeded") is True:
                self._append(
                    turn,
                    TraceKind.CONTEXT,
                    "Fresh Context Rollover",
                    parent_span_id=turn.active_step_id or turn.turn_span_id,
                    step=turn.active_step_number,
                    status=TraceStatus.SUCCEEDED,
                    metadata={"context_generation": _int_value(data, "context_generation")},
                    now=now,
                )
            return
        if event.kind is AgentEventKind.CONTEXT_COMPACTION_STARTED:
            span_id = self._append(
                turn,
                TraceKind.CONTEXT,
                "Full Compaction",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=turn.active_step_number,
                metadata=_safe_metadata(
                    data,
                    allow={"safe_point", "decision", "source_item_count", "candidate_item_count"},
                ),
                now=now,
            )
            turn.pending_compaction_id = span_id
            return
        if event.kind is AgentEventKind.CONTEXT_COMPACTION_COMPLETED:
            pending_span_id = turn.pending_compaction_id
            metadata = _safe_metadata(
                data,
                allow={
                    "safe_point",
                    "source_item_count",
                    "candidate_item_count",
                    "summary_tokens",
                    "summary_truncated",
                },
            )
            if pending_span_id is not None:
                self._update(
                    turn,
                    pending_span_id,
                    status=TraceStatus.SUCCEEDED,
                    duration_ms=(now - self._find(turn, pending_span_id).started_monotonic) * 1000,
                    metadata=metadata,
                )
                turn.pending_compaction_id = None
            else:
                self._append(
                    turn,
                    TraceKind.CONTEXT,
                    "Full Compaction",
                    parent_span_id=turn.active_step_id or turn.turn_span_id,
                    step=turn.active_step_number,
                    status=TraceStatus.SUCCEEDED,
                    metadata=metadata,
                    now=now,
                )
            return
        if event.kind is AgentEventKind.RUNTIME_TRACE_REPLAN:
            self._append(
                turn,
                TraceKind.REPLAN,
                _safe_label(data.get("state"), fallback="Replan"),
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=_int_value(data, "step") or turn.active_step_number,
                status=TraceStatus.SUCCEEDED,
                metadata=_safe_metadata(
                    data,
                    allow={
                        "state",
                        "reason_code",
                        "replan_count",
                        "cycle_period",
                        "progress_since_replan",
                        "resolved",
                    },
                ),
                now=now,
            )
            return
        if event.kind is AgentEventKind.RUNTIME_TRACE_VERIFICATION:
            self._append(
                turn,
                TraceKind.VERIFICATION,
                "Verification",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=turn.active_step_number,
                status=_status_value(data.get("status")),
                duration_ms=_duration_ms(data),
                metadata=_safe_metadata(data, allow={"status", "error_type", "exit_code"}),
                now=now,
            )
            return
        if event.kind is AgentEventKind.RUNTIME_TRACE_FINALIZER:
            self._append(
                turn,
                TraceKind.FINALIZER,
                "Finalizer",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                step=turn.active_step_number,
                status=_status_value(data.get("status")),
                duration_ms=_duration_ms(data),
                input_tokens=_int_value(data, "input_tokens"),
                output_tokens=_int_value(data, "output_tokens"),
                metadata=_safe_metadata(data, allow={"status", "attempts", "error_type", "source"}),
                now=now,
            )
            return
        if event.kind is AgentEventKind.RUNTIME_TRACE_SUBAGENT:
            duration_ms = _duration_ms(data)
            elapsed_seconds = (duration_ms or 0.0) / 1000
            self._append(
                turn,
                TraceKind.SUBAGENT,
                "Subagent",
                parent_span_id=turn.turn_span_id,
                status=_status_value(data.get("status")),
                duration_ms=duration_ms,
                metadata=_safe_metadata(
                    data, allow={"status", "steps", "child_session_id", "task_id", "cancelled"}
                ),
                now=now - elapsed_seconds,
                started_at=datetime.now(UTC) - timedelta(seconds=elapsed_seconds),
            )
            return
        if event.kind in {
            AgentEventKind.TURN_COMPLETED,
            AgentEventKind.TURN_FAILED,
            AgentEventKind.TURN_ABANDONED,
        }:
            reported_turn_id = data.get("turn_id")
            if isinstance(reported_turn_id, str) and reported_turn_id:
                turn.turn_id = _safe_label(reported_turn_id, fallback=turn.turn_id)
                self._synchronize_turn_identity(turn)
            status = (
                TraceStatus.SUCCEEDED
                if event.kind is AgentEventKind.TURN_COMPLETED
                else TraceStatus.CANCELLED
                if event.kind is AgentEventKind.TURN_ABANDONED
                else TraceStatus.FAILED
            )
            self._finish_turn(turn, status, now=now)

    def snapshot(self, trace_id: str | None = None) -> TraceSnapshot | None:
        turn = next(
            (
                item
                for item in reversed(self._turns)
                if trace_id is None or item.trace_id == trace_id
            ),
            None,
        )
        if turn is None:
            return None
        now = turn.ended_monotonic if turn.ended_monotonic is not None else self._clock()
        duration_ms = max(0.0, (now - turn.started_monotonic) * 1000)
        records = tuple(turn.records)
        return TraceSnapshot(
            trace_id=turn.trace_id,
            turn_id=turn.turn_id,
            source=turn.source,
            parent_trace_id=turn.parent_trace_id,
            status=turn.status,
            started_at=turn.started_at,
            duration_ms=duration_ms if turn.ended_monotonic is not None else None,
            records=records,
            summary=_summarize(
                records,
                duration_ms if turn.ended_monotonic is not None else None,
                user_visible_ttft_ms=turn.user_visible_ttft_ms,
            ),
            dropped_records=turn.dropped_records,
        )

    def snapshots(self) -> tuple[TraceSnapshot, ...]:
        return tuple(
            snapshot
            for turn in self._turns
            if (snapshot := self.snapshot(turn.trace_id)) is not None
        )

    def export_json(self, *, trace_id: str | None = None, jsonl: bool = False) -> str:
        snapshots = (self.snapshot(trace_id),) if trace_id is not None else self.snapshots()
        values = tuple(snapshot.to_dict() for snapshot in snapshots if snapshot is not None)
        if jsonl:
            content = "\n".join(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) for value in values
            )
        else:
            content = json.dumps(
                {"schema_version": 1, "traces": values}, ensure_ascii=False, indent=2
            )
        if len(content.encode("utf-8")) > MAX_TRACE_EXPORT_BYTES:
            raise ValueError("trace export exceeds the metadata export limit")
        return content

    def add_subagent(
        self,
        *,
        duration_ms: float,
        status: str,
        steps: int | None = None,
        task_id: str | None = None,
        child_session_id: str | None = None,
    ) -> None:
        data: dict[str, object] = {"duration_ms": duration_ms, "status": status}
        if steps is not None:
            data["steps"] = steps
        if task_id is not None:
            data["task_id"] = _safe_label(task_id)
        if child_session_id is not None:
            data["child_session_id"] = _safe_label(child_session_id)
        self.observe(AgentEvent.create(1, AgentEventKind.RUNTIME_TRACE_SUBAGENT, data))

    def _record_model_trace(self, turn: _Turn, data: Mapping[str, Any], *, now: float) -> None:
        request_id = _safe_label(data.get("request_id"))
        span_id = turn.request_spans.get(request_id)
        if span_id is None:
            request_duration_ms = _duration_ms(data)
            request_duration_seconds = (request_duration_ms or 0.0) / 1000
            request_started_monotonic = now - request_duration_seconds
            span_id = self._append(
                turn,
                TraceKind.MODEL,
                f"{_safe_label(data.get('provider'))}/{_safe_label(data.get('model'))}",
                parent_span_id=turn.active_step_id or turn.turn_span_id,
                request_id=request_id if request_id != "unknown" else None,
                step=_int_value(data, "step") or turn.active_step_number,
                now=request_started_monotonic,
                started_at=datetime.now(UTC) - timedelta(seconds=request_duration_seconds),
            )
            if request_id != "unknown":
                turn.request_spans[request_id] = span_id
        current = self._find(turn, span_id)
        metadata = dict(current.metadata)
        trace_metadata = _safe_metadata(
            data,
            allow={
                "source",
                "context_generation",
                "cache_epoch",
                "cache_boundary_reason",
                "append_only",
                "common_prefix_messages",
                "capacity_tokens",
                "estimated_context_tokens",
                "status",
                "output_kind",
                "error_type",
                "retry_count",
                "failover_count",
                "stream_duration_ms",
                "provider_attempts",
            },
        )
        estimated_context_tokens = _int_value(data, "estimated_context_tokens")
        if estimated_context_tokens is None:
            context_record = next(
                (
                    record
                    for record in reversed(turn.records)
                    if record.kind is TraceKind.CONTEXT
                    and record.parent_span_id == current.parent_span_id
                ),
                None,
            )
            if context_record is not None:
                value = context_record.metadata.get("estimated_tokens")
                if isinstance(value, int) and not isinstance(value, bool):
                    estimated_context_tokens = value
        if estimated_context_tokens is not None:
            trace_metadata["estimated_context_tokens"] = estimated_context_tokens
        metadata.update(trace_metadata)
        self._update(
            turn,
            span_id,
            status=_status_value(data.get("status")),
            duration_ms=max(0.0, (now - current.started_monotonic) * 1000),
            ttft_ms=(_float_value(data, "ttft_ms") if "ttft_ms" in data else current.ttft_ms),
            provider=(
                _safe_label(data.get("provider")) if "provider" in data else current.provider
            ),
            model=_safe_label(data.get("model")) if "model" in data else current.model,
            input_tokens=_int_value(data, "input_tokens"),
            output_tokens=_int_value(data, "output_tokens"),
            cache_read_tokens=_int_value(data, "cache_read_tokens"),
            cache_write_tokens=_int_value(data, "cache_write_tokens"),
            cache_miss_tokens=_int_value(data, "cache_miss_tokens"),
            cache_reuse_ratio=_float_value(data, "cache_reuse_ratio"),
            metadata=metadata,
        )
        attempts = data.get("provider_attempts")
        if isinstance(attempts, tuple | list):
            for item in attempts[:8]:
                if not isinstance(item, Mapping):
                    continue
                attempt_provider = _safe_label(item.get("provider"))
                attempt_model = _safe_label(item.get("model"))
                attempt_status = _status_value(item.get("status"))
                attempt_duration = _float_value(item, "duration_ms")
                attempt_index = _int_value(item, "attempt_index")
                if any(
                    existing.kind is TraceKind.PROVIDER_ATTEMPT
                    and existing.parent_span_id == span_id
                    and (
                        existing.metadata.get("attempt_index") == attempt_index
                        if attempt_index is not None
                        else existing.provider == attempt_provider
                        and existing.model == attempt_model
                        and existing.status is attempt_status
                        and existing.duration_ms == attempt_duration
                    )
                    for existing in turn.records
                ):
                    continue
                attempt_started_monotonic, attempt_started_at, effective_duration_ms = (
                    _reconstruct_child_start(
                        current,
                        ended_monotonic=now,
                        duration_ms=attempt_duration,
                    )
                )
                self._append(
                    turn,
                    TraceKind.PROVIDER_ATTEMPT,
                    f"{attempt_provider}/{attempt_model}",
                    parent_span_id=span_id,
                    step=_int_value(data, "step") or turn.active_step_number,
                    provider=attempt_provider,
                    model=attempt_model,
                    status=attempt_status,
                    duration_ms=effective_duration_ms,
                    now=attempt_started_monotonic,
                    started_at=attempt_started_at,
                    metadata=_safe_metadata(
                        item,
                        allow={
                            "attempt_index",
                            "status",
                            "error_type",
                            "failure_kind",
                            "status_code",
                            "failover",
                        },
                    ),
                )

    def _record_provider_failure(self, turn: _Turn, data: Mapping[str, Any], *, now: float) -> None:
        request_id = _safe_label(data.get("request_id"))
        parent = (
            turn.request_spans.get(request_id)
            or self._active_request_span(turn)
            or turn.active_step_id
            or turn.turn_span_id
        )
        duration_ms = _duration_ms(data)
        parent_record = self._find(turn, parent)
        attempt_started_monotonic, attempt_started_at, effective_duration_ms = (
            _reconstruct_child_start(
                parent_record,
                ended_monotonic=now,
                duration_ms=duration_ms,
            )
        )
        attempt_index = sum(
            1
            for record in turn.records
            if record.kind is TraceKind.PROVIDER_ATTEMPT and record.parent_span_id == parent
        )
        metadata = _safe_metadata(
            data,
            allow={"error_type", "failure_kind", "status_code"},
        )
        metadata["attempt_index"] = attempt_index
        self._append(
            turn,
            TraceKind.PROVIDER_ATTEMPT,
            f"{_safe_label(data.get('provider'))}/{_safe_label(data.get('model'))}",
            parent_span_id=parent,
            step=turn.active_step_number,
            provider=_safe_label(data.get("provider")),
            model=_safe_label(data.get("model")),
            status=TraceStatus.FAILED,
            duration_ms=effective_duration_ms,
            metadata=metadata,
            now=attempt_started_monotonic,
            started_at=attempt_started_at,
        )

    def _tool_requested(self, turn: _Turn, data: Mapping[str, Any], *, now: float) -> None:
        call_id = _safe_label(data.get("id"))
        if call_id == "unknown":
            return
        if turn.active_step_id is None:
            turn.active_step_id = self._append(
                turn,
                TraceKind.STEP,
                "Tool step",
                parent_span_id=turn.turn_span_id,
                step=turn.active_step_number,
                now=now,
            )
        if turn.active_batch_id is None:
            turn.batch_started_at = now
            turn.active_batch_id = self._append(
                turn,
                TraceKind.TOOL_BATCH,
                "Tool batch",
                parent_span_id=turn.active_step_id,
                step=turn.active_step_number,
                now=now,
            )
        span_id = self._append(
            turn,
            TraceKind.TOOL,
            _safe_label(data.get("name")),
            parent_span_id=turn.active_batch_id,
            step=turn.active_step_number,
            tool_call_id=call_id,
            now=now,
        )
        turn.tool_timings[call_id] = _ToolTiming(span_id, now)

    def _tool_started(self, turn: _Turn, data: Mapping[str, Any], *, now: float) -> None:
        call_id = _safe_label(data.get("id"))
        timing = turn.tool_timings.get(call_id)
        if timing is None:
            self._tool_requested(turn, data, now=now)
            timing = turn.tool_timings.get(call_id)
        if timing is not None:
            timing.started_at = now
            self._update(
                turn,
                timing.span_id,
                metadata={"permission_wait_ms": max(0.0, (now - timing.requested_at) * 1000)},
            )

    def _tool_finished(
        self, turn: _Turn, data: Mapping[str, Any], *, now: float, failed: bool
    ) -> None:
        call_id = _safe_label(data.get("id"))
        timing = turn.tool_timings.get(call_id)
        if timing is None:
            self._tool_requested(turn, data, now=now)
            timing = turn.tool_timings.get(call_id)
        if timing is None:
            return
        timing.completed_at = now
        total_ms = max(0.0, (now - timing.requested_at) * 1000)
        execution_ms = (
            max(0.0, (now - timing.started_at) * 1000) if timing.started_at is not None else 0.0
        )
        timing.duration_ms = execution_ms
        projection = data.get("model_context_projection")
        metadata: dict[str, object] = {
            "permission_wait_ms": round(
                max(0.0, (timing.started_at or now) - timing.requested_at) * 1000, 3
            ),
            "execution_ms": round(execution_ms, 3),
            "execution_offset_ms": round(
                max(0.0, ((timing.started_at or now) - turn.started_monotonic) * 1000), 3
            ),
            "result_bytes": _result_byte_count(data),
            "cancelled": data.get("cancelled") is True,
            "not_started": data.get("not_started") is True,
        }
        if isinstance(projection, Mapping):
            metadata.update(
                _safe_metadata(
                    projection,
                    allow={
                        "truncated",
                        "original_bytes",
                        "projected_bytes",
                        "artifact_available",
                        "strategy",
                    },
                )
            )
        self._update(
            turn,
            timing.span_id,
            status=TraceStatus.CANCELLED
            if data.get("cancelled") is True
            else TraceStatus.FAILED
            if failed
            else TraceStatus.SUCCEEDED,
            duration_ms=total_ms,
            metadata=metadata,
        )

    def _finish_step(self, turn: _Turn, *, now: float) -> None:
        if turn.active_batch_id is not None:
            self._finish_batch(turn, now=now)
        if turn.active_step_id is not None:
            record = self._find(turn, turn.active_step_id)
            if record.status is TraceStatus.RUNNING:
                self._update(
                    turn,
                    record.span_id,
                    status=TraceStatus.SUCCEEDED,
                    duration_ms=max(0.0, (now - record.started_monotonic) * 1000),
                )
        turn.active_step_id = None

    def _finish_batch(self, turn: _Turn, *, now: float) -> None:
        batch_id = turn.active_batch_id
        if batch_id is None:
            return
        records = [
            record
            for record in turn.records
            if record.parent_span_id == batch_id and record.kind is TraceKind.TOOL
        ]
        intervals = [
            (timing.started_at, timing.completed_at)
            for timing in turn.tool_timings.values()
            if timing.started_at is not None
            and timing.completed_at is not None
            and any(record.span_id == timing.span_id for record in records)
        ]
        has_overlap = any(
            left[1] is not None and right[0] is not None and left[1] > right[0]
            for index, left in enumerate(intervals)
            for right in intervals[index + 1 :]
        )
        wall_ms = max(0.0, (now - (turn.batch_started_at or now)) * 1000)
        sum_child_ms = sum(
            timing.duration_ms or 0.0
            for timing in turn.tool_timings.values()
            if any(record.span_id == timing.span_id for record in records)
        )
        self._update(
            turn,
            batch_id,
            status=TraceStatus.SUCCEEDED
            if all(record.status is not TraceStatus.FAILED for record in records)
            else TraceStatus.FAILED,
            duration_ms=wall_ms,
            metadata={
                "child_tool_count": len(records),
                "parallel": has_overlap,
                "exclusive": not has_overlap,
                "batch_wall_ms": round(wall_ms, 3),
                "sum_child_execution_ms": round(sum_child_ms, 3),
            },
        )
        turn.active_batch_id = None
        turn.batch_started_at = None

    def _finish_turn(
        self,
        turn: _Turn,
        status: TraceStatus,
        *,
        now: float,
    ) -> None:
        self._finish_step(turn, now=now)
        self._synchronize_turn_identity(turn)
        for record in tuple(turn.records):
            if record.kind is TraceKind.MODEL and record.status is TraceStatus.RUNNING:
                self._update(
                    turn,
                    record.span_id,
                    status=status,
                    duration_ms=max(0.0, (now - record.started_monotonic) * 1000),
                )
        turn.status = status
        turn.ended_monotonic = now
        duration_ms = max(0.0, (now - turn.started_monotonic) * 1000)
        self._update(turn, turn.turn_span_id, status=status, duration_ms=duration_ms)
        if turn is self._active:
            self._active = None

    def _append(
        self,
        turn: _Turn,
        kind: TraceKind,
        name: str,
        *,
        parent_span_id: str | None,
        step: int | None = None,
        request_id: str | None = None,
        tool_call_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        status: TraceStatus = TraceStatus.RUNNING,
        duration_ms: float | None = None,
        ttft_ms: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        cache_miss_tokens: int | None = None,
        cache_reuse_ratio: float | None = None,
        metadata: Mapping[str, object] | None = None,
        span_id: str | None = None,
        now: float | None = None,
        started_at: datetime | None = None,
    ) -> str:
        moment = self._clock() if now is None else now
        identifier = span_id or uuid.uuid4().hex
        turn.records.append(
            TraceRecord(
                trace_id=turn.trace_id,
                span_id=identifier,
                parent_span_id=parent_span_id,
                turn_id=turn.turn_id,
                step=step,
                request_id=request_id,
                tool_call_id=tool_call_id,
                kind=kind,
                name=_safe_label(name),
                status=status,
                started_at=(started_at or datetime.now(UTC)).isoformat(),
                start_offset_ms=max(0.0, (moment - turn.started_monotonic) * 1000),
                started_monotonic=moment,
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_miss_tokens=cache_miss_tokens,
                cache_reuse_ratio=cache_reuse_ratio,
                metadata=_safe_metadata(metadata or {}, allow=set(metadata or {})),
            )
        )
        self._total_records += 1
        if len(turn.records) > self._max_per_turn:
            turn.records.pop(1 if len(turn.records) > 1 else 0)
            self._total_records -= 1
            turn.dropped_records += 1
        if self._total_records > self._max_records:
            self._trim()
        return identifier

    def _find(self, turn: _Turn, span_id: str) -> TraceRecord:
        return next(record for record in turn.records if record.span_id == span_id)

    @staticmethod
    def _synchronize_turn_identity(turn: _Turn) -> None:
        for index, record in enumerate(turn.records):
            if record.turn_id != turn.turn_id:
                turn.records[index] = replace(record, turn_id=turn.turn_id)

    def _update(self, turn: _Turn, span_id: str, **changes: object) -> None:
        for index, record in enumerate(turn.records):
            if record.span_id == span_id:
                turn.records[index] = replace(record, **cast(Any, changes))
                return

    @staticmethod
    def _active_request_span(turn: _Turn) -> str | None:
        return next(reversed(turn.request_spans.values()), None) if turn.request_spans else None

    def _trim(self) -> None:
        while len(self._turns) > self._max_turns:
            removed = self._turns.popleft()
            self._total_records -= len(removed.records)
            if removed is self._active:
                self._active = None
        while self._total_records > self._max_records:
            turn = next(
                (item for item in self._turns if item is not self._active and item.records), None
            )
            if turn is None:
                turn = next((item for item in self._turns if item.records), None)
            if turn is None:
                break
            turn.records.pop(1 if len(turn.records) > 1 else 0)
            self._total_records -= 1
            turn.dropped_records += 1


def _safe_metadata(data: Mapping[str, Any], *, allow: set[str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in allow:
        value = data.get(key)
        if isinstance(value, str):
            result[key] = _safe_label(value, fallback="") if value else ""
        elif isinstance(value, bool | int | float) or value is None:
            result[key] = value
        elif isinstance(value, tuple | list) and key == "provider_attempts":
            result[key] = tuple(
                _safe_metadata(
                    item,
                    allow={
                        "attempt_index",
                        "provider",
                        "model",
                        "status",
                        "duration_ms",
                        "error_type",
                        "failure_kind",
                        "status_code",
                        "failover",
                    },
                )
                for item in value[:8]
                if isinstance(item, Mapping)
            )
        elif isinstance(value, Mapping) and key == "microcompaction":
            result[key] = _safe_metadata(
                value,
                allow={
                    "trigger_reason",
                    "groups_compacted",
                    "results_compacted",
                    "estimated_bytes_before",
                    "estimated_bytes_after",
                    "estimated_bytes_saved",
                    "estimated_tokens_before",
                    "estimated_tokens_after",
                    "estimated_tokens_saved",
                    "noop_reason",
                    "estimates_saturated",
                },
            )
    return result


def _duration_ms(data: Mapping[str, Any]) -> float | None:
    duration = _float_value(data, "duration_ms")
    if duration is not None:
        return duration
    seconds = _float_value(data, "duration_seconds")
    return seconds * 1000 if seconds is not None else None


def _result_byte_count(data: Mapping[str, Any]) -> int | None:
    content = data.get("content")
    if not isinstance(content, str):
        return None
    return len(content.encode("utf-8"))


def _summarize(
    records: tuple[TraceRecord, ...],
    duration_ms: float | None,
    *,
    user_visible_ttft_ms: float | None = None,
) -> TraceSummary:
    def by_kind(kind: TraceKind) -> tuple[TraceRecord, ...]:
        return tuple(record for record in records if record.kind is kind)

    models = by_kind(TraceKind.MODEL)
    tools = by_kind(TraceKind.TOOL)
    batches = by_kind(TraceKind.TOOL_BATCH)
    ttfts = tuple(record.ttft_ms for record in models if record.ttft_ms is not None)
    cache_misses = tuple(
        record.cache_miss_tokens for record in models if record.cache_miss_tokens is not None
    )
    cache_usage = tuple(
        (record.cache_read_tokens, record.input_tokens)
        for record in models
        if record.cache_read_tokens is not None and record.input_tokens is not None
    )
    parallel_calls = sum(
        1
        for record in tools
        if any(
            parent.span_id == record.parent_span_id and parent.metadata.get("parallel") is True
            for parent in batches
        )
    )
    retry_records = tuple(
        record
        for record in by_kind(TraceKind.PROVIDER_ATTEMPT)
        if record.status is TraceStatus.FAILED
    )
    failover_count = sum(
        value
        for record in models
        if (value := record.metadata.get("failover_count")) is not None
        and isinstance(value, int)
        and not isinstance(value, bool)
    )
    compactions = sum(
        1 for record in by_kind(TraceKind.CONTEXT) if record.name == "Full_Compaction"
    )

    def numeric_metadata(record: TraceRecord, key: str, fallback: float = 0.0) -> float:
        value = record.metadata.get(key)
        return (
            float(value)
            if isinstance(value, int | float) and not isinstance(value, bool)
            else fallback
        )

    tool_time = sum(
        numeric_metadata(record, "execution_ms", record.duration_ms or 0) for record in tools
    )
    permission_wait = sum(numeric_metadata(record, "permission_wait_ms") for record in tools)
    provider_time = sum(record.duration_ms or 0 for record in models)
    context_records = by_kind(TraceKind.CONTEXT)
    context_time = sum(record.duration_ms or 0 for record in context_records)
    active_intervals = [
        (record.start_offset_ms, record.start_offset_ms + (record.duration_ms or 0))
        for record in models
        if record.duration_ms is not None
    ]
    for record in tools:
        permission_wait_ms = numeric_metadata(record, "permission_wait_ms", -1)
        if permission_wait_ms > 0:
            active_intervals.append(
                (record.start_offset_ms, record.start_offset_ms + permission_wait_ms)
            )
        execution_ms = numeric_metadata(record, "execution_ms", -1)
        if execution_ms >= 0:
            execution_offset_ms = numeric_metadata(
                record, "execution_offset_ms", record.start_offset_ms
            )
            active_intervals.append((execution_offset_ms, execution_offset_ms + execution_ms))
    for record in context_records:
        if record.duration_ms is not None:
            active_intervals.append(
                (record.start_offset_ms, record.start_offset_ms + record.duration_ms)
            )
    active_ms = 0.0
    if active_intervals:
        active_intervals.sort()
        interval_start, interval_end = active_intervals[0]
        for start, end in active_intervals[1:]:
            if start <= interval_end:
                interval_end = max(interval_end, end)
            else:
                active_ms += max(0.0, interval_end - interval_start)
                interval_start, interval_end = start, end
        active_ms += max(0.0, interval_end - interval_start)
    runtime_other = max(0.0, duration_ms - active_ms) if duration_ms is not None else None
    return TraceSummary(
        duration_ms=duration_ms,
        user_visible_ttft_ms=user_visible_ttft_ms,
        model_steps=len(by_kind(TraceKind.STEP)),
        model_requests=len(models),
        tool_calls=len(tools),
        tool_batches=len(batches),
        parallel_tool_ratio=parallel_calls / len(tools) if tools else None,
        provider_time_ms=provider_time,
        tool_time_ms=tool_time,
        permission_wait_ms=permission_wait,
        context_time_ms=context_time,
        runtime_other_ms=runtime_other,
        weighted_cache_reuse=(
            sum(cache_read for cache_read, _ in cache_usage)
            / sum(input_tokens for _, input_tokens in cache_usage)
            if cache_usage and sum(input_tokens for _, input_tokens in cache_usage) > 0
            else None
        ),
        average_ttft_ms=sum(ttfts) / len(ttfts) if ttfts else None,
        max_ttft_ms=max(ttfts) if ttfts else None,
        cache_miss_per_request=sum(cache_misses) / len(cache_misses) if cache_misses else None,
        retries=len(retry_records),
        failovers=failover_count,
        replans=len(by_kind(TraceKind.REPLAN)),
        compactions=compactions,
        finalizer_calls=len(by_kind(TraceKind.FINALIZER)),
        longest_model_requests=tuple(
            (record.name, record.duration_ms or 0.0)
            for record in sorted(models, key=lambda item: item.duration_ms or 0, reverse=True)[:5]
        ),
        longest_tools=tuple(
            (record.name, record.duration_ms or 0.0)
            for record in sorted(tools, key=lambda item: item.duration_ms or 0, reverse=True)[:5]
        ),
    )


__all__ = [
    "MAX_RECORDS_PER_TURN",
    "MAX_TRACE_EXPORT_BYTES",
    "MAX_TRACE_RECORDS",
    "MAX_TRACE_TURNS",
    "TRACE_LEDGER_PAGE_SIZE",
    "TraceCollector",
    "TraceKind",
    "TraceRecord",
    "TraceSnapshot",
    "TraceStatus",
    "TraceSummary",
]
