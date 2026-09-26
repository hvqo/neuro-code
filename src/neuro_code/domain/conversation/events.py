from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from neuro_code.domain.conversation.messages import PreservedContextItem, ToolCall
from neuro_code.domain.conversation.prompt_continuity import (
    CacheBoundaryReason,
    ModelRequestSource,
)


class AgentEventKind(StrEnum):
    SESSION_STARTED = "session_started"
    USER_MESSAGE = "user_message"
    MODEL_STEP_STARTED = "model_step_started"
    MODEL_REQUEST_SNAPSHOT = "model_request_snapshot"
    MODEL_REQUEST_TRAJECTORY = "model_request_trajectory"
    RUNTIME_TRACE_MODEL_REQUEST = "runtime_trace_model_request"
    RUNTIME_TRACE_CONTEXT_BUILD = "runtime_trace_context_build"
    RUNTIME_TRACE_CONTEXT_ROLLOVER = "runtime_trace_context_rollover"
    RUNTIME_TRACE_REPLAN = "runtime_trace_replan"
    RUNTIME_TRACE_VERIFICATION = "runtime_trace_verification"
    RUNTIME_TRACE_FINALIZER = "runtime_trace_finalizer"
    RUNTIME_TRACE_SUBAGENT = "runtime_trace_subagent"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_OUTPUT_STARTED = "model_output_started"
    MODEL_THINKING_COMPLETED = "model_thinking_completed"
    CONTEXT_USAGE_UPDATED = "context_usage_updated"
    CONTEXT_PREFLIGHT = "context_preflight"
    EXECUTION_BUDGET_UPDATED = "execution_budget_updated"
    CONTEXT_COMPACTION_STARTED = "context_compaction_started"
    CONTEXT_COMPACTION_COMPLETED = "context_compaction_completed"
    EXECUTION_SEGMENT_CHECKPOINTED = "execution_segment_checkpointed"
    WORKSPACE_UNDO_STATE = "workspace_undo_state"
    FINALIZING_STARTED = "finalizing_started"
    ULTRACODE_DELEGATION_PROGRESS = "ultracode_delegation_progress"
    BACKGROUND_TASK_COMPLETION_REMINDER = "background_task_completion_reminder"
    BACKGROUND_TASK_AUTO_WAKE_STARTED = "background_task_auto_wake_started"
    PROVIDER_ATTEMPT_FAILED = "provider_attempt_failed"
    PROVIDER_SELECTED = "provider_selected"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    BACKEND_TOOL_STARTED = "backend_tool_started"
    BACKEND_TOOL_COMPLETED = "backend_tool_completed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_PERMISSION = "tool_permission"
    TOOL_APPROVAL_REQUESTED = "tool_approval_requested"
    TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    USER_INPUT_REQUESTED = "user_input_requested"
    USER_INPUT_RESOLVED = "user_input_resolved"
    PLAN_UPDATED = "plan_updated"
    PLAN_EXECUTION_REQUESTED = "plan_execution_requested"
    SESSION_TASK_STARTED = "session_task_started"
    SESSION_TASK_COMPLETED = "session_task_completed"
    SESSION_TASK_FAILED = "session_task_failed"
    SESSION_TASK_CANCELLED = "session_task_cancelled"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    TURN_ABANDONED = "turn_abandoned"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    sequence: int
    kind: AgentEventKind
    data: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")

    @classmethod
    def create(
        cls,
        sequence: int,
        kind: AgentEventKind,
        data: Mapping[str, Any] | None = None,
    ) -> AgentEvent:
        return cls(sequence, kind, data or {}, datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind.value,
            "data": dict(self.data),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call: ToolCall


@dataclass(frozen=True, slots=True)
class ModelBackendToolStarted:
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ModelBackendToolCompleted:
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ModelProviderAttemptFailed:
    provider: str
    model: str
    error_type: str
    message: str
    failure_kind: str | None = None
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class ModelProviderSelected:
    provider: str
    model: str
    context_affinity: str | None
    failover: bool
    context_window_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelRequestTrajectoryObserved:
    """Body-free fingerprints of one final provider-wire request.

    One request is represented only by keyed digests and bounded shape facts.
    It never contains prompt text, tool arguments, reasoning, URLs, or headers.
    """

    sequence: int
    source: ModelRequestSource
    provider: str
    model: str
    context_generation: int
    cache_epoch: int
    boundary_reason: CacheBoundaryReason | None
    message_fingerprints: tuple[str, ...]
    message_count: int
    tool_count: int
    tools_fingerprint: str
    stable_prefix_fingerprint: str
    request_fingerprint: str | None
    common_prefix_messages: int | None
    first_divergence_index: int | None
    previous_message_count: int | None
    append_only: bool | None
    fingerprints_truncated: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("trajectory sequence must be positive")
        if not isinstance(self.source, ModelRequestSource):
            raise TypeError("trajectory source must be a ModelRequestSource")
        if not self.provider or not self.model:
            raise ValueError("trajectory provider and model must be non-empty")
        for name in ("context_generation", "cache_epoch", "message_count", "tool_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.boundary_reason is not None and not isinstance(
            self.boundary_reason, CacheBoundaryReason
        ):
            raise TypeError("boundary reason must be a CacheBoundaryReason or None")
        object.__setattr__(self, "message_fingerprints", tuple(self.message_fingerprints))
        if len(self.message_fingerprints) > self.message_count:
            raise ValueError("fingerprint count cannot exceed provider message count")
        for digest in (
            *self.message_fingerprints,
            self.tools_fingerprint,
            self.stable_prefix_fingerprint,
            *((self.request_fingerprint,) if self.request_fingerprint is not None else ()),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("trajectory fingerprints must be lowercase SHA-256 digests")
        if self.common_prefix_messages is not None and (
            isinstance(self.common_prefix_messages, bool) or self.common_prefix_messages < 0
        ):
            raise ValueError("common prefix must be non-negative or None")
        if self.first_divergence_index is not None and (
            isinstance(self.first_divergence_index, bool) or self.first_divergence_index < 0
        ):
            raise ValueError("first divergence must be non-negative or None")
        if self.previous_message_count is not None and (
            isinstance(self.previous_message_count, bool) or self.previous_message_count < 0
        ):
            raise ValueError("previous message count must be non-negative or None")
        if self.append_only is not None and not isinstance(self.append_only, bool):
            raise TypeError("append_only must be bool or None")
        if not isinstance(self.fingerprints_truncated, bool):
            raise TypeError("fingerprints_truncated must be bool")

    def to_event_data(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "source": self.source.value,
            "provider": self.provider,
            "model": self.model,
            "context_generation": self.context_generation,
            "cache_epoch": self.cache_epoch,
            "boundary_reason": (
                self.boundary_reason.value if self.boundary_reason is not None else None
            ),
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "message_fingerprints": list(self.message_fingerprints),
            "tools_fingerprint": self.tools_fingerprint,
            "stable_prefix_fingerprint": self.stable_prefix_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "common_prefix_messages": self.common_prefix_messages,
            "first_divergence_index": self.first_divergence_index,
            "previous_message_count": self.previous_message_count,
            "append_only": self.append_only,
            "fingerprints_truncated": self.fingerprints_truncated,
        }


class ModelInputTokenSemantics(StrEnum):
    """Describe what a Provider's reported ``input_tokens`` value measures.

    描述 Provider 上报的 ``input_tokens`` 实际衡量的范围。
    """

    TOTAL = "total"
    UNCACHED_TAIL = "uncached_tail"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Provider-reported model usage without assuming one cache billing dialect.

    ``input_tokens`` and ``output_tokens`` retain the Provider's native
    request/response fields.  Most providers report total input, while
    Anthropic reports the uncached tail after its cache breakpoint.  The
    ``input_token_semantics`` field makes that distinction explicit;
    ``processed_input_tokens`` returns an exact total only when the provider
    reported enough information to derive one.  Cache fields are optional
    because providers expose different subsets and sometimes report only an
    aggregate input count.  ``cache_read_tokens`` is the canonical internal
    name for cached input reused by a request; ``cache_hit_tokens`` remains a
    readable alias for APIs that use that wording.

    表示 Provider 上报的模型用量,不假定各家缓存计费字段完全一致. ``input_tokens`` 和
    ``output_tokens`` 保留 Provider 原始字段,大多数 Provider 报告总输入,Anthropic 则
    报告缓存断点之后的未缓存尾部。``input_token_semantics`` 明确标识该差异,只有在
    Provider 上报了足够信息时,``processed_input_tokens`` 才返回精确的完整处理输入。
    缓存字段是可选的,因为不同 Provider 只会暴露其中一部分.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_miss_tokens: int | None = None
    input_token_semantics: ModelInputTokenSemantics = ModelInputTokenSemantics.TOTAL

    def __post_init__(self) -> None:
        if not isinstance(self.input_token_semantics, ModelInputTokenSemantics):
            raise TypeError("input_token_semantics must be a ModelInputTokenSemantics")
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_miss_tokens",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer or None")

    @property
    def cache_hit_tokens(self) -> int | None:
        """Return cached input tokens using the common cache-hit terminology.

        以常见的 cache-hit 术语返回已复用的输入 token.
        """

        return self.cache_read_tokens

    @property
    def processed_input_tokens(self) -> int | None:
        """Return total input processed only when its calculation is exact.

        仅在计算精确时返回完整处理输入 token。
        """

        if self.input_tokens is None:
            return None
        if self.input_token_semantics is ModelInputTokenSemantics.TOTAL:
            return self.input_tokens
        if self.cache_read_tokens is None or self.cache_write_tokens is None:
            return None
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cache_reuse_ratio(self) -> float | None:
        """Return cache reuse only when the provider supplied an exact denominator."""

        if self.cache_read_tokens is None:
            return None
        denominator = self.processed_input_tokens
        if denominator is None or denominator <= 0 or self.cache_read_tokens > denominator:
            return None
        return self.cache_read_tokens / denominator

    @property
    def has_reported_tokens(self) -> bool:
        """Whether the Provider reported at least one numeric usage field.

        Provider 是否上报了至少一个数值用量字段。
        """

        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.cache_miss_tokens,
            )
        )

    def to_event_data(self) -> dict[str, int | float | str | None]:
        """Return the bounded, provider-neutral usage projection for events.

        返回用于事件的有界且与 Provider 无关的用量投影.
        """

        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "input_token_semantics": self.input_token_semantics.value,
            "processed_input_tokens": self.processed_input_tokens,
            "cache_reuse_ratio": self.cache_reuse_ratio,
        }


@dataclass(frozen=True, slots=True)
class ModelCompleted:
    stop_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    context_items: tuple[PreservedContextItem, ...] = ()
    response_text: str | None = None
    usage: ModelUsage | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_items", tuple(self.context_items))
        if not all(isinstance(item, PreservedContextItem) for item in self.context_items):
            raise TypeError("context_items must contain PreservedContextItem values")
        if self.usage is not None and not isinstance(self.usage, ModelUsage):
            raise TypeError("usage must be a ModelUsage or None")
        legacy_usage = ModelUsage(self.input_tokens, self.output_tokens)
        if self.usage is None:
            if self.input_tokens is not None or self.output_tokens is not None:
                object.__setattr__(self, "usage", legacy_usage)
            return
        if (
            self.input_tokens is not None
            and self.usage.input_tokens is not None
            and self.input_tokens != self.usage.input_tokens
        ):
            raise ValueError("input_tokens must agree with usage.input_tokens")
        if (
            self.output_tokens is not None
            and self.usage.output_tokens is not None
            and self.output_tokens != self.usage.output_tokens
        ):
            raise ValueError("output_tokens must agree with usage.output_tokens")
        if self.input_tokens is None:
            object.__setattr__(self, "input_tokens", self.usage.input_tokens)
        if self.output_tokens is None:
            object.__setattr__(self, "output_tokens", self.usage.output_tokens)


type ModelEvent = (
    ModelTextDelta
    | ModelReasoningDelta
    | ModelToolCall
    | ModelBackendToolStarted
    | ModelBackendToolCompleted
    | ModelProviderAttemptFailed
    | ModelProviderSelected
    | ModelRequestTrajectoryObserved
    | ModelCompleted
)

__all__ = [
    "AgentEvent",
    "AgentEventKind",
    "ModelBackendToolCompleted",
    "ModelBackendToolStarted",
    "ModelCompleted",
    "ModelEvent",
    "ModelInputTokenSemantics",
    "ModelProviderAttemptFailed",
    "ModelProviderSelected",
    "ModelReasoningDelta",
    "ModelRequestTrajectoryObserved",
    "ModelTextDelta",
    "ModelToolCall",
    "ModelUsage",
]
