"""Per-step model stream processor.

Stage 3E of the Runtime Kernel split: this module owns the normalization of
one provider stream into step text, reasoning, tool calls, and completion
state.  It also owns provider-origin adoption bookkeeping, thinking-completion
timing, and pristine cancel-eligibility updates for the current step.

The module intentionally does not import :mod:`agent`; it depends only on
ports, domain values, and callbacks supplied by the loop.

提供逐模型步骤的流处理器,负责规范化文本、推理、工具调用和完成状态.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelBackendToolCompleted,
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
    ModelProviderAttemptFailed,
    ModelProviderSelected,
    ModelReasoningDelta,
    ModelRequestTrajectoryObserved,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.shared.errors import ProviderError

EventSink = Callable[[AgentEventKind, dict[str, object]], Awaitable[AgentEvent]]
DiagnosticSink = Callable[[AgentEventKind, dict[str, object]], Awaitable[None] | None]

MAX_BUFFERED_MODEL_STEP_TEXT_BYTES = 256 * 1024
MAX_BUFFERED_MODEL_STEP_TEXT_CHUNKS = 4096


class _BoundedStepTextBuffer:
    """Keep gated model text finite until the step's tool shape is known."""

    __slots__ = ("_bytes", "_chunks", "_values")

    def __init__(self) -> None:
        self._bytes = 0
        self._chunks = 0
        self._values: list[str] = []

    def append(self, value: str) -> None:
        value_bytes = len(value.encode("utf-8"))
        if self._chunks >= MAX_BUFFERED_MODEL_STEP_TEXT_CHUNKS:
            raise ProviderError("gated model step text buffer exceeded its chunk limit")
        if self._bytes + value_bytes > MAX_BUFFERED_MODEL_STEP_TEXT_BYTES:
            raise ProviderError("gated model step text buffer exceeded its byte limit")
        self._values.append(value)
        self._chunks += 1
        self._bytes += value_bytes

    def values(self) -> tuple[str, ...]:
        return tuple(self._values)

    @staticmethod
    def validate(value: str) -> None:
        if len(value.encode("utf-8")) > MAX_BUFFERED_MODEL_STEP_TEXT_BYTES:
            raise ProviderError("gated model step text buffer exceeded its byte limit")


@dataclass(frozen=True, slots=True)
class ModelStepResult:
    """Normalized state produced by consuming one provider stream.

    表示消费一次 Provider 流后生成的规范化状态."""

    text: tuple[str, ...]
    reasoning: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    completion: ModelCompleted | None
    selected_provider: ModelProviderSelected | None = None


class ModelStepProcessor:
    """Consume one provider stream and normalize events into step state.

    消费一次 Provider 流,并将事件规范化为模型步骤状态."""

    __slots__ = ("_session_store",)

    def __init__(self, *, session_store: SessionStore | None) -> None:
        self._session_store = session_store

    async def consume(
        self,
        stream: AsyncIterator[ModelEvent],
        *,
        emit: EventSink,
        step: int,
        step_started_at: float,
        session_id: str | None,
        can_adopt_provider_origin: bool,
        on_imperfect: Callable[[], None],
        request_id: str | None = None,
        request_started_at: float | None = None,
        provider_name: str | None = None,
        model_name: str | None = None,
        context: ModelContext | None = None,
        context_capacity_tokens: int | None = None,
        diagnostic_sink: DiagnosticSink | None = None,
        on_output_started: Callable[[str], Awaitable[None]] | None = None,
        buffer_text: bool = False,
    ) -> ModelStepResult:
        """Consume one model stream, emitting normalized events as it goes.

        消费一次模型流,同时持续发出规范化事件."""

        step_text: list[str] = []
        buffered_text = _BoundedStepTextBuffer() if buffer_text else None
        step_reasoning: list[str] = []
        tool_calls: list[ToolCall] = []
        completion: ModelCompleted | None = None
        selected_provider: ModelProviderSelected | None = None
        backend_tool_started_at: dict[str, float] = {}
        request_started_at = request_started_at if request_started_at is not None else monotonic()
        first_output_at: float | None = None
        first_output_kind: str | None = None
        provider_attempt_started_at = request_started_at
        provider_attempt_provider = provider_name
        provider_attempt_model = model_name
        provider_attempt_failures: list[dict[str, object]] = []
        thinking_completed = False
        output_started = False

        def mark_trace_output(output_kind: str) -> None:
            nonlocal first_output_at, first_output_kind
            if first_output_at is None:
                first_output_at = monotonic()
                first_output_kind = output_kind

        async def emit_trace(data: dict[str, object]) -> None:
            if diagnostic_sink is None:
                return
            try:
                outcome = diagnostic_sink(AgentEventKind.RUNTIME_TRACE_MODEL_REQUEST, data)
                if hasattr(outcome, "__await__"):
                    await outcome  # type: ignore[misc]
            except Exception:
                # Instrumentation is deliberately fail-open and body-free.
                return

        async def measured_stream() -> AsyncIterator[ModelEvent]:
            try:
                async for event in stream:
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failed_at = monotonic()
                provider = provider_attempt_provider or provider_name or "unknown"
                model = provider_attempt_model or model_name or "unknown"
                attempt_duration = max(0.0, failed_at - provider_attempt_started_at) * 1000
                attempts = (
                    *provider_attempt_failures,
                    {
                        "attempt_index": len(provider_attempt_failures),
                        "provider": provider,
                        "model": model,
                        "status": "failed",
                        "duration_ms": attempt_duration,
                        "error_type": type(error).__name__,
                        "failure_kind": "provider_error",
                        "failover": selected_provider.failover
                        if selected_provider is not None
                        else False,
                    },
                )
                await emit_trace(
                    {
                        "request_id": request_id or "",
                        "step": step,
                        "provider": provider,
                        "model": model,
                        "source": context.request_source.value
                        if context is not None
                        else "unknown",
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "duration_ms": max(0.0, (failed_at - request_started_at) * 1000),
                        "ttft_ms": max(0.0, (first_output_at - request_started_at) * 1000)
                        if first_output_at is not None
                        else None,
                        "stream_duration_ms": max(0.0, (failed_at - first_output_at) * 1000)
                        if first_output_at is not None
                        else None,
                        "context_generation": context.context_generation
                        if context is not None
                        else None,
                        "cache_epoch": context.cache_epoch if context is not None else None,
                        "cache_boundary_reason": context.cache_boundary_reason.value
                        if context is not None and context.cache_boundary_reason is not None
                        else "none",
                        "capacity_tokens": context_capacity_tokens,
                        "provider_attempts": attempts[:8],
                        "retry_count": len(provider_attempt_failures),
                        "failover_count": int(selected_provider.failover)
                        if selected_provider is not None
                        else 0,
                    }
                )
                raise

        async def mark_output_started(output_kind: str) -> None:
            nonlocal output_started
            if output_started or on_output_started is None:
                return
            await on_output_started(output_kind)
            output_started = True

        async def complete_thinking() -> None:
            nonlocal thinking_completed
            if thinking_completed:
                return
            thinking_completed = True
            await emit(
                AgentEventKind.MODEL_THINKING_COMPLETED,
                {
                    "step": step,
                    "duration_seconds": monotonic() - step_started_at,
                },
            )

        async for model_event in measured_stream():
            if isinstance(model_event, ModelProviderAttemptFailed):
                failure_at = monotonic()
                attempt_duration = max(0.0, failure_at - provider_attempt_started_at)
                attempt_index = len(provider_attempt_failures)
                provider_attempt_failures.append(
                    {
                        "attempt_index": attempt_index,
                        "provider": model_event.provider,
                        "model": model_event.model,
                        "status": "failed",
                        "duration_ms": attempt_duration * 1000,
                        "error_type": model_event.error_type,
                        "failure_kind": model_event.failure_kind or "unknown",
                        "status_code": model_event.status_code,
                        "failover": False,
                    }
                )
                await emit(
                    AgentEventKind.PROVIDER_ATTEMPT_FAILED,
                    {
                        "provider": model_event.provider,
                        "model": model_event.model,
                        "error_type": model_event.error_type,
                        "message": model_event.message,
                        "failure_kind": model_event.failure_kind,
                        "status_code": model_event.status_code,
                        "request_id": request_id or "",
                        "duration_seconds": attempt_duration,
                    },
                )
                provider_attempt_started_at = failure_at
            elif isinstance(model_event, ModelProviderSelected):
                selected_provider = model_event
                provider_attempt_provider = model_event.provider
                provider_attempt_model = model_event.model
                origin_updated = False
                if (
                    can_adopt_provider_origin
                    and self._session_store is not None
                    and session_id is not None
                ):
                    await self._session_store.update_session_provider(
                        session_id,
                        model_event.provider,
                        model_event.model,
                        model_event.context_affinity,
                    )
                    origin_updated = True
                await emit(
                    AgentEventKind.PROVIDER_SELECTED,
                    {
                        "provider": model_event.provider,
                        "model": model_event.model,
                        "context_window_tokens": model_event.context_window_tokens,
                        "failover": model_event.failover,
                        "session_origin_updated": origin_updated,
                    },
                )
            elif isinstance(model_event, ModelTextDelta):
                await complete_thinking()
                if model_event.text:
                    mark_trace_output("text")
                    await mark_output_started("text")
                    on_imperfect()
                if buffered_text is None:
                    step_text.append(model_event.text)
                    await emit(AgentEventKind.TEXT_DELTA, {"text": model_event.text})
                else:
                    buffered_text.append(model_event.text)
            elif isinstance(model_event, ModelReasoningDelta):
                if model_event.text:
                    mark_trace_output("reasoning")
                    await mark_output_started("reasoning")
                    on_imperfect()
                step_reasoning.append(model_event.text)
                await emit(
                    AgentEventKind.REASONING_DELTA,
                    {"text": model_event.text},
                )
            elif isinstance(model_event, ModelBackendToolStarted):
                mark_trace_output("backend_tool_started")
                await mark_output_started("backend_tool_started")
                await complete_thinking()
                on_imperfect()
                backend_tool_started_at[model_event.call_id] = monotonic()
                await emit(
                    AgentEventKind.BACKEND_TOOL_STARTED,
                    {"id": model_event.call_id, "name": model_event.name},
                )
            elif isinstance(model_event, ModelBackendToolCompleted):
                mark_trace_output("backend_tool_completed")
                await mark_output_started("backend_tool_completed")
                await complete_thinking()
                on_imperfect()
                started_at = backend_tool_started_at.pop(
                    model_event.call_id,
                    step_started_at,
                )
                await emit(
                    AgentEventKind.BACKEND_TOOL_COMPLETED,
                    {
                        "id": model_event.call_id,
                        "name": model_event.name,
                        "duration_seconds": monotonic() - started_at,
                    },
                )
            elif isinstance(model_event, ModelToolCall):
                mark_trace_output("tool_call")
                await mark_output_started("tool_call")
                await complete_thinking()
                on_imperfect()
                tool_calls.append(model_event.call)
            elif isinstance(model_event, ModelRequestTrajectoryObserved):
                await emit(
                    AgentEventKind.MODEL_REQUEST_TRAJECTORY,
                    model_event.to_event_data(),
                )
            elif isinstance(model_event, ModelCompleted):
                completed_at = monotonic()
                await mark_output_started("completed")
                await complete_thinking()
                on_imperfect()
                if buffered_text is not None and model_event.response_text is not None:
                    _BoundedStepTextBuffer.validate(model_event.response_text)
                completion = model_event

                usage = model_event.usage
                provider = provider_attempt_provider or (
                    selected_provider.provider if selected_provider is not None else None
                )
                model = provider_attempt_model or (
                    selected_provider.model if selected_provider is not None else None
                )
                provider_attempts = [
                    {
                        "attempt_index": len(provider_attempt_failures),
                        "provider": provider or "unknown",
                        "model": model or "unknown",
                        "status": "succeeded",
                        "duration_ms": max(
                            0.0, (completed_at - provider_attempt_started_at) * 1000
                        ),
                        "error_type": "",
                        "failure_kind": "",
                        "status_code": None,
                        "failover": selected_provider.failover
                        if selected_provider is not None
                        else False,
                    }
                ]
                await emit_trace(
                    {
                        "request_id": request_id or "",
                        "step": step,
                        "provider": provider or "unknown",
                        "model": model or "unknown",
                        "source": context.request_source.value
                        if context is not None
                        else "unknown",
                        "status": "succeeded",
                        "duration_ms": max(0.0, (completed_at - request_started_at) * 1000),
                        "ttft_ms": max(0.0, (first_output_at - request_started_at) * 1000)
                        if first_output_at is not None
                        else None,
                        "stream_duration_ms": max(0.0, (completed_at - first_output_at) * 1000)
                        if first_output_at is not None
                        else None,
                        "output_kind": first_output_kind or "no_output_before_completion",
                        "input_tokens": usage.input_tokens if usage is not None else None,
                        "output_tokens": usage.output_tokens if usage is not None else None,
                        "cache_read_tokens": usage.cache_read_tokens if usage is not None else None,
                        "cache_write_tokens": usage.cache_write_tokens
                        if usage is not None
                        else None,
                        "cache_miss_tokens": usage.cache_miss_tokens if usage is not None else None,
                        "cache_reuse_ratio": usage.cache_reuse_ratio if usage is not None else None,
                        "capacity_tokens": context_capacity_tokens,
                        "context_generation": context.context_generation
                        if context is not None
                        else None,
                        "cache_epoch": context.cache_epoch if context is not None else None,
                        "cache_boundary_reason": context.cache_boundary_reason.value
                        if context is not None and context.cache_boundary_reason is not None
                        else "none",
                        "provider_attempts": tuple(provider_attempts[:8]),
                        "retry_count": len(provider_attempt_failures),
                        "failover_count": int(selected_provider.failover)
                        if selected_provider is not None
                        else 0,
                    }
                )

        return ModelStepResult(
            text=buffered_text.values() if buffered_text is not None else tuple(step_text),
            reasoning=tuple(step_reasoning),
            tool_calls=tuple(tool_calls),
            completion=completion,
            selected_provider=selected_provider,
        )


__all__ = [
    "MAX_BUFFERED_MODEL_STEP_TEXT_BYTES",
    "MAX_BUFFERED_MODEL_STEP_TEXT_CHUNKS",
    "ModelStepProcessor",
    "ModelStepResult",
]
