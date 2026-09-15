"""Model-facing bounded current-session Working Set tool.

The tool accepts a complete typed replacement guarded by a revision.  The
runtime-owned ``ToolContext.session_id`` is the only session selector; no
model argument can choose another session.

面向模型的有界当前会话 Working Set tool.

该 tool 接受由 revision 保护的完整类型化替换.唯一的会话选择器是 Runtime 所有的
``ToolContext.session_id``;模型参数不能选择其他会话.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.working_set import (
    MAX_WORKING_SET_ENTRIES_PER_SECTION,
    MAX_WORKING_SET_TOTAL_BYTES,
    WORKING_SET_SECTION_ORDER,
    ReadWorkingSetRequest,
    UpdateWorkingSetRequest,
    WorkingSetController,
    WorkingSetSnapshot,
    WorkingSetUpdate,
)
from neuro_code.domain.tools import ToolDefinition, ToolExecutionMode, ToolResult
from neuro_code.shared.errors import SessionError, ToolError


class _WorkingSetOperation(StrEnum):
    READ = "read"
    UPDATE = "update"


def _update_ack_payload(
    *,
    revision: int,
    entry_count: int,
    text_bytes: int,
) -> dict[str, object]:
    return {
        "operation": _WorkingSetOperation.UPDATE.value,
        "revision": revision,
        "entry_count": entry_count,
        "text_bytes": text_bytes,
        "durable_write": True,
        "snapshot_omitted": True,
    }


def _encoded_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _update_ack_bytes(
    *,
    revision: int,
    entry_count: int,
    text_bytes: int,
) -> int:
    return len(
        _encoded_json(
            _update_ack_payload(
                revision=revision,
                entry_count=entry_count,
                text_bytes=text_bytes,
            )
        ).encode("utf-8")
    )


class SessionWorkingSetTool:
    """Read or replace only the currently bound session's Working Set."""

    definition = ToolDefinition(
        name="session_working_set",
        execution_mode=ToolExecutionMode.EXCLUSIVE,
        description=(
            "Read or replace the current session's bounded structured task state. "
            "Use read for the current snapshot. Use update for a complete replacement "
            "with the expected revision and all six sections; updates are durable session "
            "metadata only and never change workspace files or conversation history. "
            "History source_ref values must come from this session's session_history tool."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [operation.value for operation in _WorkingSetOperation],
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Required for update; use the last returned revision.",
                },
                "sections": {
                    "type": "object",
                    "properties": {
                        section.value: {
                            "type": "array",
                            "maxItems": MAX_WORKING_SET_ENTRIES_PER_SECTION,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "source_ref": {"type": ["string", "null"]},
                                },
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                        }
                        for section in WORKING_SET_SECTION_ORDER
                    },
                    "required": [section.value for section in WORKING_SET_SECTION_ORDER],
                    "additionalProperties": False,
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    )

    # ``side_effecting`` denotes workspace/shell mutation in the existing
    # ToolExecutor contract. Like update_plan, this session-state write is
    # reported in result metadata without entering workspace undo/verification.
    side_effecting = False

    def __init__(self, controller: WorkingSetController) -> None:
        self._controller = controller

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not isinstance(arguments, Mapping):
            raise ToolError("session_working_set arguments must be an object")
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        session_id = context.session_id
        if not isinstance(session_id, str) or not session_id.strip() or "\x00" in session_id:
            raise ToolError("session working set requires a bound session")
        operation = self._operation(arguments)
        try:
            if operation is _WorkingSetOperation.READ:
                if set(arguments) != {"operation"}:
                    raise ToolError("read operation contains unsupported fields")
                snapshot = await self._controller.read_working_set(
                    ReadWorkingSetRequest(session_id)
                )
                return self._render(operation, snapshot, context, durable_write=False)
            if set(arguments) != {"operation", "expected_revision", "sections"}:
                raise ToolError("update operation contains unsupported fields")
            update = WorkingSetUpdate.from_dict(
                {
                    "expected_revision": arguments.get("expected_revision"),
                    "sections": arguments.get("sections"),
                }
            )
            entry_count = sum(len(state.entries) for state in update.sections)
            # Check the largest acknowledgement possible for this request
            # before the controller can perform a durable update. The revision
            # and entry count are request-bounded; text_bytes uses the
            # snapshot's total bound because configured redaction may expand
            # text before persistence.
            if (
                _update_ack_bytes(
                    revision=update.expected_revision + 1,
                    entry_count=entry_count,
                    text_bytes=MAX_WORKING_SET_TOTAL_BYTES,
                )
                > context.output_byte_limit
            ):
                raise ToolError(
                    "output_byte_limit cannot represent a working set update acknowledgement"
                )
            snapshot = await self._controller.update_working_set(
                UpdateWorkingSetRequest(session_id, update)
            )
            return self._render(operation, snapshot, context, durable_write=True)
        except ToolError:
            raise
        except (SessionError, TypeError, ValueError) as error:
            raise ToolError("session working set request could not be completed") from error

    @staticmethod
    def _operation(arguments: Mapping[str, Any]) -> _WorkingSetOperation:
        raw = arguments.get("operation")
        if not isinstance(raw, str):
            raise ToolError("session_working_set.operation must be a string")
        try:
            return _WorkingSetOperation(raw)
        except ValueError as error:
            raise ToolError("session_working_set.operation is invalid") from error

    @staticmethod
    def _render(
        operation: _WorkingSetOperation,
        snapshot: WorkingSetSnapshot,
        context: ToolContext,
        *,
        durable_write: bool,
    ) -> ToolResult:
        payload = _encoded_json(
            {
                "operation": operation.value,
                "working_set": snapshot.to_model_dict(),
            }
        )
        metadata = {
            "revision": snapshot.revision,
            "entry_count": snapshot.entry_count,
            "text_bytes": snapshot.text_bytes,
            "durable_write": durable_write,
        }
        if len(payload.encode("utf-8")) <= context.output_byte_limit:
            return ToolResult(payload, metadata=metadata)
        if operation is _WorkingSetOperation.UPDATE:
            acknowledgement = _encoded_json(
                _update_ack_payload(
                    revision=snapshot.revision,
                    entry_count=snapshot.entry_count,
                    text_bytes=snapshot.text_bytes,
                )
            )
            return ToolResult(
                acknowledgement,
                metadata={**metadata, "snapshot_omitted": True},
            )
        message = "working set result exceeds output limit"
        if len(message.encode("utf-8")) > context.output_byte_limit:
            message = message.encode("utf-8")[: context.output_byte_limit].decode("utf-8", "ignore")
        return ToolResult(
            message,
            is_error=True,
            metadata={**metadata, "failure_kind": "output_limit"},
        )


__all__ = ["SessionWorkingSetTool"]
