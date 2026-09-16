"""Deterministic model-facing projection for canonical tool results.

The runtime keeps one complete :class:`ToolResult` for terminal events,
verification, supervision, progress, and artifact metadata.  This module
owns only the bounded text projection inserted into a model ``Role.TOOL``
message.  It never calls a model and never creates or rewrites artifacts.

规范工具结果面向模型的确定性投影.

Runtime 为终态事件、验证、监督、进展和 artifact 元数据保留一个完整的
``ToolResult``. 本模块只拥有放入模型 ``Role.TOOL`` 消息的有界文本投影,
不会调用模型,也不会创建或重写 artifact.
"""

from __future__ import annotations

from neuro_code.domain.conversation.context import estimate_text_tokens
from neuro_code.domain.tools import (
    ToolResult,
    ToolResultContextProjection,
    ToolResultProjectionStrategy,
)

# This is a provider-neutral safety ceiling, not a provider tokenizer claim.
# ContextPreflight remains authoritative for the complete request and uses the
# configured ProviderContextWindow plus the same approximate estimator as the
# rest of the runtime.
MAX_MODEL_TOOL_RESULT_BYTES = 64 * 1024
MAX_MODEL_TOOL_RESULT_ESTIMATED_TOKENS = 16 * 1024

_MINIMAL_TRUNCATION_MARKER = "[tool output truncated]"


def _truncation_marker(
    *,
    is_error: bool,
    omitted_bytes: int,
    artifact_available: bool,
) -> str:
    kind = "tool error output" if is_error else "tool output"
    artifact = (
        "a fuller artifact is available"
        if artifact_available
        else "no fuller artifact is available"
    )
    return (
        f"\n\n[{kind} truncated; approximately {omitted_bytes} bytes omitted; {artifact}. "
        "Prefer targeted, filtering, or range-based requests.]"
    )


def _split_head_tail(encoded: bytes, source_budget: int) -> tuple[str, str]:
    if source_budget <= 0:
        return "", ""
    head_budget = (source_budget + 1) // 2
    tail_budget = source_budget - head_budget
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
    return head, tail


def _bounded_marker(marker: str, byte_limit: int) -> str:
    if byte_limit <= 0:
        return ""
    return marker.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")


def project_tool_result(
    result: ToolResult,
    *,
    byte_limit: int,
    estimated_token_limit: int | None = None,
    artifact_available: bool = False,
) -> ToolResultContextProjection:
    """Build a deterministic, UTF-8-safe model-facing result projection.

    ``byte_limit`` is the existing tool-context output bound.  The guard also
    applies the provider-neutral ceiling above so an extension tool cannot
    place an unbounded result in the next request merely by using the default
    tool limit.  A caller at the ordered tool-batch boundary may provide a
    smaller approximate token share for aggregate request budgeting.  Token
    values are approximate and are never presented as provider-native
    accounting. ``artifact_available`` is a runtime-owned fact; generic
    ``ToolResult.metadata`` is intentionally not trusted to derive it.

    构建确定性且 UTF-8 安全的面向模型工具结果投影.
    ``byte_limit`` 是既有工具上下文输出上限; guard 同时应用 Provider 无关的上限,
    避免扩展工具仅通过使用默认工具上限就在下一次请求中注入无界结果. 有序工具批次边界
    可额外提供更小的近似 token 份额以进行聚合请求预算. token 值是近似值,
    绝不伪装为 Provider 原生计量.
    """

    if not isinstance(result, ToolResult):
        raise TypeError("result must be a ToolResult")
    if isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit < 0:
        raise ValueError("byte_limit must be a non-negative integer")
    if estimated_token_limit is not None and (
        isinstance(estimated_token_limit, bool)
        or not isinstance(estimated_token_limit, int)
        or estimated_token_limit < 0
    ):
        raise ValueError("estimated_token_limit must be a non-negative integer or None")
    if not isinstance(artifact_available, bool):
        raise TypeError("artifact_available must be a bool")

    content = result.content
    encoded = content.encode("utf-8")
    original_bytes = len(encoded)
    original_tokens = estimate_text_tokens(content)
    effective_limit = min(byte_limit, MAX_MODEL_TOOL_RESULT_BYTES)
    effective_token_limit = min(
        MAX_MODEL_TOOL_RESULT_ESTIMATED_TOKENS,
        estimated_token_limit
        if estimated_token_limit is not None
        else MAX_MODEL_TOOL_RESULT_ESTIMATED_TOKENS,
    )
    if original_bytes <= effective_limit and original_tokens <= effective_token_limit:
        return ToolResultContextProjection(
            content=content,
            truncated=False,
            original_bytes=original_bytes,
            projected_bytes=original_bytes,
            original_estimated_tokens=original_tokens,
            projected_estimated_tokens=original_tokens,
            omitted_bytes=0,
            artifact_available=artifact_available,
            strategy=ToolResultProjectionStrategy.PASS_THROUGH,
        )

    if effective_limit == 0:
        return ToolResultContextProjection(
            content="",
            truncated=True,
            original_bytes=original_bytes,
            projected_bytes=0,
            original_estimated_tokens=original_tokens,
            projected_estimated_tokens=0,
            omitted_bytes=original_bytes,
            artifact_available=artifact_available,
            strategy=ToolResultProjectionStrategy.DETERMINISTIC_HEAD_TAIL,
        )

    source_budget = max(
        0,
        min(
            original_bytes - 1,
            effective_limit
            - len(
                _truncation_marker(
                    is_error=result.is_error,
                    omitted_bytes=original_bytes,
                    artifact_available=artifact_available,
                ).encode("utf-8")
            ),
        ),
    )
    for _ in range(32):
        head, tail = _split_head_tail(encoded, source_budget)
        preserved_bytes = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        marker = _truncation_marker(
            is_error=result.is_error,
            omitted_bytes=max(0, original_bytes - preserved_bytes),
            artifact_available=artifact_available,
        )
        projected = f"{head}{marker}{tail}"
        projected_bytes = len(projected.encode("utf-8"))
        projected_tokens = estimate_text_tokens(projected)
        if projected_bytes <= effective_limit and projected_tokens <= effective_token_limit:
            return ToolResultContextProjection(
                content=projected,
                truncated=True,
                original_bytes=original_bytes,
                projected_bytes=projected_bytes,
                original_estimated_tokens=original_tokens,
                projected_estimated_tokens=projected_tokens,
                omitted_bytes=original_bytes - preserved_bytes,
                artifact_available=artifact_available,
                strategy=ToolResultProjectionStrategy.DETERMINISTIC_HEAD_TAIL,
            )
        if source_budget == 0:
            break
        if projected_bytes > effective_limit:
            source_budget = max(0, source_budget - (projected_bytes - effective_limit))
        else:
            source_budget //= 2

    marker = _truncation_marker(
        is_error=result.is_error,
        omitted_bytes=original_bytes,
        artifact_available=artifact_available,
    )
    bounded = _bounded_marker(marker, effective_limit)
    if "truncated" not in bounded:
        bounded = _bounded_marker(_MINIMAL_TRUNCATION_MARKER, effective_limit)
    if estimate_text_tokens(bounded) > effective_token_limit:
        bounded = ""
    return ToolResultContextProjection(
        content=bounded,
        truncated=True,
        original_bytes=original_bytes,
        projected_bytes=len(bounded.encode("utf-8")),
        original_estimated_tokens=original_tokens,
        projected_estimated_tokens=estimate_text_tokens(bounded),
        omitted_bytes=original_bytes,
        artifact_available=artifact_available,
        strategy=ToolResultProjectionStrategy.DETERMINISTIC_HEAD_TAIL,
    )


__all__ = [
    "MAX_MODEL_TOOL_RESULT_BYTES",
    "MAX_MODEL_TOOL_RESULT_ESTIMATED_TOKENS",
    "project_tool_result",
]
