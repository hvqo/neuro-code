"""Canonical tool value objects.

Tool registration and execution remain outside the domain package.  This
module only owns the immutable request/result shapes shared by those ports.

定义规范的工具值对象. 工具注册和执行位于领域包之外,此处只拥有端口共享的不可变请求和结果形状.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ToolExecutionMode(StrEnum):
    """Scheduling policy advertised by a tool definition.

    ``AUTO`` is resolved from the tool's side-effect contract.  It keeps old
    tools safe while allowing new read-only tools to opt into bounded
    parallelism without weakening the permission pipeline.
    """

    AUTO = "auto"
    EXCLUSIVE = "exclusive"
    PARALLEL = "parallel"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    execution_mode: ToolExecutionMode = ToolExecutionMode.AUTO

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        if not isinstance(self.execution_mode, ToolExecutionMode):
            raise TypeError("execution_mode must be a ToolExecutionMode")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }
        # ``auto`` is an internal compatibility default and must not alter
        # existing provider wire payloads.
        if self.execution_mode is not ToolExecutionMode.AUTO:
            result["execution_mode"] = self.execution_mode.value
        return result


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"content": self.content, "is_error": self.is_error}
        if self.metadata is not None:
            result["metadata"] = dict(self.metadata)
        return result


class ToolResultProjectionStrategy(StrEnum):
    """Describe how a canonical tool result entered model context.

    描述规范工具结果如何进入模型上下文.
    """

    PASS_THROUGH = "pass_through"
    DETERMINISTIC_HEAD_TAIL = "deterministic_head_tail"


@dataclass(frozen=True, slots=True)
class ToolResultContextProjection:
    """Bounded model-facing facts for one canonical tool result.

    The projection is deliberately separate from :class:`ToolResult`: the
    canonical result remains complete for events, verification, supervision,
    and artifact handling, while this value describes only the content placed
    in a model-facing ``Role.TOOL`` message.

    一个规范工具结果面向模型的有界事实投影. 该投影有意独立于
    :class:`ToolResult`:规范结果继续为事件、验证、监督和 artifact 处理保留完整内容,
    此值只描述放入面向模型 ``Role.TOOL`` 消息的内容.
    """

    content: str
    truncated: bool
    original_bytes: int
    projected_bytes: int
    original_estimated_tokens: int
    projected_estimated_tokens: int
    omitted_bytes: int
    artifact_available: bool
    strategy: ToolResultProjectionStrategy

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("projected tool content must be text")
        if not isinstance(self.truncated, bool):
            raise TypeError("tool projection truncation must be a bool")
        for name in (
            "original_bytes",
            "projected_bytes",
            "original_estimated_tokens",
            "projected_estimated_tokens",
            "omitted_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.projected_bytes != len(self.content.encode("utf-8")):
            raise ValueError("projected_bytes must match the UTF-8 content size")
        if self.omitted_bytes > self.original_bytes:
            raise ValueError("omitted_bytes must not exceed original_bytes")
        if not isinstance(self.artifact_available, bool):
            raise TypeError("artifact_available must be a bool")
        if not isinstance(self.strategy, ToolResultProjectionStrategy):
            raise TypeError("tool projection strategy must be canonical")
        if not self.truncated:
            if self.strategy is not ToolResultProjectionStrategy.PASS_THROUGH:
                raise ValueError("untruncated projection must use pass_through")
            if self.omitted_bytes != 0:
                raise ValueError("untruncated projection must omit no bytes")
        elif self.strategy is ToolResultProjectionStrategy.PASS_THROUGH:
            raise ValueError("truncated projection must describe a bounded strategy")

    def to_metadata(self) -> dict[str, object]:
        """Return bounded diagnostic metadata without output text or paths.

        返回不包含输出文本或路径的有界诊断元数据.
        """

        return {
            "activated": self.truncated,
            "truncated": self.truncated,
            "original_bytes": self.original_bytes,
            "projected_bytes": self.projected_bytes,
            "original_estimated_tokens": self.original_estimated_tokens,
            "projected_estimated_tokens": self.projected_estimated_tokens,
            "omitted_bytes": self.omitted_bytes,
            "artifact_available": self.artifact_available,
            "strategy": self.strategy.value,
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Canonical bounded result shared by model, UI, ACP, and replay layers."""

    call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    metadata: Mapping[str, Any] | None = None
    duration_seconds: float | None = None
    not_started: bool = False
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ValueError("tool execution identity must be non-empty")
        if not isinstance(self.content, str):
            raise TypeError("tool execution content must be text")
        if not isinstance(self.is_error, bool):
            raise TypeError("is_error must be a bool")
        if self.metadata is not None:
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        if not isinstance(self.not_started, bool) or not isinstance(self.cancelled, bool):
            raise TypeError("tool execution flags must be bools")

    @classmethod
    def from_tool_result(
        cls,
        call_id: str,
        tool_name: str,
        result: ToolResult,
        *,
        duration_seconds: float | None = None,
        not_started: bool = False,
        cancelled: bool = False,
    ) -> ToolExecutionResult:
        if not isinstance(result, ToolResult):
            raise TypeError("result must be a ToolResult")
        return cls(
            call_id,
            tool_name,
            result.content,
            result.is_error,
            result.metadata,
            duration_seconds,
            not_started,
            cancelled,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.call_id,
            "name": self.tool_name,
            "content": self.content,
            "is_error": self.is_error,
            "not_started": self.not_started,
            "cancelled": self.cancelled,
        }
        if self.metadata is not None:
            result["metadata"] = dict(self.metadata)
        if self.duration_seconds is not None:
            result["duration_seconds"] = self.duration_seconds
        return result


__all__ = [
    "ToolDefinition",
    "ToolExecutionMode",
    "ToolExecutionResult",
    "ToolResult",
    "ToolResultContextProjection",
    "ToolResultProjectionStrategy",
]
