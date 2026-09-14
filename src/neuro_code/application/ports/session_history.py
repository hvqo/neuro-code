"""Contracts for bounded, read-only durable session-history rehydration.

持久化会话历史有界只读回填的规范端口契约.

The query implementation remains an application session owner.  These
contracts live at the port boundary so infrastructure tools can consume the
typed capability without depending on an application implementation module.
查询实现仍由 application session owner 持有.这些契约位于 port boundary,使基础设施 tool
可以消费类型化能力而不依赖 application implementation module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from neuro_code.domain.conversation.messages import Message, Role, SessionItem
from neuro_code.shared.errors import SessionError

MAX_SESSION_ITEM_LIST_LIMIT = 50
MAX_SESSION_ITEM_OFFSET = 1_000_000
MAX_SESSION_ITEM_SEARCH_QUERY_BYTES = 512
MAX_SESSION_ITEM_PREVIEW_BYTES = 256
MAX_SESSION_ITEM_READ_BYTES = 64 * 1024
MAX_SESSION_ITEM_READ_OFFSET = 1_000_000_000
MIN_SESSION_ITEM_READ_BYTES = 4

_REFERENCE_PREFIX = "shr1_"
_REFERENCE_VERSION = 1
_REFERENCE_SCOPE_BYTES = 16
_REFERENCE_DIGEST_BYTES = 32
_REFERENCE_CHECKSUM_BYTES = 16
_REFERENCE_BODY_BYTES = 1 + 8 + _REFERENCE_SCOPE_BYTES + _REFERENCE_DIGEST_BYTES
_REFERENCE_TOTAL_BYTES = _REFERENCE_BODY_BYTES + _REFERENCE_CHECKSUM_BYTES
_REFERENCE_MAX_BYTES = 256
_HISTORY_ITEM_KIND = "message"
_TOOL_RESULT_KIND = "tool_result"


class SessionItemReadKind(StrEnum):
    """Public kinds exposed by the model-facing history projection."""

    MESSAGE = _HISTORY_ITEM_KIND
    TOOL_RESULT = _TOOL_RESULT_KIND


def _require_session_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session_id must not be empty")
    if "\x00" in value:
        raise ValueError("session_id must not contain NUL characters")


def _require_page_value(name: str, value: int, *, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")


def _require_limit(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_SESSION_ITEM_LIST_LIMIT
    ):
        raise ValueError(f"limit must be between 1 and {MAX_SESSION_ITEM_LIST_LIMIT}")


def _require_page_request(offset: int, limit: int) -> None:
    _require_page_value("offset", offset, maximum=MAX_SESSION_ITEM_OFFSET)
    if offset > MAX_SESSION_ITEM_OFFSET - limit:
        raise ValueError("offset leaves no bounded history page")


def _canonical_item_bytes(item: SessionItem) -> bytes:
    if isinstance(item, Message):
        payload: dict[str, object] = {"kind": "message", **item.to_dict()}
    else:
        payload = {"kind": "preserved_context", **item.to_dict()}
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _item_digest(item: SessionItem) -> bytes:
    return hashlib.sha256(b"neuro-code/session-item/v1\0" + _canonical_item_bytes(item)).digest()


def _scope_digest(session_id: str) -> bytes:
    return hashlib.sha256(
        b"neuro-code/session-item-scope/v1\0" + session_id.encode("utf-8")
    ).digest()[:_REFERENCE_SCOPE_BYTES]


def _reference_token(session_id: str, ordinal: int, item: SessionItem) -> str:
    body = (
        bytes((_REFERENCE_VERSION,))
        + ordinal.to_bytes(8, "big", signed=False)
        + _scope_digest(session_id)
        + _item_digest(item)
    )
    checksum = hashlib.sha256(b"neuro-code/session-item-reference/v1\0" + body).digest()[
        :_REFERENCE_CHECKSUM_BYTES
    ]
    encoded = base64.urlsafe_b64encode(body + checksum).decode("ascii").rstrip("=")
    return f"{_REFERENCE_PREFIX}{encoded}"


def _decode_reference(token: str) -> tuple[int, bytes, bytes]:
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > _REFERENCE_MAX_BYTES:
        raise SessionError("session item reference is invalid")
    if not token.startswith(_REFERENCE_PREFIX):
        raise SessionError("session item reference is invalid")
    encoded = token[len(_REFERENCE_PREFIX) :]
    if not encoded or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in encoded
    ):
        raise SessionError("session item reference is invalid")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise SessionError("session item reference is invalid") from None
    if len(raw) != _REFERENCE_TOTAL_BYTES or raw[0] != _REFERENCE_VERSION:
        raise SessionError("session item reference is invalid")
    body = raw[:_REFERENCE_BODY_BYTES]
    checksum = raw[_REFERENCE_BODY_BYTES:]
    expected_checksum = hashlib.sha256(b"neuro-code/session-item-reference/v1\0" + body).digest()[
        :_REFERENCE_CHECKSUM_BYTES
    ]
    if not hmac.compare_digest(checksum, expected_checksum):
        raise SessionError("session item reference is invalid")
    ordinal = int.from_bytes(raw[1:9], "big", signed=False)
    if ordinal <= 0:
        raise SessionError("session item reference is invalid")
    return (
        ordinal,
        raw[9 : 9 + _REFERENCE_SCOPE_BYTES],
        raw[9 + _REFERENCE_SCOPE_BYTES : _REFERENCE_BODY_BYTES],
    )


def _is_public_history_item(item: SessionItem) -> bool:
    return (
        isinstance(item, Message)
        and item.synthetic_reason is None
        and item.role in {Role.USER, Role.ASSISTANT, Role.TOOL}
    )


@dataclass(frozen=True, slots=True)
class SessionItemReference:
    """Opaque, session-bound address of one durable history item."""

    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("session item reference token must not be empty")
        if "\x00" in self.token or len(self.token.encode("utf-8")) > _REFERENCE_MAX_BYTES:
            raise ValueError("session item reference token is invalid")

    @classmethod
    def for_item(cls, session_id: str, ordinal: int, item: SessionItem) -> SessionItemReference:
        _require_session_id(session_id)
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
            raise ValueError("session item ordinal must be positive")
        if not _is_public_history_item(item):
            raise ValueError("session item is not publicly readable")
        return cls(_reference_token(session_id, ordinal, item))

    def resolve(self, session_id: str, items: Sequence[SessionItem]) -> tuple[int, Message]:
        """Resolve and validate one reference against one session's items."""

        _require_session_id(session_id)
        ordinal, expected_scope, expected_digest = _decode_reference(self.token)
        if not hmac.compare_digest(expected_scope, _scope_digest(session_id)):
            raise SessionError("session item reference is invalid for this session")
        if ordinal > len(items):
            raise SessionError("session item reference is stale")
        item = items[ordinal - 1]
        if not hmac.compare_digest(expected_digest, _item_digest(item)):
            raise SessionError("session item reference is stale")
        if not _is_public_history_item(item):
            raise SessionError("session item reference is not publicly readable")
        assert isinstance(item, Message)
        return ordinal, item


@dataclass(frozen=True, slots=True)
class ListSessionItemsRequest:
    """Bounded newest-first page of one session's public durable items."""

    session_id: str
    limit: int = 20
    offset: int = 0

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)
        _require_limit(self.limit)
        _require_page_request(self.offset, self.limit)


@dataclass(frozen=True, slots=True)
class SearchSessionItemsRequest:
    """Bounded search over one session's safe textual durable history."""

    session_id: str
    query: str
    limit: int = 20
    offset: int = 0

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("query must not be empty")
        if (
            "\x00" in self.query
            or len(self.query.encode("utf-8")) > MAX_SESSION_ITEM_SEARCH_QUERY_BYTES
        ):
            raise ValueError("query exceeds the bounded history search limit")
        _require_limit(self.limit)
        _require_page_request(self.offset, self.limit)


@dataclass(frozen=True, slots=True)
class ReadSessionItemRequest:
    """Bounded read of one exact public item projection."""

    session_id: str
    reference: SessionItemReference
    offset: int = 0
    max_bytes: int = MAX_SESSION_ITEM_READ_BYTES

    def __post_init__(self) -> None:
        _require_session_id(self.session_id)
        if not isinstance(self.reference, SessionItemReference):
            raise ValueError("reference must be a canonical session item reference")
        _require_page_value("offset", self.offset, maximum=MAX_SESSION_ITEM_READ_OFFSET)
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or not MIN_SESSION_ITEM_READ_BYTES <= self.max_bytes <= MAX_SESSION_ITEM_READ_BYTES
        ):
            raise ValueError(
                "max_bytes must be between "
                f"{MIN_SESSION_ITEM_READ_BYTES} and {MAX_SESSION_ITEM_READ_BYTES}"
            )


@dataclass(frozen=True, slots=True)
class SessionItemSummary:
    """Compact safe metadata returned by list and search."""

    reference: SessionItemReference
    ordinal: int
    role: Role
    kind: SessionItemReadKind
    tool_name: str | None
    preview: str
    snippet: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, SessionItemReference):
            raise ValueError("history summary reference must be canonical")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal <= 0:
            raise ValueError("history summary ordinal must be positive")
        if not isinstance(self.role, Role) or not isinstance(self.kind, SessionItemReadKind):
            raise ValueError("history summary role and kind must be canonical")
        if self.role is Role.TOOL and self.kind is not SessionItemReadKind.TOOL_RESULT:
            raise ValueError("tool history summary kind is invalid")
        if self.role is not Role.TOOL and self.kind is not SessionItemReadKind.MESSAGE:
            raise ValueError("message history summary kind is invalid")
        if self.tool_name is not None and not isinstance(self.tool_name, str):
            raise ValueError("history summary tool name must be text")
        if not isinstance(self.preview, str) or not isinstance(self.snippet, str | None):
            raise ValueError("history summary text fields must be canonical")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "reference": self.reference.token,
            "ordinal": self.ordinal,
            "role": self.role.value,
            "kind": self.kind.value,
            "tool_name": self.tool_name,
            "preview": self.preview,
        }
        if self.snippet is not None:
            result["snippet"] = self.snippet
        return result


@dataclass(frozen=True, slots=True)
class SessionItemPage:
    """Bounded page returned by list or search."""

    items: tuple[SessionItemSummary, ...]
    next_offset: int | None
    total_estimate: int

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if not all(isinstance(item, SessionItemSummary) for item in items):
            raise ValueError("history page items must be canonical")
        object.__setattr__(self, "items", items)
        if self.next_offset is not None:
            _require_page_value("next_offset", self.next_offset, maximum=MAX_SESSION_ITEM_OFFSET)
        if (
            isinstance(self.total_estimate, bool)
            or not isinstance(self.total_estimate, int)
            or self.total_estimate < len(items)
        ):
            raise ValueError("history page total must cover returned items")

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "next_offset": self.next_offset,
            "total_estimate": self.total_estimate,
        }


@dataclass(frozen=True, slots=True)
class SessionItemRead:
    """One bounded safe content chunk from one durable item."""

    reference: SessionItemReference
    ordinal: int
    role: Role
    kind: SessionItemReadKind
    tool_name: str | None
    content: str
    offset: int
    total_bytes: int
    next_offset: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, SessionItemReference):
            raise ValueError("history read reference must be canonical")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal <= 0:
            raise ValueError("history read ordinal must be positive")
        if not isinstance(self.role, Role) or not isinstance(self.kind, SessionItemReadKind):
            raise ValueError("history read role and kind must be canonical")
        if not isinstance(self.content, str):
            raise ValueError("history read content must be text")
        _require_page_value("offset", self.offset, maximum=MAX_SESSION_ITEM_READ_OFFSET)
        if (
            isinstance(self.total_bytes, bool)
            or not isinstance(self.total_bytes, int)
            or self.total_bytes < 0
        ):
            raise ValueError("history read total must be non-negative")
        if self.next_offset is not None:
            _require_page_value(
                "next_offset",
                self.next_offset,
                maximum=MAX_SESSION_ITEM_READ_OFFSET,
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference.token,
            "ordinal": self.ordinal,
            "role": self.role.value,
            "kind": self.kind.value,
            "tool_name": self.tool_name,
            "content": self.content,
            "offset": self.offset,
            "total_bytes": self.total_bytes,
            "next_offset": self.next_offset,
            "truncated": self.next_offset is not None,
        }


class SessionHistoryQueryController(Protocol):
    """Read-only application capability consumed by the history tool."""

    async def list_session_items(self, request: ListSessionItemsRequest) -> SessionItemPage: ...

    async def search_session_items(self, request: SearchSessionItemsRequest) -> SessionItemPage: ...

    async def read_session_item(self, request: ReadSessionItemRequest) -> SessionItemRead: ...


__all__ = [
    "MAX_SESSION_ITEM_LIST_LIMIT",
    "MAX_SESSION_ITEM_OFFSET",
    "MAX_SESSION_ITEM_PREVIEW_BYTES",
    "MAX_SESSION_ITEM_READ_BYTES",
    "MAX_SESSION_ITEM_READ_OFFSET",
    "MAX_SESSION_ITEM_SEARCH_QUERY_BYTES",
    "MIN_SESSION_ITEM_READ_BYTES",
    "ListSessionItemsRequest",
    "ReadSessionItemRequest",
    "SearchSessionItemsRequest",
    "SessionHistoryQueryController",
    "SessionItemPage",
    "SessionItemRead",
    "SessionItemReadKind",
    "SessionItemReference",
    "SessionItemSummary",
]
