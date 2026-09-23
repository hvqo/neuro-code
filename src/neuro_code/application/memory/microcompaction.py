"""Deterministic, non-durable cleanup of old model-facing tool results.

Microcompaction replaces only eligible ``Role.TOOL`` bodies in one immutable
model projection. The source ``ModelContext`` and the canonical session history
remain untouched. A small in-memory snapshot pins each completed batch for one
session/context generation; a restart safely starts from the durable source
again.

对旧工具结果进行确定性、非持久化的模型上下文清理。

Microcompaction 只替换不可变模型投影中符合条件的 ``Role.TOOL`` 正文, 绝不修改源
``ModelContext`` 或规范会话历史. 每个 session/context generation 使用有界内存快照固定已完成
的批次; 进程重启后安全地从持久化来源重新开始.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from neuro_code.domain.conversation.context import ModelContext, estimate_context_tokens
from neuro_code.domain.conversation.messages import (
    Message,
    PreservedContextItem,
    Role,
    SessionItem,
    SyntheticReason,
)

MAX_MICROCOMPACTION_SCAN_ITEMS = 16_384
MAX_MICROCOMPACTION_CONTEXT_BYTES = 8 * 1024 * 1024
MAX_MICROCOMPACTION_SCAN_VALUES = 65_536
MAX_MICROCOMPACTION_VALUE_DEPTH = 32
MAX_MICROCOMPACTION_CALLS_PER_GROUP = 128
MAX_MICROCOMPACTION_CONTENT_PARTS = 256
MAX_MICROCOMPACTED_GROUPS = 4_096
MAX_TRACKED_TOOL_RESULTS = 8_192
PROTECTED_RECENT_TOOL_GROUPS = 3
MIN_MICROCOMPACTION_SAVINGS_BYTES = 1_024
MIN_MICROCOMPACTION_SAVINGS_TOKENS = 256
MIN_RETRIGGER_APPENDED_ITEMS = 8
MIN_RETRIGGER_APPENDED_TOKENS = 2_048

_MICROCOMPACTION_MARKER = (
    "[Older tool result omitted from active context. The prior result remains in this session's "
    "history; inspect history before relying on it.]"
)
_VOLATILE_TAIL_REASONS = frozenset(
    {
        SyntheticReason.WORKING_SET,
        SyntheticReason.RUNTIME_PLAN,
        SyntheticReason.RUNTIME_BUDGET,
        SyntheticReason.RUNTIME_CHECKPOINT,
        SyntheticReason.RUNTIME_SUPERVISION,
        SyntheticReason.RUNTIME_BACKGROUND_TASK,
    }
)


class MicrocompactionTriggerReason(StrEnum):
    """A conservative boundary that permits a new cleanup batch."""

    CONTEXT_PRESSURE = "context_pressure"


class MicrocompactionNoopReason(StrEnum):
    """Why a triggered evaluation did not add a cleanup batch."""

    NO_DURABLE_SESSION = "no_durable_session"
    NO_CURRENT_TURN_BOUNDARY = "no_current_turn_boundary"
    SOURCE_LIMIT = "source_limit"
    NO_ELIGIBLE_GROUPS = "no_eligible_groups"
    INSUFFICIENT_SAVINGS = "insufficient_savings"
    STATE_LIMIT = "state_limit"


def _require_count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class MicrocompactionTelemetry:
    """Bounded aggregate measurements for one pressure evaluation.

    No result content, tool arguments, paths, artifact identifiers, or secret
    values are retained or emitted.

    一次上下文压力评估的有界聚合指标。不保留或输出结果正文、工具参数、路径、artifact 标识或秘密值。
    """

    trigger_reason: MicrocompactionTriggerReason
    context_generation: int
    stable_boundary: int
    groups_compacted: int
    results_compacted: int
    estimated_bytes_before: int
    estimated_bytes_after: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    noop_reason: MicrocompactionNoopReason | None = None
    estimates_saturated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.trigger_reason, MicrocompactionTriggerReason):
            raise TypeError("trigger_reason must be canonical")
        if self.noop_reason is not None and not isinstance(
            self.noop_reason, MicrocompactionNoopReason
        ):
            raise TypeError("noop_reason must be canonical or None")
        if not isinstance(self.estimates_saturated, bool):
            raise TypeError("estimates_saturated must be a bool")
        for name in (
            "context_generation",
            "stable_boundary",
            "groups_compacted",
            "results_compacted",
            "estimated_bytes_before",
            "estimated_bytes_after",
            "estimated_tokens_before",
            "estimated_tokens_after",
        ):
            _require_count(name, getattr(self, name))
        if self.groups_compacted == 0 and self.results_compacted != 0:
            raise ValueError("results cannot be compacted without a compacted group")

    @property
    def estimated_bytes_saved(self) -> int:
        return max(0, self.estimated_bytes_before - self.estimated_bytes_after)

    @property
    def estimated_tokens_saved(self) -> int:
        return max(0, self.estimated_tokens_before - self.estimated_tokens_after)

    def to_event_data(self) -> dict[str, object]:
        """Return typed, body-free event metadata."""

        return {
            "trigger_reason": self.trigger_reason.value,
            "context_generation": self.context_generation,
            "stable_boundary": self.stable_boundary,
            "groups_compacted": self.groups_compacted,
            "results_compacted": self.results_compacted,
            "estimated_bytes_before": self.estimated_bytes_before,
            "estimated_bytes_after": self.estimated_bytes_after,
            "estimated_bytes_saved": self.estimated_bytes_saved,
            "estimated_tokens_before": self.estimated_tokens_before,
            "estimated_tokens_after": self.estimated_tokens_after,
            "estimated_tokens_saved": self.estimated_tokens_saved,
            "noop_reason": self.noop_reason.value if self.noop_reason is not None else None,
            "estimates_saturated": self.estimates_saturated,
        }


@dataclass(frozen=True, slots=True)
class MicrocompactionSnapshot:
    """In-memory projection state pinned to one session and generation."""

    session_id: str
    context_generation: int
    stable_boundary: int
    stable_prefix_fingerprint: str
    compaction_id: str | None
    compacted_group_fingerprints: tuple[str, ...]
    last_telemetry: MicrocompactionTelemetry

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        _require_count("context_generation", self.context_generation)
        _require_count("stable_boundary", self.stable_boundary)
        if len(self.stable_prefix_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.stable_prefix_fingerprint
        ):
            raise ValueError("stable_prefix_fingerprint must be a lowercase SHA-256 digest")
        if self.compaction_id is not None and (
            not isinstance(self.compaction_id, str)
            or not self.compaction_id
            or len(self.compaction_id.encode("utf-8")) > 256
        ):
            raise ValueError("compaction_id must be a bounded non-empty string or None")
        if len(self.compacted_group_fingerprints) > MAX_MICROCOMPACTED_GROUPS:
            raise ValueError("microcompaction state exceeds its group limit")
        if any(
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            for fingerprint in self.compacted_group_fingerprints
        ):
            raise ValueError("compacted group fingerprints must be lowercase SHA-256 digests")
        if not isinstance(self.last_telemetry, MicrocompactionTelemetry):
            raise TypeError("last_telemetry must be a MicrocompactionTelemetry")


@dataclass(frozen=True, slots=True)
class MicrocompactionEvaluation:
    """One projected request plus optional batch telemetry."""

    context: ModelContext
    telemetry: MicrocompactionTelemetry | None
    has_compacted_results: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, ModelContext):
            raise TypeError("context must be a ModelContext")
        if self.telemetry is not None and not isinstance(self.telemetry, MicrocompactionTelemetry):
            raise TypeError("telemetry must be canonical or None")
        if not isinstance(self.has_compacted_results, bool):
            raise TypeError("has_compacted_results must be a bool")


@dataclass(frozen=True, slots=True)
class _ToolGroup:
    first_index: int
    end_index: int
    call_ids: tuple[str, ...]
    result_indices: tuple[int, ...]
    complete: bool


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _item_bytes(item: SessionItem) -> bytes:
    payload = item.to_dict()
    if isinstance(item, Message):
        payload["synthetic_reason"] = (
            item.synthetic_reason.value if item.synthetic_reason is not None else None
        )
    return json.dumps(
        _plain_json(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _context_bytes(items: Sequence[SessionItem]) -> int:
    return sum(len(_item_bytes(item)) for item in items)


def _bounded_json_upper_bytes(
    value: Any,
    remaining_bytes: int,
    scan_values: list[int],
    *,
    depth: int = 0,
) -> int:
    scan_values[0] -= 1
    if scan_values[0] < 0 or depth > MAX_MICROCOMPACTION_VALUE_DEPTH:
        return remaining_bytes + 1
    if isinstance(value, str):
        return min(remaining_bytes + 1, len(value) * 6 + 2)
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, int) and not isinstance(value, bool):
            size = (value.bit_length() * 30_103) // 100_000 + 16
            return min(remaining_bytes + 1, size)
        return 32
    total = 2
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return remaining_bytes + 1
            key_size = len(key) * 6 + 4
            if key_size > remaining_bytes - total:
                return remaining_bytes + 1
            total += key_size
            item_size = _bounded_json_upper_bytes(
                item,
                remaining_bytes - total,
                scan_values,
                depth=depth + 1,
            )
            if item_size > remaining_bytes - total:
                return remaining_bytes + 1
            total += item_size + 1
        return total
    if isinstance(value, (tuple, list)):
        for item in value:
            item_size = _bounded_json_upper_bytes(
                item,
                remaining_bytes - total,
                scan_values,
                depth=depth + 1,
            )
            if item_size > remaining_bytes - total:
                return remaining_bytes + 1
            total += item_size + 1
        return total
    return remaining_bytes + 1


def _item_upper_bytes(
    item: SessionItem,
    remaining_bytes: int,
    scan_values: list[int],
) -> int:
    total = 256
    if total > remaining_bytes:
        return remaining_bytes + 1
    if isinstance(item, PreservedContextItem):
        return (
            _bounded_json_upper_bytes(
                item.payload,
                remaining_bytes - total,
                scan_values,
            )
            + total
        )
    if (
        len(item.tool_calls) > MAX_MICROCOMPACTION_CALLS_PER_GROUP
        or len(item.content_parts) > MAX_MICROCOMPACTION_CONTENT_PARTS
    ):
        return remaining_bytes + 1

    def add_text(value: str | None) -> bool:
        nonlocal total
        if value is None:
            return True
        size = len(value) * 6 + 2
        if size > remaining_bytes - total:
            return False
        total += size
        return True

    if not all(
        add_text(value)
        for value in (
            item.role.value,
            item.content,
            item.name,
            item.tool_call_id,
            item.reasoning_content,
            item.synthetic_reason.value if item.synthetic_reason is not None else None,
        )
    ):
        return remaining_bytes + 1
    for call in item.tool_calls:
        total += 128
        if total > remaining_bytes:
            return remaining_bytes + 1
        if not add_text(call.id) or not add_text(call.name):
            return remaining_bytes + 1
        for json_value in (call.arguments, call.metadata):
            size = _bounded_json_upper_bytes(
                json_value,
                remaining_bytes - total,
                scan_values,
            )
            if size > remaining_bytes - total:
                return remaining_bytes + 1
            total += size
    for part in item.content_parts:
        total += 128
        if total > remaining_bytes:
            return remaining_bytes + 1
        for text_value in (part.kind.value, part.text, part.url, part.data, part.mime_type):
            if not add_text(text_value):
                return remaining_bytes + 1
    return total


def _context_within_scan_budget(items: Sequence[SessionItem]) -> bool:
    if len(items) > MAX_MICROCOMPACTION_SCAN_ITEMS:
        return False
    remaining_bytes = MAX_MICROCOMPACTION_CONTEXT_BYTES
    scan_values = [MAX_MICROCOMPACTION_SCAN_VALUES]
    for item in items:
        item_bytes = _item_upper_bytes(item, remaining_bytes, scan_values)
        if item_bytes > remaining_bytes:
            return False
        remaining_bytes -= item_bytes
    return True


def _stable_stream(items: Sequence[SessionItem]) -> tuple[SessionItem, ...]:
    return tuple(
        item
        for item in items
        if not (isinstance(item, Message) and item.synthetic_reason in _VOLATILE_TAIL_REASONS)
    )


def _stable_prefix_fingerprint(items: Sequence[SessionItem]) -> str:
    prefix: list[bytes] = []
    for item in items:
        if isinstance(item, Message) and item.role is Role.USER and item.synthetic_reason is None:
            break
        prefix.append(_item_bytes(item))
    digest = hashlib.sha256()
    for payload in prefix:
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _tool_groups(items: Sequence[SessionItem]) -> tuple[_ToolGroup, ...]:
    groups: list[_ToolGroup] = []
    index = 0
    while index < len(items):
        item = items[index]
        if not isinstance(item, Message) or item.role is not Role.ASSISTANT or not item.tool_calls:
            index += 1
            continue
        if len(item.tool_calls) > MAX_MICROCOMPACTION_CALLS_PER_GROUP:
            groups.append(_ToolGroup(index, index + 1, (), (), False))
            index += 1
            continue
        call_ids = tuple(call.id for call in item.tool_calls)
        expected_end = index + 1 + len(call_ids)
        matched = expected_end <= len(items)
        result_indices: list[int] = []
        if matched:
            for offset, call_id in enumerate(call_ids, start=1):
                result = items[index + offset]
                if (
                    not isinstance(result, Message)
                    or result.role is not Role.TOOL
                    or result.tool_call_id != call_id
                    or result.synthetic_reason is not None
                ):
                    matched = False
                    break
                result_indices.append(index + offset)
        if matched:
            groups.append(_ToolGroup(index, expected_end, call_ids, tuple(result_indices), True))
            index = expected_end
            continue

        end = index + 1
        while end < len(items):
            candidate = items[end]
            if not isinstance(candidate, Message) or candidate.role is not Role.TOOL:
                break
            end += 1
        groups.append(_ToolGroup(index, end, call_ids, (), False))
        index = end
    return tuple(groups)


def _group_fingerprint(items: Sequence[SessionItem], group: _ToolGroup) -> str:
    digest = hashlib.sha256()
    for item in items[group.first_index : group.end_index]:
        payload = _item_bytes(item)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _is_binary_or_media_result(message: Message) -> bool:
    if message.content_parts:
        return True
    content = message.model_content()
    if "\x00" in content or "data:" in content[:128].casefold():
        return True
    binary_controls = sum(
        ord(character) < 32 and character not in "\n\r\t" for character in content
    )
    return bool(content) and binary_controls / len(content) >= 0.01


def _group_contains_media(items: Sequence[SessionItem], group: _ToolGroup) -> bool:
    for result_index in group.result_indices:
        result = items[result_index]
        if not isinstance(result, Message) or _is_binary_or_media_result(result):
            return True
    return False


def _replace_group_results(
    items: Sequence[SessionItem],
    groups: Sequence[_ToolGroup],
    fingerprints: set[str],
) -> tuple[SessionItem, ...]:
    replacements: dict[int, Message] = {}
    for group in groups:
        if not group.complete or _group_fingerprint(items, group) not in fingerprints:
            continue
        for result_index in group.result_indices:
            result = items[result_index]
            assert isinstance(result, Message)
            replacements[result_index] = replace(result, content=_MICROCOMPACTION_MARKER)
    return tuple(replacements.get(index, item) for index, item in enumerate(items))


class MicrocompactionRuntimeState:
    """Bounded, session-generation-scoped owner for microcompaction state.

    State is intentionally in memory only. Every compacted group is identified
    by a digest of its exact call/result group; append-only requests reapply the
    same marker set, and restart begins with no projection state. Terminal
    result status is learned from runtime-owned events; unknown results fail
    closed and are never compacted.

    按 session 和 context generation 管理有界 microcompaction 状态. 状态只驻留内存; 每组以
    精确调用/结果内容的 digest 标识, 追加式请求重用同一 marker 集合, 重启后不恢复临时投影. 终态由
    Runtime 自有事件提供; 未知状态的结果 fail closed, 不会被压缩.
    """

    __slots__ = ("_scope", "_snapshot", "_tool_result_statuses")

    def __init__(self) -> None:
        self._scope: tuple[str | None, int] | None = None
        self._snapshot: MicrocompactionSnapshot | None = None
        self._tool_result_statuses: OrderedDict[str, bool] = OrderedDict()

    @property
    def snapshot(self) -> MicrocompactionSnapshot | None:
        return self._snapshot

    def begin_scope(self, session_id: str | None, context_generation: int) -> None:
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            raise ValueError("session_id must be non-empty or None")
        _require_count("context_generation", context_generation)
        scope = (session_id, context_generation)
        if self._scope != scope:
            self._scope = scope
            self._snapshot = None
            self._tool_result_statuses.clear()

    def record_tool_result_status(self, call_id: str, *, is_error: bool) -> None:
        if not isinstance(call_id, str) or not call_id or len(call_id.encode("utf-8")) > 512:
            return
        self._tool_result_statuses.pop(call_id, None)
        self._tool_result_statuses[call_id] = is_error
        while len(self._tool_result_statuses) > MAX_TRACKED_TOOL_RESULTS:
            self._tool_result_statuses.popitem(last=False)

    def project_cached(
        self,
        context: ModelContext,
        *,
        session_id: str | None,
        context_generation: int,
        compaction_id: str | None,
        _source_checked: bool = False,
    ) -> MicrocompactionEvaluation:
        """Reapply an existing snapshot without evaluating another trigger."""

        self.begin_scope(session_id, context_generation)
        snapshot = self._snapshot
        if snapshot is None:
            return MicrocompactionEvaluation(context, None, False)
        if not _source_checked and not _context_within_scan_budget(context.items):
            return MicrocompactionEvaluation(context, None, False)
        if not snapshot.compacted_group_fingerprints:
            if compaction_id != snapshot.compaction_id:
                self._snapshot = replace(
                    snapshot,
                    stable_boundary=len(_stable_stream(context.items)),
                    stable_prefix_fingerprint=_stable_prefix_fingerprint(context.items),
                    compaction_id=compaction_id,
                )
            return MicrocompactionEvaluation(context, None, False)
        groups = _tool_groups(context.items)
        current_user_indices = [
            index
            for index, item in enumerate(context.items)
            if isinstance(item, Message)
            and item.role is Role.USER
            and item.synthetic_reason is None
        ]
        current_user_index = current_user_indices[-1] if current_user_indices else None
        prior_group_fingerprints = tuple(
            _group_fingerprint(context.items, group)
            for group in groups
            if group.complete
            and current_user_index is not None
            and group.end_index <= current_user_index
        )
        fingerprint_counts = Counter(prior_group_fingerprints)
        applicable = {
            fingerprint for fingerprint, count in fingerprint_counts.items() if count == 1
        }
        matching = applicable.intersection(snapshot.compacted_group_fingerprints)
        projected_items = _replace_group_results(context.items, groups, matching)
        has_compacted = bool(matching)
        projected = replace(context, items=projected_items) if has_compacted else context
        if compaction_id != snapshot.compaction_id:
            self._snapshot = replace(
                snapshot,
                stable_boundary=len(_stable_stream(context.items)),
                stable_prefix_fingerprint=_stable_prefix_fingerprint(context.items),
                compaction_id=compaction_id,
                compacted_group_fingerprints=tuple(sorted(matching)),
            )
        return MicrocompactionEvaluation(projected, None, has_compacted)

    def _record_source_limit_noop(
        self,
        context: ModelContext,
        *,
        session_id: str | None,
        context_generation: int,
        compaction_id: str | None,
        trigger_reason: MicrocompactionTriggerReason,
    ) -> MicrocompactionEvaluation:
        stable_boundary = (
            len(_stable_stream(context.items))
            if len(context.items) <= MAX_MICROCOMPACTION_SCAN_ITEMS
            else MAX_MICROCOMPACTION_SCAN_ITEMS + 1
        )
        fingerprint = hashlib.sha256(b"microcompaction-source-limit").hexdigest()
        previous = self._snapshot
        if (
            previous is not None
            and previous.stable_boundary == stable_boundary
            and previous.stable_prefix_fingerprint == fingerprint
            and previous.compaction_id == compaction_id
            and not previous.compacted_group_fingerprints
        ):
            return MicrocompactionEvaluation(context, None, False)
        telemetry = MicrocompactionTelemetry(
            trigger_reason,
            context_generation,
            stable_boundary,
            0,
            0,
            MAX_MICROCOMPACTION_CONTEXT_BYTES,
            MAX_MICROCOMPACTION_CONTEXT_BYTES,
            MAX_MICROCOMPACTION_CONTEXT_BYTES,
            MAX_MICROCOMPACTION_CONTEXT_BYTES,
            MicrocompactionNoopReason.SOURCE_LIMIT,
            estimates_saturated=True,
        )
        self._snapshot = (
            MicrocompactionSnapshot(
                session_id,
                context_generation,
                stable_boundary,
                fingerprint,
                compaction_id,
                (),
                telemetry,
            )
            if session_id is not None
            else None
        )
        return MicrocompactionEvaluation(context, telemetry, False)

    def evaluate(
        self,
        context: ModelContext,
        *,
        session_id: str | None,
        context_generation: int,
        current_user_message: Message | None,
        compaction_id: str | None,
        trigger_reason: MicrocompactionTriggerReason | None,
    ) -> MicrocompactionEvaluation:
        """Reapply the pinned batch and optionally evaluate one pressure trigger."""

        self.begin_scope(session_id, context_generation)
        if trigger_reason is not None and not isinstance(
            trigger_reason,
            MicrocompactionTriggerReason,
        ):
            raise TypeError("trigger_reason must be canonical or None")
        if not _context_within_scan_budget(context.items):
            if trigger_reason is None:
                return MicrocompactionEvaluation(context, None, False)
            return self._record_source_limit_noop(
                context,
                session_id=session_id,
                context_generation=context_generation,
                compaction_id=compaction_id,
                trigger_reason=trigger_reason,
            )
        previous_snapshot = self._snapshot
        cached = self.project_cached(
            context,
            session_id=session_id,
            context_generation=context_generation,
            compaction_id=compaction_id,
            _source_checked=True,
        )
        if trigger_reason is None:
            return cached

        stable_items = _stable_stream(context.items)
        stable_boundary = len(stable_items)
        prefix_fingerprint = _stable_prefix_fingerprint(context.items)
        noop_reason: MicrocompactionNoopReason | None = None
        if session_id is None:
            noop_reason = MicrocompactionNoopReason.NO_DURABLE_SESSION
        elif current_user_message is None or not any(
            item is current_user_message for item in context.items
        ):
            noop_reason = MicrocompactionNoopReason.NO_CURRENT_TURN_BOUNDARY

        if noop_reason is None and previous_snapshot is not None:
            context_boundary_changed = (
                prefix_fingerprint != previous_snapshot.stable_prefix_fingerprint
                or compaction_id != previous_snapshot.compaction_id
            )
            appended = (
                stable_items[previous_snapshot.stable_boundary :]
                if previous_snapshot.stable_boundary <= stable_boundary
                else ()
            )
            growth_ready = (
                len(appended) >= MIN_RETRIGGER_APPENDED_ITEMS
                or estimate_context_tokens(appended) >= MIN_RETRIGGER_APPENDED_TOKENS
            )
            if not context_boundary_changed and not growth_ready:
                return cached
        elif noop_reason is None and self._snapshot is None:
            pass

        groups = _tool_groups(context.items) if noop_reason is None else ()
        candidates: tuple[_ToolGroup, ...] = ()
        if noop_reason is None:
            current_user_index = next(
                (index for index, item in enumerate(context.items) if item is current_user_message),
                None,
            )
            if current_user_index is None:
                noop_reason = MicrocompactionNoopReason.NO_CURRENT_TURN_BOUNDARY
            else:
                prior_groups = tuple(
                    group for group in groups if group.end_index <= current_user_index
                )
                candidate_groups = prior_groups[:-PROTECTED_RECENT_TOOL_GROUPS]
                duplicate_ids = _duplicate_call_ids(groups)
                already_compacted = set(
                    self._snapshot.compacted_group_fingerprints
                    if self._snapshot is not None
                    else ()
                )
                candidates = tuple(
                    group
                    for group in candidate_groups
                    if group.complete
                    and not duplicate_ids.intersection(group.call_ids)
                    and all(
                        self._tool_result_statuses.get(call_id) is False
                        for call_id in group.call_ids
                    )
                    and not _group_contains_media(context.items, group)
                    and _group_fingerprint(context.items, group) not in already_compacted
                )
                if not candidates:
                    noop_reason = MicrocompactionNoopReason.NO_ELIGIBLE_GROUPS
                else:
                    remaining_slots = MAX_MICROCOMPACTED_GROUPS - len(already_compacted)
                    if remaining_slots <= 0:
                        candidates = ()
                        noop_reason = MicrocompactionNoopReason.STATE_LIMIT
                    elif len(candidates) > remaining_slots:
                        candidates = candidates[:remaining_slots]

        existing_fingerprints = set(
            self._snapshot.compacted_group_fingerprints if self._snapshot is not None else ()
        )
        candidate_fingerprints = (
            {_group_fingerprint(context.items, group) for group in candidates}
            if noop_reason is None
            else set()
        )
        before_context = cached.context
        before_bytes = _context_bytes(before_context.items)
        before_tokens = estimate_context_tokens(before_context.items)
        proposed_fingerprints = existing_fingerprints | candidate_fingerprints
        if proposed_fingerprints:
            all_groups = _tool_groups(context.items)
            proposed_items = _replace_group_results(
                context.items,
                all_groups,
                proposed_fingerprints,
            )
            proposed_context = replace(context, items=proposed_items)
        else:
            proposed_context = context
        after_bytes = _context_bytes(proposed_context.items)
        after_tokens = estimate_context_tokens(proposed_context.items)
        newly_compacted = bool(candidate_fingerprints)

        if noop_reason is None and (
            before_bytes - after_bytes < MIN_MICROCOMPACTION_SAVINGS_BYTES
            or before_tokens - after_tokens < MIN_MICROCOMPACTION_SAVINGS_TOKENS
        ):
            noop_reason = MicrocompactionNoopReason.INSUFFICIENT_SAVINGS
            proposed_context = before_context
            proposed_fingerprints = existing_fingerprints
            after_bytes = before_bytes
            after_tokens = before_tokens
            newly_compacted = False

        compacted_groups = len(candidate_fingerprints) if newly_compacted else 0
        compacted_results = (
            sum(len(group.call_ids) for group in candidates) if newly_compacted else 0
        )
        telemetry = MicrocompactionTelemetry(
            trigger_reason,
            context_generation,
            stable_boundary,
            compacted_groups,
            compacted_results,
            before_bytes,
            after_bytes,
            before_tokens,
            after_tokens,
            noop_reason,
        )
        state_fingerprints = tuple(sorted(proposed_fingerprints))[:MAX_MICROCOMPACTED_GROUPS]
        self._snapshot = (
            MicrocompactionSnapshot(
                session_id or "no-session",
                context_generation,
                stable_boundary,
                prefix_fingerprint,
                compaction_id,
                state_fingerprints,
                telemetry,
            )
            if session_id is not None
            else None
        )
        return MicrocompactionEvaluation(
            proposed_context,
            telemetry,
            bool(state_fingerprints and newly_compacted) or cached.has_compacted_results,
        )


def _duplicate_call_ids(groups: Sequence[_ToolGroup]) -> set[str]:
    counts = Counter(call_id for group in groups for call_id in group.call_ids)
    return {call_id for call_id, count in counts.items() if count > 1}


__all__ = [
    "MAX_MICROCOMPACTED_GROUPS",
    "MAX_MICROCOMPACTION_CALLS_PER_GROUP",
    "MAX_MICROCOMPACTION_CONTENT_PARTS",
    "MAX_MICROCOMPACTION_CONTEXT_BYTES",
    "MAX_MICROCOMPACTION_SCAN_ITEMS",
    "MAX_MICROCOMPACTION_SCAN_VALUES",
    "MAX_MICROCOMPACTION_VALUE_DEPTH",
    "MAX_TRACKED_TOOL_RESULTS",
    "MIN_MICROCOMPACTION_SAVINGS_BYTES",
    "MIN_MICROCOMPACTION_SAVINGS_TOKENS",
    "MIN_RETRIGGER_APPENDED_ITEMS",
    "MIN_RETRIGGER_APPENDED_TOKENS",
    "PROTECTED_RECENT_TOOL_GROUPS",
    "MicrocompactionEvaluation",
    "MicrocompactionNoopReason",
    "MicrocompactionRuntimeState",
    "MicrocompactionSnapshot",
    "MicrocompactionTelemetry",
    "MicrocompactionTriggerReason",
]
