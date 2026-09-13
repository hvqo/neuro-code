"""Concrete bounded read-only Git inspection adapter.

This module owns only Git protocol parsing and projection.  Process creation
is delegated to the existing :class:`LocalGitWorktreeAdapter`; no second Git
runner or model-controlled argv path is introduced.

具体的有界只读 Git 检查适配器.

本模块只拥有 Git 协议解析与投影.进程创建委托给现有
``LocalGitWorktreeAdapter``,不新增第二套 Git runner,也不引入模型可控 argv 或路径.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from neuro_code.application.ports.git_inspection import (
    MAX_GIT_INSPECTION_DIFF_SECTION_BYTES,
    MAX_GIT_INSPECTION_PATH_BYTES,
    MAX_GIT_INSPECTION_STATUS_ENTRIES,
    MAX_GIT_INSPECTION_TIMEOUT_SECONDS,
    GitChangeKind,
    GitDiffProjection,
    GitInspectionCompleteness,
    GitInspectionError,
    GitInspectionFailureKind,
    GitInspectionPort,
    GitInspectionResult,
    GitInspectionView,
    GitRepositoryState,
    GitStatusEntry,
    GitStatusProjection,
)
from neuro_code.application.ports.worktree import WorktreeError, WorktreeFailureKind
from neuro_code.infrastructure.git.worktree import (
    LocalGitWorktreeAdapter,
)
from neuro_code.shared.redaction import redact_sensitive_text

_STATUS_ARGS = (
    "status",
    "--porcelain=v2",
    "--branch",
    "-z",
    "--untracked-files=all",
    "--ignore-submodules=none",
)
_STAGED_DIFF_ARGS = (
    "diff",
    "--cached",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
    "--",
)
_UNSTAGED_DIFF_ARGS = (
    "diff",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
    "--",
)
_STATUS_CHARS = frozenset(".MADRCU?!T").union({" "})
_BRANCH_AB_PATTERN = re.compile(r"^\+(\d+) -(\d+)$")
_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


@dataclass(frozen=True, slots=True)
class _GitStatusMetadata:
    head_sha: str
    branch: str | None
    detached: bool
    upstream: str | None
    ahead: int | None
    behind: int | None


def _protocol_error(message: str) -> WorktreeError:
    return WorktreeError(message, kind=WorktreeFailureKind.PROTOCOL)


def _bounded_repo_path(raw: bytes, *, field_name: str) -> str:
    if not raw or len(raw) > MAX_GIT_INSPECTION_PATH_BYTES or b"\x00" in raw:
        raise _protocol_error(f"Git {field_name} is invalid or exceeds its bound")
    value = os.fsdecode(raw)
    if not value or "\x00" in value:
        raise _protocol_error(f"Git {field_name} is invalid")
    try:
        pure = PurePosixPath(value)
    except (TypeError, ValueError) as error:
        raise _protocol_error(f"Git {field_name} is invalid") from error
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise _protocol_error(f"Git {field_name} is not a repository-relative path")
    return value


def _bounded_branch_name(raw: bytes) -> str:
    if (
        not raw
        or len(raw) > MAX_GIT_INSPECTION_PATH_BYTES
        or b"\x00" in raw
        or any(value < 32 or value == 127 for value in raw)
    ):
        raise _protocol_error("Git branch name is malformed")
    value = os.fsdecode(raw)
    if not value:
        raise _protocol_error("Git branch name is empty")
    return value


def _status_pair(raw: bytes) -> tuple[str, str]:
    if len(raw) != 2 or any(chr(value) not in _STATUS_CHARS for value in raw):
        raise _protocol_error("Git status record has an invalid XY field")
    return chr(raw[0]), chr(raw[1])


def _submodule_flag(raw: bytes) -> bool:
    if len(raw) != 4 or raw[:1] not in {b"N", b"S"}:
        raise _protocol_error("Git status record has an invalid submodule field")
    return raw[:1] == b"S"


def _entry(
    *,
    raw_xy: bytes,
    raw_submodule: bytes,
    raw_path: bytes,
    kind: GitChangeKind,
    original_path: bytes | None = None,
) -> GitStatusEntry:
    index_status, worktree_status = _status_pair(raw_xy)
    path = _bounded_repo_path(raw_path, field_name="status path")
    original = (
        None
        if original_path is None
        else _bounded_repo_path(original_path, field_name="original status path")
    )
    if kind is GitChangeKind.UNTRACKED:
        index_status, worktree_status = "?", "?"
    return GitStatusEntry(
        path=path,
        kind=kind,
        index_status=index_status,
        worktree_status=worktree_status,
        original_path=original,
        submodule=_submodule_flag(raw_submodule) if kind is not GitChangeKind.UNTRACKED else False,
    )


def _parse_git_status_porcelain(output: bytes) -> tuple[_GitStatusMetadata, GitStatusProjection]:
    """Parse the strict NUL-delimited porcelain-v2 status protocol."""

    branch_oid: str | None = None
    branch_head: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    entries: list[GitStatusEntry] = []
    fields = output.split(b"\0")
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if record.startswith(b"# "):
            header = record[2:]
            name, separator, value = header.partition(b" ")
            if not separator or not value:
                raise _protocol_error("Git status branch header is malformed")
            rendered = os.fsdecode(value)
            if name == b"branch.oid":
                if branch_oid is not None:
                    raise _protocol_error("Git status branch oid is duplicated")
                if _HEX_PATTERN.fullmatch(rendered) is None:
                    raise _protocol_error("Git status branch oid is malformed")
                branch_oid = rendered.casefold()
            elif name == b"branch.head":
                if branch_head is not None or "\x00" in rendered:
                    raise _protocol_error("Git status branch head is malformed")
                branch_head = _bounded_branch_name(value)
            elif name == b"branch.upstream":
                if upstream is not None:
                    raise _protocol_error("Git status upstream is duplicated")
                upstream = _bounded_repo_path(value, field_name="upstream")
            elif name == b"branch.ab":
                if ahead is not None or behind is not None:
                    raise _protocol_error("Git status ahead/behind is duplicated")
                match = _BRANCH_AB_PATTERN.fullmatch(rendered)
                if match is None:
                    raise _protocol_error("Git status ahead/behind is malformed")
                ahead, behind = (int(group) for group in match.groups())
            else:
                raise _protocol_error("Git status contains an unknown branch header")
            continue
        kind_code = record[:1]
        if kind_code == b"1":
            parts = record.split(b" ", 8)
            if len(parts) != 9 or parts[0] != b"1":
                raise _protocol_error("Git ordinary status record is malformed")
            entries.append(
                _entry(
                    raw_xy=parts[1],
                    raw_submodule=parts[2],
                    raw_path=parts[8],
                    kind=GitChangeKind.ORDINARY,
                )
            )
        elif kind_code == b"2":
            parts = record.split(b" ", 9)
            if len(parts) != 10 or parts[0] != b"2" or index >= len(fields) or not fields[index]:
                raise _protocol_error("Git rename/copy status record is malformed")
            original_path = fields[index]
            index += 1
            score = parts[8]
            if score[:1] not in {b"R", b"C"} or score[1:] == b"":
                raise _protocol_error("Git rename/copy status score is malformed")
            entries.append(
                _entry(
                    raw_xy=parts[1],
                    raw_submodule=parts[2],
                    raw_path=parts[9],
                    original_path=original_path,
                    kind=(GitChangeKind.RENAMED if score[:1] == b"R" else GitChangeKind.COPIED),
                )
            )
        elif kind_code == b"u":
            parts = record.split(b" ", 10)
            if len(parts) != 11 or parts[0] != b"u":
                raise _protocol_error("Git unmerged status record is malformed")
            entries.append(
                _entry(
                    raw_xy=parts[1],
                    raw_submodule=parts[2],
                    raw_path=parts[10],
                    kind=GitChangeKind.UNMERGED,
                )
            )
        elif kind_code == b"?":
            if not record.startswith(b"? "):
                raise _protocol_error("Git untracked status record is malformed")
            entries.append(
                _entry(
                    raw_xy=b"??",
                    raw_submodule=b"N...",
                    raw_path=record[2:],
                    kind=GitChangeKind.UNTRACKED,
                )
            )
        else:
            raise _protocol_error("Git status contains an unknown record")
        if len(entries) > MAX_GIT_INSPECTION_STATUS_ENTRIES:
            raise WorktreeError(
                "Git status exceeded the bounded entry limit",
                kind=WorktreeFailureKind.OUTPUT_LIMIT,
            )

    if branch_oid is None or branch_head is None:
        raise _protocol_error("Git status did not provide complete branch identity")
    detached = branch_head == "(detached)"
    branch = None if detached else branch_head
    if not detached and not branch:
        raise _protocol_error("Git status branch name is empty")
    staged_count = sum(entry.index_status != "?" and entry.index_status != "." for entry in entries)
    unstaged_count = sum(
        entry.kind is not GitChangeKind.UNTRACKED and entry.worktree_status not in {".", "?"}
        for entry in entries
    )
    untracked_count = sum(entry.kind is GitChangeKind.UNTRACKED for entry in entries)
    unmerged_count = sum(entry.kind is GitChangeKind.UNMERGED for entry in entries)
    return _GitStatusMetadata(
        head_sha=branch_oid,
        branch=branch,
        detached=detached,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
    ), GitStatusProjection(
        entries=tuple(entries),
        staged_count=staged_count,
        unstaged_count=unstaged_count,
        untracked_count=untracked_count,
        unmerged_count=unmerged_count,
    )


def parse_git_status_porcelain(output: bytes) -> GitStatusProjection:
    """Return the status projection after strict porcelain-v2 validation."""

    _, status = _parse_git_status_porcelain(output)
    return status


def _binary_paths(text: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in text.splitlines():
        if not line.startswith("Binary files ") or " and " not in line:
            continue
        sides = line.removeprefix("Binary files ").removesuffix(" differ").rsplit(" and ", 1)
        if len(sides) != 2:
            continue
        for candidate in sides:
            if candidate == "/dev/null":
                continue
            if candidate[:2] in {"a/", "b/"}:
                candidate = candidate[2:]
            if not candidate or candidate in paths:
                continue
            try:
                _bounded_repo_path(os.fsencode(candidate), field_name="binary diff path")
            except WorktreeError:
                continue
            paths.append(candidate)
    return tuple(paths)


def _without_binary_patch(text: str) -> str:
    """Remove defensive binary-patch payloads from an untrusted projection."""

    rendered: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line.startswith("GIT binary patch"):
            skipping = True
            continue
        if skipping and line.startswith("diff --git "):
            skipping = False
        if not skipping:
            rendered.append(line)
    return "".join(rendered)


def project_git_diff(raw: bytes, *, redaction_values: tuple[str, ...]) -> GitDiffProjection:
    """Project text and binary metadata without ever requesting binary patches."""

    clipped = raw[:MAX_GIT_INSPECTION_DIFF_SECTION_BYTES]
    text = os.fsdecode(clipped)
    safe_text = redact_sensitive_text(text, explicit_values=redaction_values)
    safe_text = _without_binary_patch(safe_text)
    completeness = (
        GitInspectionCompleteness.TRUNCATED
        if len(raw) > MAX_GIT_INSPECTION_DIFF_SECTION_BYTES
        else GitInspectionCompleteness.COMPLETE
    )
    return GitDiffProjection(
        text=safe_text,
        byte_count=len(clipped),
        completeness=completeness,
        binary_paths=_binary_paths(safe_text),
        redacted=safe_text != text,
    )


def _map_failure(error: WorktreeError) -> GitInspectionError:
    mapping = {
        WorktreeFailureKind.NOT_AVAILABLE: GitInspectionFailureKind.NOT_AVAILABLE,
        WorktreeFailureKind.NOT_REPOSITORY: GitInspectionFailureKind.NOT_REPOSITORY,
        WorktreeFailureKind.REPOSITORY_MISSING: GitInspectionFailureKind.NOT_REPOSITORY,
        WorktreeFailureKind.IDENTITY_MISMATCH: GitInspectionFailureKind.IDENTITY_MISMATCH,
        WorktreeFailureKind.PROTOCOL: GitInspectionFailureKind.PROTOCOL,
        WorktreeFailureKind.TIMEOUT: GitInspectionFailureKind.TIMEOUT,
        WorktreeFailureKind.CANCELLED: GitInspectionFailureKind.CANCELLED,
        WorktreeFailureKind.OUTPUT_LIMIT: GitInspectionFailureKind.OUTPUT_LIMIT,
        WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION: GitInspectionFailureKind.UNSAFE_CONFIGURATION,
        WorktreeFailureKind.EXTERNAL_FILTER_UNSUPPORTED: GitInspectionFailureKind.UNSAFE_CONFIGURATION,
    }
    return GitInspectionError(
        str(error), kind=mapping.get(error.kind, GitInspectionFailureKind.COMMAND_FAILED)
    )


class LocalGitInspectionAdapter(GitInspectionPort):
    """Read-only Git projection backed by the existing hardened Git adapter."""

    def __init__(
        self,
        git: LocalGitWorktreeAdapter,
        *,
        redaction_values: tuple[str, ...] = (),
    ) -> None:
        self._git = git
        self._redaction_values = tuple(redaction_values)

    async def inspect(
        self,
        workspace: Path,
        view: GitInspectionView = GitInspectionView.ALL,
        /,
    ) -> GitInspectionResult:
        if not isinstance(view, GitInspectionView):
            raise GitInspectionError(
                "Git inspection view is invalid",
                kind=GitInspectionFailureKind.PROTOCOL,
            )
        try:
            await self._git.git_version(read_only=True)
            identity = await self._git.repository_identity(workspace, read_only=True)
            status_output, _, _ = await self._git.run_read_only_git(
                identity.source_worktree,
                _STATUS_ARGS,
                timeout_seconds=MAX_GIT_INSPECTION_TIMEOUT_SECONDS,
                failure_kind=WorktreeFailureKind.NOT_REPOSITORY,
            )
            status_identity, status = _parse_git_status_porcelain(status_output)
            if status_identity.head_sha != identity.head_sha:
                raise WorktreeError(
                    "Git HEAD changed during read-only inspection",
                    kind=WorktreeFailureKind.IDENTITY_MISMATCH,
                )
            repository = GitRepositoryState(
                root=identity.source_worktree,
                repository_id=identity.repository_id,
                head_sha=identity.head_sha,
                branch=status_identity.branch,
                detached=status_identity.detached,
                upstream=status_identity.upstream,
                ahead=status_identity.ahead,
                behind=status_identity.behind,
            )
            staged_diff: GitDiffProjection | None = None
            unstaged_diff: GitDiffProjection | None = None
            if view in {GitInspectionView.DIFF, GitInspectionView.ALL}:
                staged_output, _, _ = await self._git.run_read_only_git(
                    identity.source_worktree,
                    _STAGED_DIFF_ARGS,
                    timeout_seconds=MAX_GIT_INSPECTION_TIMEOUT_SECONDS,
                    failure_kind=WorktreeFailureKind.NOT_REPOSITORY,
                )
                unstaged_output, _, _ = await self._git.run_read_only_git(
                    identity.source_worktree,
                    _UNSTAGED_DIFF_ARGS,
                    timeout_seconds=MAX_GIT_INSPECTION_TIMEOUT_SECONDS,
                    failure_kind=WorktreeFailureKind.NOT_REPOSITORY,
                )
                staged_diff = project_git_diff(
                    staged_output,
                    redaction_values=self._redaction_values,
                )
                unstaged_diff = project_git_diff(
                    unstaged_output,
                    redaction_values=self._redaction_values,
                )
            return GitInspectionResult(
                repository=repository,
                status=status,
                view=view,
                staged_diff=staged_diff,
                unstaged_diff=unstaged_diff,
            )
        except GitInspectionError:
            raise
        except WorktreeError as error:
            raise _map_failure(error) from error


__all__ = [
    "LocalGitInspectionAdapter",
    "parse_git_status_porcelain",
    "project_git_diff",
]
