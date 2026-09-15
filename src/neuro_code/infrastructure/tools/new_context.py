"""Model-facing bounded fresh-context rollover control.

The control is session-bound by bootstrap and receives no model-selectable
session identifier.  Its durable write is intentionally separate from the
canonical SessionItem history.

面向模型的有界 fresh-context rollover control.

该 control 由 bootstrap 绑定到 session, 模型不能选择 session identifier。其 durable write
有意与 canonical SessionItem history 分离.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.context_rollover import (
    CONTEXT_ROLLOVER_TOOL_NAME,
    MAX_CONTEXT_GENERATION,
    AdvanceContextRolloverRequest,
    ContextRolloverController,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.tools import ToolDefinition, ToolExecutionMode, ToolResult
from neuro_code.shared.errors import SessionError, ToolError


def _encoded_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _ack_payload(generation: int) -> dict[str, object]:
    return {
        "operation": CONTEXT_ROLLOVER_TOOL_NAME,
        "generation": generation,
        "durable_write": True,
        "fresh_context": True,
    }


class NewContextTool:
    """Advance the active context for the runtime-bound session."""

    definition = ToolDefinition(
        name=CONTEXT_ROLLOVER_TOOL_NAME,
        execution_mode=ToolExecutionMode.EXCLUSIVE,
        description=(
            "Start the next model step in a fresh active context for the current session. "
            "Canonical history and the current Working Set remain available through their "
            "bounded session tools. This control takes no arguments and does not create a "
            "new session."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )

    # The existing flag governs workspace/shell mutation.  This control only
    # changes durable session metadata and therefore does not enter workspace
    # undo or verification mutation accounting.
    side_effecting = False

    def __init__(self, controller: ContextRolloverController) -> None:
        self._controller = controller

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not isinstance(arguments, Mapping) or set(arguments):
            raise ToolError("new_context takes no arguments")
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        session_id = context.session_id
        if not isinstance(session_id, str) or not session_id.strip() or "\x00" in session_id:
            raise ToolError("new_context requires a bound session")

        # Generation is bounded by the durable integer contract.  Checking
        # the maximum representation before the write prevents a committed
        # control from becoming an output-limit failure.
        maximum_ack = _encoded_json(_ack_payload(MAX_CONTEXT_GENERATION))
        if len(maximum_ack.encode("utf-8")) > context.output_byte_limit:
            raise ToolError("output_byte_limit cannot represent a context rollover acknowledgement")
        try:
            state = await self._controller.advance_context_rollover(
                AdvanceContextRolloverRequest(
                    session_id,
                    history_item_boundary=context.context_rollover_item_boundary,
                    turn_id=context.turn_id,
                )
            )
        except (SessionError, TypeError, ValueError) as error:
            raise ToolError("context rollover could not be completed") from error
        return ToolResult(
            _encoded_json(_ack_payload(state.generation)),
            metadata={
                "context_rollover": True,
                "generation": state.generation,
                "durable_write": True,
            },
        )


__all__ = ["NewContextTool"]
