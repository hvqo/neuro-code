"""Typed contracts for bounded, read-only Git workspace inspection.

Git inspection is deliberately separate from managed worktree lifecycle.  The
port exposes only an observed projection; it does not expose revisions,
arbitrary Git arguments, or a mutation capability.

定义有界只读 Git 工作区检查的类型化契约.

Git 检查刻意与受管 worktree 生命周期分离.端口只暴露观测投影,不暴露 revision、
任意 Git 参数或修改能力.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text

MAX_GIT_INSPECTION_STATUS_ENTRIES = 512
MAX_GIT_INSPECTION_PATH_BYTES = 4_096
MAX_GIT_INSPECTION_DIFF_SECTION_BYTES = 128 * 1024
MAX_GIT_INSPECTION_ERROR_BYTES = 1_000
MAX_GIT_INSPECTION_TIMEOUT_SECONDS = 120.0

_REPOSITORY_ID_PATTERN = re.compile(r"^[0-9a-f]{32,64}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


class GitInspectionView(StrEnum):
    """The bounded part of the workspace projection requested by a caller."""

    STATUS = "status"
    DIFF = "diff"
    ALL = "all"


class GitInspectionCompleteness(StrEnum):
    """Whether an observed section is complete and safe to describe as such."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"
    INCOMPLETE = "incomplete"


class GitChangeKind(StrEnum):
    """The strict record families accepted from porcelain v2."""

    ORDINARY = "ordinary"
    RENAMED = "renamed"
    COPIED = "copied"
    UNMERGED = "unmerged"
    UNTRACKED = "untracked"


class GitInspectionFailureKind(StrEnum):
    """Bounded failure facts at the read-only Git inspection boundary."""

    NOT_AVAILABLE = "not_available"
    NOT_REPOSITORY = "not_repository"
    PATH_UNSAFE = "path_unsafe"
    IDENTITY_MISMATCH = "identity_mismatch"
    PROTOCOL = "protocol"
    COMMAND_FAILED = "command_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    UNSAFE_CONFIGURATION = "unsafe_configuration"


class GitInspectionError(ToolError):
    """Expected, typed, redacted failure from the Git inspection boundary."""

    def __init__(self, message: str, *, kind: GitInspectionFailureKind) -> None:
        self.kind = kind
        safe_message = redact_sensitive_text(str(message).replace("\x00", ""))
        super().__init__(safe_message[:MAX_GIT_INSPECTION_ERROR_BYTES])


def _bounded_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field_name} must be non-empty text without NUL")
    if len(value.encode("utf-8", "surrogateescape")) > MAX_GIT_INSPECTION_PATH_BYTES:
        raise ValueError(f"{field_name} is too long")
    return value


@dataclass(frozen=True, slots=True)
class GitRepositoryState:
    """Safe repository identity and branch projection for one workspace."""

    root: Path
    repository_id: str
    head_sha: str
    branch: str | None
    detached: bool
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("Git repository root must be an absolute path")
        if not isinstance(self.repository_id, str) or not _REPOSITORY_ID_PATTERN.fullmatch(
            self.repository_id.casefold()
        ):
            raise ValueError("Git repository id must be hexadecimal")
        if (
            not isinstance(self.head_sha, str)
            or _SHA_PATTERN.fullmatch(self.head_sha.casefold()) is None
        ):
            raise ValueError("Git repository HEAD must be a hexadecimal SHA")
        object.__setattr__(self, "head_sha", self.head_sha.casefold())
        if self.branch is not None:
            _bounded_path(self.branch, field_name="Git branch")
        if not isinstance(self.detached, bool):
            raise TypeError("Git detached flag must be boolean")
        if self.detached and self.branch is not None:
            raise ValueError("detached Git state cannot expose a branch")
        if not self.detached and self.branch is None:
            raise ValueError("attached Git state must expose a branch")
        if self.upstream is not None:
            _bounded_path(self.upstream, field_name="Git upstream")
        for name in ("ahead", "behind"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"Git {name} count must be non-negative")


@dataclass(frozen=True, slots=True)
class GitStatusEntry:
    """One bounded status record; untracked entries never carry file content."""

    path: str
    kind: GitChangeKind
    index_status: str
    worktree_status: str
    original_path: str | None = None
    submodule: bool = False

    def __post_init__(self) -> None:
        _bounded_path(self.path, field_name="Git status path")
        if not isinstance(self.kind, GitChangeKind):
            raise TypeError("Git status kind must be canonical")
        for name in ("index_status", "worktree_status"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 1 or value == " ":
                raise ValueError(f"Git {name} must be one status character")
        if self.original_path is not None:
            _bounded_path(self.original_path, field_name="Git original status path")
        if not isinstance(self.submodule, bool):
            raise TypeError("Git submodule flag must be boolean")

    @property
    def xy(self) -> str:
        return f"{self.index_status}{self.worktree_status}"

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "kind": self.kind.value,
            "xy": self.xy,
            "submodule": self.submodule,
        }
        if self.original_path is not None:
            result["original_path"] = self.original_path
        return result


@dataclass(frozen=True, slots=True)
class GitStatusProjection:
    """Explicit status facts with conflicts counted separately.

    ``staged_count`` and ``unstaged_count`` exclude ``UNMERGED`` entries;
    conflicts are reported only through ``unmerged_count``.
    """

    entries: tuple[GitStatusEntry, ...]
    staged_count: int
    unstaged_count: int
    untracked_count: int
    unmerged_count: int
    completeness: GitInspectionCompleteness = GitInspectionCompleteness.COMPLETE

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        object.__setattr__(self, "entries", entries)
        if len(entries) > MAX_GIT_INSPECTION_STATUS_ENTRIES:
            raise ValueError("Git status entry bound exceeded")
        if not all(isinstance(entry, GitStatusEntry) for entry in entries):
            raise TypeError("Git status entries must be canonical")
        if not isinstance(self.completeness, GitInspectionCompleteness):
            raise TypeError("Git status completeness must be canonical")
        for name in ("staged_count", "unstaged_count", "untracked_count", "unmerged_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Git {name} must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "staged_count": self.staged_count,
            "unstaged_count": self.unstaged_count,
            "untracked_count": self.untracked_count,
            "unmerged_count": self.unmerged_count,
            "completeness": self.completeness.value,
        }


@dataclass(frozen=True, slots=True)
class GitDiffProjection:
    """A bounded textual diff plus binary metadata, never a reappliable patch promise.

    ``byte_count`` is the UTF-8/surrogateescape byte count of the returned
    redacted display text.  ``TRUNCATED`` also records when the bounded Git
    source exceeded the display-section limit, even if redaction shortened it.
    """

    text: str
    byte_count: int
    completeness: GitInspectionCompleteness
    binary_paths: tuple[str, ...] = ()
    redacted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("Git diff text must be text")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise ValueError("Git diff byte count must be non-negative")
        if not isinstance(self.completeness, GitInspectionCompleteness):
            raise TypeError("Git diff completeness must be canonical")
        paths = tuple(self.binary_paths)
        object.__setattr__(self, "binary_paths", paths)
        if not all(isinstance(path, str) and path for path in paths):
            raise ValueError("Git binary paths must be non-empty text")
        if not isinstance(self.redacted, bool):
            raise TypeError("Git diff redacted flag must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "byte_count": self.byte_count,
            "completeness": self.completeness.value,
            "binary_paths": list(self.binary_paths),
            "redacted": self.redacted,
        }


@dataclass(frozen=True, slots=True)
class GitInspectionResult:
    """One complete typed projection shared by the tool and CLI surfaces."""

    repository: GitRepositoryState
    status: GitStatusProjection
    view: GitInspectionView
    staged_diff: GitDiffProjection | None = None
    unstaged_diff: GitDiffProjection | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repository, GitRepositoryState):
            raise TypeError("Git repository state must be canonical")
        if not isinstance(self.status, GitStatusProjection):
            raise TypeError("Git status projection must be canonical")
        if not isinstance(self.view, GitInspectionView):
            raise TypeError("Git inspection view must be canonical")
        for name in ("staged_diff", "unstaged_diff"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, GitDiffProjection):
                raise TypeError(f"Git {name} must be canonical")

    @property
    def completeness(self) -> GitInspectionCompleteness:
        sections = [self.status.completeness]
        for diff in (self.staged_diff, self.unstaged_diff):
            if diff is not None:
                sections.append(diff.completeness)
        if GitInspectionCompleteness.INCOMPLETE in sections:
            return GitInspectionCompleteness.INCOMPLETE
        if GitInspectionCompleteness.TRUNCATED in sections:
            return GitInspectionCompleteness.TRUNCATED
        return GitInspectionCompleteness.COMPLETE

    @property
    def redacted(self) -> bool:
        return any(
            diff is not None and diff.redacted for diff in (self.staged_diff, self.unstaged_diff)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": {
                "root": str(self.repository.root),
                "repository_id": self.repository.repository_id,
                "head_sha": self.repository.head_sha,
                "branch": self.repository.branch,
                "detached": self.repository.detached,
                "upstream": self.repository.upstream,
                "ahead": self.repository.ahead,
                "behind": self.repository.behind,
            },
            "status": self.status.to_dict(),
            "staged_diff": self.staged_diff.to_dict() if self.staged_diff is not None else None,
            "unstaged_diff": (
                self.unstaged_diff.to_dict() if self.unstaged_diff is not None else None
            ),
            "view": self.view.value,
            "completeness": self.completeness.value,
            "redacted": self.redacted,
        }


class GitInspectionPort(Protocol):
    """Concrete local adapter boundary for one trusted workspace."""

    async def inspect(
        self,
        workspace: Path,
        view: GitInspectionView = GitInspectionView.ALL,
        /,
    ) -> GitInspectionResult: ...


class GitInspectionApplication(Protocol):
    """Inbound application capability used by the CLI and model tool."""

    async def inspect(
        self,
        workspace: Path,
        view: GitInspectionView = GitInspectionView.ALL,
        /,
    ) -> GitInspectionResult: ...


__all__ = [
    "MAX_GIT_INSPECTION_DIFF_SECTION_BYTES",
    "MAX_GIT_INSPECTION_ERROR_BYTES",
    "MAX_GIT_INSPECTION_PATH_BYTES",
    "MAX_GIT_INSPECTION_STATUS_ENTRIES",
    "MAX_GIT_INSPECTION_TIMEOUT_SECONDS",
    "GitChangeKind",
    "GitDiffProjection",
    "GitInspectionApplication",
    "GitInspectionCompleteness",
    "GitInspectionError",
    "GitInspectionFailureKind",
    "GitInspectionPort",
    "GitInspectionResult",
    "GitInspectionView",
    "GitRepositoryState",
    "GitStatusEntry",
    "GitStatusProjection",
]
