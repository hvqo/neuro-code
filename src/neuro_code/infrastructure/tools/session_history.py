"""Model-facing bounded read-only durable session history tool.

The tool is bound to the runtime-owned session identity carried by
``ToolContext``.  The model can receive opaque references from this session,
but it cannot choose a session ID or access another session's transcript.

面向模型的有界只读持久化会话历史工具.

工具绑定到 ``ToolContext`` 中由 Runtime 持有的会话身份.模型只能使用当前会话返回的不透明
引用,不能选择 session ID 或访问其他会话的 transcript.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from enum import StrEnum
from typing import Any

from neuro_code.application.ports.session_history import (
    MAX_SESSION_ITEM_LIST_LIMIT,
    MAX_SESSION_ITEM_OFFSET,
    MAX_SESSION_ITEM_READ_BYTES,
    MAX_SESSION_ITEM_READ_OFFSET,
    MIN_SESSION_ITEM_READ_BYTES,
    ListSessionItemsRequest,
    ReadSessionItemRequest,
    SearchSessionItemsRequest,
    SessionHistoryQueryController,
    SessionItemPage,
    SessionItemRead,
    SessionItemReference,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import SessionError, ToolError


class _HistoryOperation(StrEnum):
    LIST = "list"
    SEARCH = "search"
    READ = "read"


class SessionHistoryTool:
    """Expose only the current bound session's safe durable item projection."""

    definition = ToolDefinition(
        name="session_history",
        description=(
            "Read the current session's durable conversation history without changing it. "
            "Use list for newest-first item metadata, search for bounded matches in visible "
            "user/assistant/tool-result text, and read for one exact item chunk using a "
            "reference returned by list or search. References are session-scoped; never invent "
            "or modify one. Hidden reasoning, provider-private context, synthetic runtime "
            "messages, and internal payloads are not available."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [operation.value for operation in _HistoryOperation],
                },
                "query": {
                    "type": "string",
                    "maxLength": 512,
                    "description": "Required for search; visible text to find.",
                },
                "reference": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Required for read; an opaque reference returned by this tool.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_SESSION_ITEM_READ_OFFSET,
                    "description": "Page offset for list/search or UTF-8 byte offset for read.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SESSION_ITEM_LIST_LIMIT,
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": MIN_SESSION_ITEM_READ_BYTES,
                    "maximum": MAX_SESSION_ITEM_READ_BYTES,
                    "description": "Maximum UTF-8 bytes returned by one read operation.",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    def __init__(self, query: SessionHistoryQueryController) -> None:
        self._query = query

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not isinstance(arguments, Mapping):
            raise ToolError("session_history arguments must be an object")
        operation = self._operation(arguments)
        session_id = context.session_id
        if not isinstance(session_id, str) or not session_id.strip() or "\x00" in session_id:
            raise ToolError("session history requires a bound session")
        if context.output_byte_limit <= 0:
            raise ToolError("output_byte_limit must be positive")
        try:
            if operation is _HistoryOperation.LIST:
                return await self._list(arguments, session_id, context)
            if operation is _HistoryOperation.SEARCH:
                return await self._search(arguments, session_id, context)
            return await self._read(arguments, session_id, context)
        except (SessionError, ValueError) as error:
            raise ToolError("session history request could not be completed") from error

    @staticmethod
    def _operation(arguments: Mapping[str, Any]) -> _HistoryOperation:
        unsupported = set(arguments).difference(
            {"operation", "query", "reference", "offset", "limit", "max_bytes"}
        )
        if unsupported:
            raise ToolError("session_history contains unsupported fields")
        raw = arguments.get("operation")
        if not isinstance(raw, str):
            raise ToolError("session_history.operation must be a string")
        try:
            return _HistoryOperation(raw)
        except ValueError as error:
            raise ToolError("session_history.operation is invalid") from error

    @staticmethod
    def _integer(
        arguments: Mapping[str, Any],
        name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = arguments.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ToolError(f"session_history.{name} is outside its bound")
        return value

    @classmethod
    def _list_request(
        cls, arguments: Mapping[str, Any], session_id: str
    ) -> ListSessionItemsRequest:
        unsupported = set(arguments).difference({"operation", "limit", "offset"})
        if unsupported:
            raise ToolError("list operation contains unsupported fields")
        return ListSessionItemsRequest(
            session_id,
            limit=cls._integer(
                arguments,
                "limit",
                default=20,
                minimum=1,
                maximum=MAX_SESSION_ITEM_LIST_LIMIT,
            ),
            offset=cls._integer(
                arguments,
                "offset",
                default=0,
                minimum=0,
                maximum=MAX_SESSION_ITEM_OFFSET,
            ),
        )

    @classmethod
    def _search_request(
        cls,
        arguments: Mapping[str, Any],
        session_id: str,
    ) -> SearchSessionItemsRequest:
        unsupported = set(arguments).difference({"operation", "query", "limit", "offset"})
        if unsupported:
            raise ToolError("search operation contains unsupported fields")
        query = arguments.get("query")
        if not isinstance(query, str):
            raise ToolError("session_history.query must be a string")
        return SearchSessionItemsRequest(
            session_id,
            query,
            limit=cls._integer(
                arguments,
                "limit",
                default=20,
                minimum=1,
                maximum=MAX_SESSION_ITEM_LIST_LIMIT,
            ),
            offset=cls._integer(
                arguments,
                "offset",
                default=0,
                minimum=0,
                maximum=MAX_SESSION_ITEM_OFFSET,
            ),
        )

    @classmethod
    def _read_request(
        cls,
        arguments: Mapping[str, Any],
        session_id: str,
    ) -> ReadSessionItemRequest:
        unsupported = set(arguments).difference({"operation", "reference", "offset", "max_bytes"})
        if unsupported:
            raise ToolError("read operation contains unsupported fields")
        raw_reference = arguments.get("reference")
        if not isinstance(raw_reference, str):
            raise ToolError("session_history.reference must be a string")
        try:
            reference = SessionItemReference(raw_reference)
        except ValueError as error:
            raise ToolError("session_history.reference is invalid") from error
        return ReadSessionItemRequest(
            session_id,
            reference,
            offset=cls._integer(
                arguments,
                "offset",
                default=0,
                minimum=0,
                maximum=MAX_SESSION_ITEM_READ_OFFSET,
            ),
            max_bytes=cls._integer(
                arguments,
                "max_bytes",
                default=MAX_SESSION_ITEM_READ_BYTES,
                minimum=MIN_SESSION_ITEM_READ_BYTES,
                maximum=MAX_SESSION_ITEM_READ_BYTES,
            ),
        )

    async def _list(
        self,
        arguments: Mapping[str, Any],
        session_id: str,
        context: ToolContext,
    ) -> ToolResult:
        request = self._list_request(arguments, session_id)
        return await self._render_page(
            _HistoryOperation.LIST,
            request,
            context,
        )

    async def _search(
        self,
        arguments: Mapping[str, Any],
        session_id: str,
        context: ToolContext,
    ) -> ToolResult:
        request = self._search_request(arguments, session_id)
        return await self._render_page(
            _HistoryOperation.SEARCH,
            request,
            context,
        )

    async def _read(
        self,
        arguments: Mapping[str, Any],
        session_id: str,
        context: ToolContext,
    ) -> ToolResult:
        request = self._read_request(arguments, session_id)
        max_bytes = request.max_bytes
        while True:
            result = await self._query.read_session_item(replace(request, max_bytes=max_bytes))
            rendered = self._render_read(result)
            if self._fits(rendered, context.output_byte_limit):
                return ToolResult(
                    rendered,
                    metadata={
                        "read_only": True,
                        "complete": result.next_offset is None,
                        "operation": _HistoryOperation.READ.value,
                    },
                )
            if max_bytes == MIN_SESSION_ITEM_READ_BYTES:
                return self._output_limit_error(context.output_byte_limit)
            max_bytes = max(MIN_SESSION_ITEM_READ_BYTES, max_bytes // 2)

    async def _render_page(
        self,
        operation: _HistoryOperation,
        request: ListSessionItemsRequest | SearchSessionItemsRequest,
        context: ToolContext,
    ) -> ToolResult:
        limit = request.limit
        while True:
            bounded_request = replace(request, limit=limit)
            if isinstance(bounded_request, ListSessionItemsRequest):
                page = await self._query.list_session_items(bounded_request)
            else:
                page = await self._query.search_session_items(bounded_request)
            rendered = self._render_page_payload(operation, page)
            if self._fits(rendered, context.output_byte_limit):
                return ToolResult(
                    rendered,
                    metadata={
                        "read_only": True,
                        "complete": page.next_offset is None,
                        "operation": operation.value,
                    },
                )
            if limit == 1:
                return self._output_limit_error(context.output_byte_limit)
            limit = max(1, limit // 2)

    @staticmethod
    def _render_page_payload(operation: _HistoryOperation, page: SessionItemPage) -> str:
        return json.dumps(
            {"operation": operation.value, **page.to_dict()},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _render_read(result: SessionItemRead) -> str:
        return json.dumps(
            {"operation": _HistoryOperation.READ.value, "item": result.to_dict()},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _fits(value: str, byte_limit: int) -> bool:
        return len(value.encode("utf-8")) <= byte_limit

    @staticmethod
    def _output_limit_error(byte_limit: int) -> ToolResult:
        message = "history result exceeds output limit; narrow the request"
        if len(message.encode("utf-8")) > byte_limit:
            message = message.encode("utf-8")[:byte_limit].decode("utf-8", "ignore")
        return ToolResult(
            message,
            is_error=True,
            metadata={"failure_kind": "output_limit", "read_only": True},
        )


__all__ = ["SessionHistoryTool"]
