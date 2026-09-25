"""Bounded final-wire request fingerprints for development diagnostics.

面向开发诊断的有界最终请求 wire 指纹。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelRequestTrajectoryObserved
from neuro_code.domain.conversation.prompt_continuity import CacheBoundaryReason

MAX_TRAJECTORY_FINGERPRINTS = 512
MAX_TRAJECTORY_BINDINGS = 64
_PROCESS_FINGERPRINT_KEY = os.urandom(32)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any, *, key: bytes) -> str:
    return hmac.new(key, _canonical_json(value), hashlib.sha256).hexdigest()


def _messages_from_body(body: dict[str, Any]) -> tuple[list[Any], tuple[Any, ...]]:
    """Return provider-visible ordered messages and separately placed prefix fields."""

    message_key = next(
        (key for key in ("messages", "input", "contents") if isinstance(body.get(key), list)),
        None,
    )
    messages = body.get(message_key, []) if message_key is not None else []
    prefix: list[Any] = []
    for key in (
        "system",
        "instructions",
        "system_instruction",
        "systemInstruction",
    ):
        if key in body:
            prefix.append({key: body[key]})
    return messages, tuple(prefix)


def _tool_definition_count(body: dict[str, Any]) -> int:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return 0
    count = 0
    for tool in tools:
        if not isinstance(tool, dict):
            count += 1
            continue
        declarations = next(
            (
                tool[key]
                for key in ("functionDeclarations", "function_declarations")
                if isinstance(tool.get(key), list)
            ),
            None,
        )
        count += len(declarations) if isinstance(declarations, list) else 1
    return count


def _leading_authority_messages(messages: list[Any]) -> tuple[Any, ...]:
    authority: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            break
        role = message.get("role")
        if role not in {"system", "developer"}:
            break
        authority.append(message)
    return tuple(authority)


@dataclass(frozen=True, slots=True)
class _PriorProjection:
    epoch: int
    provider: str
    model: str
    source: str
    message_fingerprints: tuple[str, ...]
    message_count: int
    fingerprints_truncated: bool
    stable_prefix_fingerprint: str
    tools_fingerprint: str
    request_options_fingerprint: str
    sequence: int


class ProviderRequestTrajectoryRecorder:
    """Compare final serialized request shapes without retaining their content."""

    __slots__ = ("_key", "_prior")

    def __init__(self, *, fingerprint_key: bytes | None = None) -> None:
        key = fingerprint_key if fingerprint_key is not None else _PROCESS_FINGERPRINT_KEY
        if not isinstance(key, bytes) or len(key) < 16:
            raise ValueError("trajectory fingerprint key must contain at least 16 bytes")
        self._key = key
        # Keep independent baselines per request source. Auxiliary traffic
        # (for example a finalizer or search sidecar) must not replace the
        # preceding MAIN_TURN projection and hide its next divergence.
        self._prior: OrderedDict[tuple[str, str], _PriorProjection] = OrderedDict()

    def observe(
        self,
        body: dict[str, Any],
        *,
        context: ModelContext,
        provider: str,
        model: str,
    ) -> ModelRequestTrajectoryObserved | None:
        if not context.prompt_trajectory_enabled or context.trajectory_id is None:
            return None

        messages, prefix_fields = _messages_from_body(body)
        tools = body.get("tools", [])
        if not isinstance(tools, list):
            tools = []
        fingerprints_truncated = len(messages) > MAX_TRAJECTORY_FINGERPRINTS
        message_fingerprints = (
            tuple(_digest(message, key=self._key) for message in messages)
            if not fingerprints_truncated
            else ()
        )
        tools_fingerprint = _digest(tools, key=self._key)
        stable_prefix_fingerprint = _digest(
            {
                "separate_prefix": prefix_fields,
                "leading_authority_messages": _leading_authority_messages(messages),
                "tools": tools,
            },
            key=self._key,
        )
        message_key = next(
            (key for key in ("messages", "input", "contents") if isinstance(body.get(key), list)),
            None,
        )
        other_request_fields = {
            key: value for key, value in body.items() if key not in {message_key, "tools"}
        }
        request_options_fingerprint = _digest(
            {"message_field": message_key, "fields": other_request_fields},
            key=self._key,
        )
        request_fingerprint = (
            _digest(
                {
                    "message_field": message_key,
                    "messages": message_fingerprints,
                    "message_count": len(messages),
                    "tools_fingerprint": tools_fingerprint,
                    "request_options_fingerprint": request_options_fingerprint,
                },
                key=self._key,
            )
            if not fingerprints_truncated
            else None
        )

        trajectory_key = (context.trajectory_id, context.request_source.value)
        previous = self._prior.get(trajectory_key)
        sequence = previous.sequence + 1 if previous is not None else 1
        common: int | None = None
        divergence: int | None = None
        previous_count: int | None = None
        append_only: bool | None = None
        boundary_reason = context.cache_boundary_reason
        comparable = (
            previous is not None
            and previous.epoch == context.cache_epoch
            and previous.provider == provider
            and previous.model == model
            and previous.source == context.request_source.value
            and not previous.fingerprints_truncated
            and not fingerprints_truncated
        )
        if previous is not None and previous.epoch == context.cache_epoch:
            previous_count = previous.message_count
        if comparable and previous is not None:
            common = 0
            for old, new in zip(previous.message_fingerprints, message_fingerprints, strict=False):
                if old != new:
                    break
                common += 1
            if common < len(previous.message_fingerprints):
                divergence = common
            messages_append_only = common == len(previous.message_fingerprints) and len(
                message_fingerprints
            ) >= len(previous.message_fingerprints)
            tools_unchanged = previous.tools_fingerprint == tools_fingerprint
            stable_prefix_unchanged = (
                previous.stable_prefix_fingerprint == stable_prefix_fingerprint
            )
            options_unchanged = previous.request_options_fingerprint == request_options_fingerprint
            append_only = (
                messages_append_only
                and tools_unchanged
                and stable_prefix_unchanged
                and options_unchanged
            )
            if not tools_unchanged and boundary_reason is None:
                boundary_reason = CacheBoundaryReason.TOOL_SCHEMA_CHANGE
            elif not stable_prefix_unchanged and boundary_reason is None:
                boundary_reason = CacheBoundaryReason.INSTRUCTION_AUTHORITY_CHANGE
            elif not options_unchanged and boundary_reason is None:
                boundary_reason = CacheBoundaryReason.CONFIG_RELOAD
        elif previous is not None and previous.epoch == context.cache_epoch:
            if previous.provider != provider:
                boundary_reason = boundary_reason or CacheBoundaryReason.PROVIDER_SWITCH
            elif previous.model != model:
                boundary_reason = boundary_reason or CacheBoundaryReason.MODEL_SWITCH
            elif previous.tools_fingerprint != tools_fingerprint:
                boundary_reason = boundary_reason or CacheBoundaryReason.TOOL_SCHEMA_CHANGE

        event = ModelRequestTrajectoryObserved(
            sequence=sequence,
            source=context.request_source,
            provider=provider,
            model=model,
            context_generation=context.context_generation,
            cache_epoch=context.cache_epoch,
            boundary_reason=boundary_reason,
            message_fingerprints=message_fingerprints,
            message_count=len(messages),
            tool_count=_tool_definition_count(body),
            tools_fingerprint=tools_fingerprint,
            stable_prefix_fingerprint=stable_prefix_fingerprint,
            request_fingerprint=request_fingerprint,
            common_prefix_messages=common,
            first_divergence_index=divergence,
            previous_message_count=previous_count,
            append_only=append_only,
            fingerprints_truncated=fingerprints_truncated,
        )
        self._prior[trajectory_key] = _PriorProjection(
            context.cache_epoch,
            provider,
            model,
            context.request_source.value,
            message_fingerprints,
            len(messages),
            fingerprints_truncated,
            stable_prefix_fingerprint,
            tools_fingerprint,
            request_options_fingerprint,
            sequence,
        )
        self._prior.move_to_end(trajectory_key)
        while len(self._prior) > MAX_TRAJECTORY_BINDINGS:
            self._prior.popitem(last=False)
        return event


DEFAULT_REQUEST_TRAJECTORY_RECORDER = ProviderRequestTrajectoryRecorder()


__all__ = [
    "DEFAULT_REQUEST_TRAJECTORY_RECORDER",
    "MAX_TRAJECTORY_BINDINGS",
    "MAX_TRAJECTORY_FINGERPRINTS",
    "ProviderRequestTrajectoryRecorder",
]
