"""Typed application owner for read-only session-item queries.

该模块定义只读会话项查询的类型化应用 owner.

The ordered durable ``SessionItem`` sequence remains the only source of truth.
The addressable projections are derived on demand and never write a second
transcript or search-owned copy of history.

按需从唯一的持久化 ``SessionItem`` 有序序列派生可寻址投影,不会写入第二份 transcript
或拥有独立的搜索历史.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from neuro_code.application.ports.session_history import (
    MAX_SESSION_ITEM_PREVIEW_BYTES,
    ListSessionItemsRequest,
    ReadSessionItemRequest,
    SearchSessionItemsRequest,
    SessionItemPage,
    SessionItemRead,
    SessionItemReadKind,
    SessionItemReference,
    SessionItemSummary,
    _is_public_history_item,
)
from neuro_code.application.ports.storage import SessionStore
from neuro_code.domain.conversation.messages import Message, Role, SessionItem
from neuro_code.shared.errors import SessionError
from neuro_code.shared.redaction import redact_sensitive_text


def _history_kind(item: Message) -> SessionItemReadKind:
    return (
        SessionItemReadKind.TOOL_RESULT if item.role is Role.TOOL else SessionItemReadKind.MESSAGE
    )


def _safe_content(item: Message, redaction_values: Sequence[str]) -> str:
    if not isinstance(item.content, str):
        raise SessionError("session item content is invalid")
    return redact_sensitive_text(
        item.content.replace("\x00", ""),
        explicit_values=redaction_values,
    )


def _safe_tool_name(item: Message, redaction_values: Sequence[str]) -> str | None:
    if item.role is not Role.TOOL or not isinstance(item.name, str) or not item.name:
        return None
    return redact_sensitive_text(
        item.name.replace("\x00", ""),
        explicit_values=redaction_values,
    )


def _bounded_preview(value: str, *, limit: int = MAX_SESSION_ITEM_PREVIEW_BYTES) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return f"{encoded[: max(0, limit - 3)].decode('utf-8', 'ignore')}..."


def _search_snippet(value: str, query: str) -> str:
    folded = value.casefold()
    start = folded.find(query.casefold())
    if start < 0:
        return _bounded_preview(value)
    return _bounded_preview(value[max(0, start - 96) : start + len(query) + 160])


@dataclass(frozen=True, slots=True)
class LoadSessionItemsRequest:
    """Validated input for loading ordered persisted conversation items.

    用于加载按顺序持久化会话项的、经过验证的输入.
    """

    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must not be empty")


class SessionItemQueryController(Protocol):
    """Minimal read-only owner for persisted conversation items.

    表示持久化会话项使用的最小只读 owner 契约.
    """

    async def load_session_items(
        self,
        request: LoadSessionItemsRequest,
    ) -> tuple[SessionItem, ...]: ...

    async def list_session_items(self, request: ListSessionItemsRequest) -> SessionItemPage: ...

    async def search_session_items(self, request: SearchSessionItemsRequest) -> SessionItemPage: ...

    async def read_session_item(self, request: ReadSessionItemRequest) -> SessionItemRead: ...


class SessionItemQueryService:
    """Derive read-only history projections through the storage port.

    通过存储端口派生只读历史投影.
    """

    __slots__ = ("_redaction_values", "_store")

    def __init__(self, store: SessionStore, *, redaction_values: Sequence[str] = ()) -> None:
        self._store = store
        self._redaction_values = tuple(redaction_values)

    async def load_session_items(
        self,
        request: LoadSessionItemsRequest,
    ) -> tuple[SessionItem, ...]:
        """Load the immutable application projection without changing state.

        加载不可变应用投影,但不改变状态.
        """

        if not isinstance(request, LoadSessionItemsRequest):
            raise ValueError("load session items request must be canonical")
        return tuple(await self._store.load_session_items(request.session_id))

    async def list_session_items(self, request: ListSessionItemsRequest) -> SessionItemPage:
        """List newest-first safe metadata derived from the canonical sequence."""

        if not isinstance(request, ListSessionItemsRequest):
            raise ValueError("list session items request must be canonical")
        items = await self.load_session_items(LoadSessionItemsRequest(request.session_id))
        visible = [
            (ordinal, item)
            for ordinal, item in enumerate(items, start=1)
            if _is_public_history_item(item)
        ]
        page_records = list(reversed(visible))[request.offset : request.offset + request.limit]
        summaries = tuple(
            self._summary(request.session_id, ordinal, item) for ordinal, item in page_records
        )
        end = request.offset + len(summaries)
        return SessionItemPage(
            summaries,
            end if end < len(visible) else None,
            len(visible),
        )

    async def search_session_items(self, request: SearchSessionItemsRequest) -> SessionItemPage:
        """Search only safe visible text within one canonical session."""

        if not isinstance(request, SearchSessionItemsRequest):
            raise ValueError("search session items request must be canonical")
        items = await self.load_session_items(LoadSessionItemsRequest(request.session_id))
        query = request.query.strip()
        query_folded = query.casefold()
        total = 0
        matches: list[SessionItemSummary] = []
        for ordinal, item in enumerate(items, start=1):
            if not _is_public_history_item(item):
                continue
            assert isinstance(item, Message)
            content = _safe_content(item, self._redaction_values)
            tool_name = _safe_tool_name(item, self._redaction_values)
            searchable = "\n".join(part for part in (content, tool_name or "") if part)
            if query_folded not in searchable.casefold():
                continue
            if request.offset <= total < request.offset + request.limit:
                matches.append(
                    self._summary(
                        request.session_id,
                        ordinal,
                        item,
                        snippet=_search_snippet(searchable, query),
                    )
                )
            total += 1
        end = request.offset + len(matches)
        return SessionItemPage(tuple(matches), end if end < total else None, total)

    async def read_session_item(self, request: ReadSessionItemRequest) -> SessionItemRead:
        """Resolve one opaque reference and return one bounded safe text chunk."""

        if not isinstance(request, ReadSessionItemRequest):
            raise ValueError("read session item request must be canonical")
        items = await self.load_session_items(LoadSessionItemsRequest(request.session_id))
        ordinal, item = request.reference.resolve(request.session_id, items)
        content = _safe_content(item, self._redaction_values)
        tool_name = _safe_tool_name(item, self._redaction_values)
        encoded = content.encode("utf-8")
        total_bytes = len(encoded)
        if request.offset > total_bytes:
            raise SessionError("history item read offset is outside the item")
        try:
            encoded[: request.offset].decode("utf-8")
        except UnicodeDecodeError:
            raise SessionError("history item read offset is not a UTF-8 boundary") from None
        chunk = encoded[request.offset : request.offset + request.max_bytes].decode(
            "utf-8", "ignore"
        )
        consumed = len(chunk.encode("utf-8"))
        next_offset = request.offset + consumed if request.offset + consumed < total_bytes else None
        return SessionItemRead(
            request.reference,
            ordinal,
            item.role,
            _history_kind(item),
            tool_name,
            chunk,
            request.offset,
            total_bytes,
            next_offset,
        )

    def _summary(
        self,
        session_id: str,
        ordinal: int,
        item: SessionItem,
        *,
        snippet: str | None = None,
    ) -> SessionItemSummary:
        assert isinstance(item, Message)
        content = _safe_content(item, self._redaction_values)
        tool_name = _safe_tool_name(item, self._redaction_values)
        return SessionItemSummary(
            SessionItemReference.for_item(session_id, ordinal, item),
            ordinal,
            item.role,
            _history_kind(item),
            tool_name,
            _bounded_preview(content or tool_name or ""),
            snippet,
        )


__all__ = [
    "LoadSessionItemsRequest",
    "SessionItemQueryController",
    "SessionItemQueryService",
]
