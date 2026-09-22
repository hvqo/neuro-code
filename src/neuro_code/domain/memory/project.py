"""Validated values for local, project-owned memory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_PROJECT_MEMORY_NAME_CHARS = 120
MAX_PROJECT_MEMORY_DESCRIPTION_CHARS = 640
MAX_PROJECT_MEMORY_CONTENT_BYTES = 16_384
MAX_PROJECT_MEMORY_ID_CHARS = 40
MAX_PROJECT_MEMORY_SESSION_ID_CHARS = 128
_MEMORY_ID = re.compile(r"mem-[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ProjectMemoryType(StrEnum):
    PROJECT = "project"
    FEEDBACK = "feedback"
    USER = "user"
    REFERENCE = "reference"


def _require_clean_text(name: str, value: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > max_chars:
        raise ValueError(f"{name} exceeds its character limit")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in normalized):
        raise ValueError(f"{name} contains a control character")
    return normalized


@dataclass(frozen=True, slots=True)
class ProjectMemoryMetadata:
    memory_id: str
    name: str
    description: str
    type: ProjectMemoryType
    origin_session_id: str
    created_at: datetime
    updated_at: datetime
    updated_session_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not _MEMORY_ID.fullmatch(self.memory_id):
            raise ValueError("memory_id is invalid")
        object.__setattr__(
            self,
            "name",
            _require_clean_text("memory name", self.name, max_chars=MAX_PROJECT_MEMORY_NAME_CHARS),
        )
        object.__setattr__(
            self,
            "description",
            _require_clean_text(
                "memory description",
                self.description,
                max_chars=MAX_PROJECT_MEMORY_DESCRIPTION_CHARS,
            ),
        )
        if not isinstance(self.type, ProjectMemoryType):
            raise TypeError("memory type must be a ProjectMemoryType")
        for field_name in ("origin_session_id", "updated_session_id"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or len(value.encode("utf-8")) > MAX_PROJECT_MEMORY_SESSION_ID_CHARS
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError(f"{field_name} is invalid")
        for field_name in ("created_at", "updated_at"):
            value = getattr(self, field_name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class ProjectMemory:
    metadata: ProjectMemoryMetadata
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ProjectMemoryMetadata):
            raise TypeError("metadata must be a ProjectMemoryMetadata")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("memory content must be non-empty text")
        if len(self.content.encode("utf-8")) > MAX_PROJECT_MEMORY_CONTENT_BYTES:
            raise ValueError("memory content exceeds its byte limit")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in self.content):
            raise ValueError("memory content contains a control character")


@dataclass(frozen=True, slots=True)
class ProjectMemoryCursor:
    session_id: str
    item_count: int
    prefix_sha256: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_id, str)
            or not self.session_id.strip()
            or len(self.session_id.encode("utf-8")) > MAX_PROJECT_MEMORY_SESSION_ID_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in self.session_id)
        ):
            raise ValueError("cursor session_id is invalid")
        if (
            isinstance(self.item_count, bool)
            or not isinstance(self.item_count, int)
            or self.item_count < 0
        ):
            raise ValueError("cursor item_count must be non-negative")
        if not isinstance(self.prefix_sha256, str) or not _SHA256.fullmatch(self.prefix_sha256):
            raise ValueError("cursor prefix_sha256 is invalid")
        if not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None:
            raise ValueError("cursor updated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProjectMemorySnapshot:
    project_id: str
    memories: tuple[ProjectMemoryMetadata, ...] = ()
    cursors: tuple[ProjectMemoryCursor, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id is required")
        object.__setattr__(self, "memories", tuple(self.memories))
        object.__setattr__(self, "cursors", tuple(self.cursors))
        if not all(isinstance(memory, ProjectMemoryMetadata) for memory in self.memories):
            raise TypeError("memories must contain ProjectMemoryMetadata values")
        if not all(isinstance(cursor, ProjectMemoryCursor) for cursor in self.cursors):
            raise TypeError("cursors must contain ProjectMemoryCursor values")
        if len({memory.memory_id for memory in self.memories}) != len(self.memories):
            raise ValueError("memory identities must be unique in one project")
        if len({cursor.session_id for cursor in self.cursors}) != len(self.cursors):
            raise ValueError("extraction cursors must be unique per session and project")


__all__ = [
    "MAX_PROJECT_MEMORY_CONTENT_BYTES",
    "MAX_PROJECT_MEMORY_DESCRIPTION_CHARS",
    "MAX_PROJECT_MEMORY_ID_CHARS",
    "MAX_PROJECT_MEMORY_NAME_CHARS",
    "ProjectMemory",
    "ProjectMemoryCursor",
    "ProjectMemoryMetadata",
    "ProjectMemorySnapshot",
    "ProjectMemoryType",
]
