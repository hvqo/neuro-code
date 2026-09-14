from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from neuro_code.application.git_inspection import GitInspectionService
from neuro_code.application.ports.git_inspection import (
    MAX_GIT_INSPECTION_DIFF_SECTION_BYTES,
    GitInspectionApplication,
    GitInspectionError,
    GitInspectionFailureKind,
    GitInspectionView,
)
from neuro_code.application.ports.sandbox import LocalWorkspaceAccessMode, SandboxedProcessRequest
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.worktree import WorktreeError, WorktreeFailureKind
from neuro_code.domain.tools import ToolResult
from neuro_code.domain.worktree import WorktreeRepositoryIdentity
from neuro_code.infrastructure.git.inspection import (
    LocalGitInspectionAdapter,
    parse_git_status_porcelain,
    project_git_diff,
)
from neuro_code.infrastructure.git.worktree import LocalGitWorktreeAdapter
from neuro_code.infrastructure.tools.git_inspection import GitInspectTool
from neuro_code.infrastructure.tools.registry import default_tool_registry
from neuro_code.interfaces.cli.inspection import run_inspect_command
from neuro_code.interfaces.cli.parser import build_parser
from neuro_code.shared.errors import ConfigurationError


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _new_repository(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "neuro-code-tests@example.invalid")
    _git(repository, "config", "user.name", "Neuro Code Tests")
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "initial")
    return repository


class _FakeOutput:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self, _size: int = -1, /) -> bytes:
        payload, self._payload = self._payload, b""
        return payload


class _FakeProcess:
    def __init__(self, returncode: int, *, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.stdout = _FakeOutput(stdout)
        self.stderr = _FakeOutput(stderr)
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode

    async def terminate(self, *, grace_seconds: float | None = None) -> None:
        del grace_seconds

    async def write_stdin(self, data: bytes) -> None:
        del data

    async def close_stdin(self) -> None:
        return None


class _FakeGitSandbox:
    def __init__(self, config_outputs: dict[str, bytes]) -> None:
        self.config_outputs = config_outputs
        self.requests: list[SandboxedProcessRequest] = []

    async def spawn(self, request: SandboxedProcessRequest) -> _FakeProcess:
        self.requests.append(request)
        probe_prefix = ("config", "--null", "--includes", "--get-all")
        if request.arguments[-5:-1] == probe_prefix:
            key = request.arguments[-1]
            if key in self.config_outputs:
                return _FakeProcess(returncode=0, stdout=self.config_outputs[key])
            return _FakeProcess(returncode=1)
        return _FakeProcess(returncode=0)


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


class GitInspectionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_repository_reports_identity_and_complete_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            result = await LocalGitInspectionAdapter(LocalGitWorktreeAdapter()).inspect(repository)

            self.assertEqual(result.repository.root, repository.resolve())
            self.assertEqual(result.repository.head_sha, _git(repository, "rev-parse", "HEAD"))
            self.assertIsNotNone(result.repository.branch)
            self.assertFalse(result.repository.detached)
            self.assertEqual(result.status.entries, ())
            self.assertEqual(result.completeness.value, "complete")
            self.assertTrue(result.staged_diff is not None)
            self.assertTrue(result.unstaged_diff is not None)
            assert result.staged_diff is not None
            assert result.unstaged_diff is not None
            self.assertEqual(result.staged_diff.text, "")
            self.assertEqual(result.unstaged_diff.text, "")

    @unittest.skipUnless(os.name == "nt", "Windows Git line-ending semantics required")
    async def test_effective_autocrlf_keeps_crlf_clean_and_detects_content_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            (home / ".gitconfig").write_text(
                "[core]\n\tautocrlf = true\n\teol = crlf\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"HOME": str(home), "USERPROFILE": str(home)},
            ):
                repository = _new_repository(root)
                target = repository / "tracked.txt"
                target.write_bytes(b"committed\r\n")
                adapter = LocalGitInspectionAdapter(LocalGitWorktreeAdapter())

                clean = await adapter.inspect(repository, GitInspectionView.STATUS)
                self.assertEqual(clean.status.entries, ())

                target.write_bytes(b"changed\r\n")
                changed = await adapter.inspect(repository, GitInspectionView.STATUS)
                self.assertEqual(
                    [(entry.path, entry.xy) for entry in changed.status.entries],
                    [("tracked.txt", ".M")],
                )

    async def test_staged_and_unstaged_diffs_remain_distinct_and_untracked_content_is_hidden(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            target = repository / "tracked.txt"
            target.write_text("staged\n", encoding="utf-8")
            _git(repository, "add", "tracked.txt")
            target.write_text("unstaged\n", encoding="utf-8")
            (repository / "untracked.txt").write_text(
                "UNTRACKED_SECRET_SENTINEL\n", encoding="utf-8"
            )

            result = await LocalGitInspectionAdapter(LocalGitWorktreeAdapter()).inspect(repository)

            self.assertEqual(result.status.staged_count, 1)
            self.assertEqual(result.status.unstaged_count, 1)
            self.assertEqual(result.status.untracked_count, 1)
            self.assertEqual(
                [(entry.path, entry.xy) for entry in result.status.entries],
                [("tracked.txt", "MM"), ("untracked.txt", "??")],
            )
            assert result.staged_diff is not None
            assert result.unstaged_diff is not None
            self.assertIn("+staged", result.staged_diff.text)
            self.assertIn("+unstaged", result.unstaged_diff.text)
            self.assertNotIn("UNTRACKED_SECRET_SENTINEL", result.staged_diff.text)
            self.assertNotIn("UNTRACKED_SECRET_SENTINEL", result.unstaged_diff.text)

    async def test_detached_and_binary_repository_state_is_typed_without_binary_patch_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            binary = repository / "payload.bin"
            binary.write_bytes(b"\x00\x01\x02\x00binary")
            _git(repository, "add", "payload.bin")
            _git(repository, "checkout", "--detach", "-q")

            result = await LocalGitInspectionAdapter(LocalGitWorktreeAdapter()).inspect(repository)

            self.assertTrue(result.repository.detached)
            self.assertIsNone(result.repository.branch)
            assert result.staged_diff is not None
            self.assertIn("payload.bin", result.staged_diff.binary_paths)
            self.assertNotIn("GIT binary patch", result.staged_diff.text)

    async def test_inspection_does_not_change_index_head_or_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            (repository / "change.txt").write_text("change\n", encoding="utf-8")
            before_head = _git(repository, "rev-parse", "HEAD")
            before_status = _git(repository, "status", "--porcelain=v2", "-z")
            index = repository / _git(repository, "rev-parse", "--git-path", "index")
            before_index = index.read_bytes()

            await LocalGitInspectionAdapter(LocalGitWorktreeAdapter()).inspect(repository)

            self.assertEqual(_git(repository, "rev-parse", "HEAD"), before_head)
            self.assertEqual(_git(repository, "status", "--porcelain=v2", "-z"), before_status)
            self.assertEqual(index.read_bytes(), before_index)

    async def test_local_git_configuration_cannot_enable_inspection_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            _git(repository, "config", "core.hooksPath", str(repository / "attacker-hooks"))
            _git(repository, "config", "core.fsmonitor", "true")
            _git(repository, "config", "status.recurseSubmodules", "yes")
            _git(repository, "config", "diff.external", "/definitely/not-a-diff-command")

            result = await LocalGitInspectionAdapter(LocalGitWorktreeAdapter()).inspect(repository)

            self.assertEqual(result.completeness.value, "complete")
            self.assertEqual(result.status.entries, ())

    async def test_unsupported_line_ending_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            _git(repository, "config", "core.eol", "unexpected")

            with self.assertRaises(GitInspectionError) as raised:
                await LocalGitInspectionAdapter(LocalGitWorktreeAdapter()).inspect(
                    repository,
                    GitInspectionView.STATUS,
                )
            self.assertEqual(
                raised.exception.kind,
                GitInspectionFailureKind.UNSAFE_CONFIGURATION,
            )

    async def test_submodule_worktree_is_not_recursed_but_gitlink_metadata_is_reported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            root = Path(directory)
            submodule_source = root / "submodule-source"
            submodule_source.mkdir()
            _git(submodule_source, "init", "-q")
            _git(submodule_source, "config", "user.email", "neuro-code-tests@example.invalid")
            _git(submodule_source, "config", "user.name", "Neuro Code Tests")
            (submodule_source / "tracked.txt").write_text("submodule\n", encoding="utf-8")
            _git(submodule_source, "add", "tracked.txt")
            _git(submodule_source, "commit", "-qm", "initial")

            repository = _new_repository(root)
            _git(
                repository,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(submodule_source),
                "nested",
            )
            _git(repository, "commit", "-qm", "add submodule")

            internal_path = repository / "nested" / "submodule-internal-untracked.txt"
            internal_path.write_text("must-not-be-inspected\n", encoding="utf-8")
            adapter = LocalGitInspectionAdapter(LocalGitWorktreeAdapter())
            dirty_submodule = await adapter.inspect(repository)

            self.assertEqual(dirty_submodule.status.entries, ())
            self.assertNotIn(
                "submodule-internal-untracked.txt", json.dumps(dirty_submodule.to_dict())
            )

            nested_repository = repository / "nested"
            _git(nested_repository, "config", "user.email", "neuro-code-tests@example.invalid")
            _git(nested_repository, "config", "user.name", "Neuro Code Tests")
            _git(nested_repository, "commit", "--allow-empty", "-qm", "advance submodule")
            changed_gitlink = await adapter.inspect(repository)

            self.assertEqual(
                [(entry.path, entry.submodule) for entry in changed_gitlink.status.entries],
                [("nested", True)],
            )
            self.assertNotIn(
                "submodule-internal-untracked.txt", json.dumps(changed_gitlink.to_dict())
            )

    async def test_worktree_failures_map_to_typed_fail_closed_inspection_errors(self) -> None:
        failing = AsyncMock()
        failing.git_version.return_value = (2, 40, 0)
        failing.repository_identity.side_effect = WorktreeError(
            "timeout", kind=WorktreeFailureKind.TIMEOUT
        )
        adapter = LocalGitInspectionAdapter(cast(LocalGitWorktreeAdapter, failing))

        with self.assertRaises(GitInspectionError) as raised:
            await adapter.inspect(Path.cwd())
        self.assertEqual(raised.exception.kind, GitInspectionFailureKind.TIMEOUT)

    async def test_unsupported_git_is_a_typed_fail_closed_inspection_error(self) -> None:
        failing = AsyncMock()
        failing.git_version.side_effect = WorktreeError(
            "installed Git is too old", kind=WorktreeFailureKind.NOT_AVAILABLE
        )
        adapter = LocalGitInspectionAdapter(cast(LocalGitWorktreeAdapter, failing))

        with self.assertRaises(GitInspectionError) as raised:
            await adapter.inspect(Path.cwd())
        self.assertEqual(raised.exception.kind, GitInspectionFailureKind.NOT_AVAILABLE)

    async def test_cancellation_is_a_typed_fail_closed_inspection_error(self) -> None:
        failing = AsyncMock()
        failing.git_version.return_value = (2, 40, 0)
        failing.repository_identity.side_effect = WorktreeError(
            "cancelled", kind=WorktreeFailureKind.CANCELLED
        )
        adapter = LocalGitInspectionAdapter(cast(LocalGitWorktreeAdapter, failing))

        with self.assertRaises(GitInspectionError) as raised:
            await adapter.inspect(Path.cwd())
        self.assertEqual(raised.exception.kind, GitInspectionFailureKind.CANCELLED)

    async def test_non_repository_is_a_typed_fail_closed_inspection_error(self) -> None:
        with (
            tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory,
            self.assertRaises(GitInspectionError) as raised,
        ):
            await LocalGitInspectionAdapter(LocalGitWorktreeAdapter()).inspect(Path(directory))
        self.assertEqual(raised.exception.kind, GitInspectionFailureKind.NOT_REPOSITORY)

    async def test_read_only_runner_requests_read_only_workspace_and_disables_global_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            request_root = Path(directory)

            class Output:
                def __init__(self, chunks: tuple[bytes, ...] = ()) -> None:
                    self._chunks = list(chunks)

                async def read(self, _size: int = -1, /) -> bytes:
                    if self._chunks:
                        return self._chunks.pop(0)
                    return b""

            class Process:
                def __init__(self, returncode: int, stdout: bytes = b"") -> None:
                    self.stdout = Output((stdout,) if stdout else ())
                    self.stderr = Output()
                    self._returncode = returncode

                async def wait(self) -> int:
                    return self._returncode

                async def terminate(self, *, grace_seconds: float | None = None) -> None:
                    del grace_seconds

            class Sandbox:
                request = None

                async def spawn(self, request: object) -> Process:
                    self.request = request
                    arguments = cast(SandboxedProcessRequest, request).arguments
                    if arguments[-1:] == ("core.autocrlf",):
                        return Process(returncode=0, stdout=b"true\0")
                    if arguments[-1:] == ("core.eol",):
                        return Process(returncode=1)
                    return Process(returncode=0)

            sandbox = Sandbox()
            adapter = LocalGitWorktreeAdapter(local_process_sandbox=cast(object, sandbox))
            await adapter.run_read_only_git(request_root, ("--version",))

            request = sandbox.request
            assert request is not None
            self.assertEqual(
                request.filesystem_policy.workspace_roots[0].mode,
                LocalWorkspaceAccessMode.READ_ONLY,
            )
            self.assertEqual(request.environment_policy.variables["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(request.environment_policy.variables["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(request.environment_policy.variables["GIT_CONFIG_GLOBAL"], os.devnull)

            await adapter.run_read_only_git(
                request_root,
                ("status", "--porcelain=v2", "--branch", "-z", "--ignore-submodules=dirty"),
            )
            request = sandbox.request
            assert request is not None
            self.assertIn("status.recurseSubmodules=no", request.arguments)
            self.assertIn("core.autocrlf=true", request.arguments)

    async def test_safe_line_ending_values_are_normalized_into_adapter_overrides(self) -> None:
        cases = (
            ("core.autocrlf", "true", "true"),
            ("core.autocrlf", "yes", "true"),
            ("core.autocrlf", "on", "true"),
            ("core.autocrlf", "1", "true"),
            ("core.autocrlf", "false", "false"),
            ("core.autocrlf", "no", "false"),
            ("core.autocrlf", "off", "false"),
            ("core.autocrlf", "0", "false"),
            ("core.autocrlf", "input", "input"),
            ("core.eol", "lf", "lf"),
            ("core.eol", "crlf", "crlf"),
            ("core.eol", "native", "native"),
        )

        for key, raw_value, expected_value in cases:
            with self.subTest(key=key, value=raw_value):
                with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
                    sandbox = _FakeGitSandbox({key: f"{raw_value}\0".encode()})
                    adapter = LocalGitWorktreeAdapter(local_process_sandbox=cast(object, sandbox))

                    with patch(
                        "neuro_code.infrastructure.git.worktree.shutil.which",
                        return_value="/fake/git",
                    ):
                        await adapter.run_read_only_git(Path(directory), ("status",))

                    request = sandbox.requests[-1]
                    override = f"{key}={expected_value}"
                    self.assertIn(override, request.arguments)
                    override_index = request.arguments.index(override)
                    self.assertEqual(request.arguments[override_index - 1], "-c")

    async def test_malformed_or_unsupported_line_ending_values_fail_closed(self) -> None:
        cases = (
            ("core.autocrlf", b"maybe\0"),
            ("core.autocrlf", b"true"),
            ("core.autocrlf", b"true\x01\0"),
            ("core.eol", b"windows\0"),
            ("core.eol", b"lf"),
        )

        for key, output in cases:
            with self.subTest(key=key, output=output):
                with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
                    sandbox = _FakeGitSandbox({key: output})
                    adapter = LocalGitWorktreeAdapter(local_process_sandbox=cast(object, sandbox))

                    with patch(
                        "neuro_code.infrastructure.git.worktree.shutil.which",
                        return_value="/fake/git",
                    ):
                        with self.assertRaises(WorktreeError) as raised:
                            await adapter.run_read_only_git(Path(directory), ("status",))

                    self.assertEqual(
                        raised.exception.kind,
                        WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
                    )
                    self.assertFalse(
                        any("status" in request.arguments for request in sandbox.requests)
                    )

    async def test_config_probes_are_allowlisted_and_status_diff_disable_global_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            sandbox = _FakeGitSandbox(
                {
                    "core.autocrlf": b"yes\0",
                    "core.eol": b"native\0",
                }
            )
            adapter = LocalGitWorktreeAdapter(local_process_sandbox=cast(object, sandbox))

            with patch(
                "neuro_code.infrastructure.git.worktree.shutil.which",
                return_value="/fake/git",
            ):
                await adapter.run_read_only_git(Path(directory), ("status", "--porcelain=v2"))
                status_request = sandbox.requests[-1]
                await adapter.run_read_only_git(
                    Path(directory), ("diff", "--cached", "--", "tracked.txt")
                )
                diff_request = sandbox.requests[-1]

            probe_prefix = ("config", "--null", "--includes", "--get-all")
            probe_requests = [
                request
                for request in sandbox.requests
                if request.arguments[-5:-1] == probe_prefix
            ]
            self.assertEqual(
                [request.arguments[-1] for request in probe_requests],
                ["core.autocrlf", "core.eol", "core.autocrlf", "core.eol"],
            )
            for request in probe_requests:
                self.assertEqual(
                    request.filesystem_policy.workspace_roots[0].mode,
                    LocalWorkspaceAccessMode.READ_ONLY,
                )
                self.assertNotIn("GIT_CONFIG_NOSYSTEM", request.environment_policy.variables)
                self.assertNotIn("GIT_CONFIG_GLOBAL", request.environment_policy.variables)

            for request in (status_request, diff_request):
                self.assertEqual(
                    request.filesystem_policy.workspace_roots[0].mode,
                    LocalWorkspaceAccessMode.READ_ONLY,
                )
                self.assertEqual(request.environment_policy.variables["GIT_CONFIG_NOSYSTEM"], "1")
                self.assertEqual(
                    request.environment_policy.variables["GIT_CONFIG_GLOBAL"],
                    os.devnull,
                )
                self.assertIn("core.autocrlf=true", request.arguments)
                self.assertIn("core.eol=native", request.arguments)

    async def test_final_identity_recheck_accepts_an_unchanged_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            head = _git(repository, "rev-parse", "HEAD")
            identity = WorktreeRepositoryIdentity(
                common_dir=repository / ".git",
                source_worktree=repository,
                git_dir=repository / ".git",
                head_sha=head,
            )
            status = b"# branch.oid " + head.encode() + b"\0# branch.head main\0"
            fake_git = AsyncMock()
            fake_git.git_version.return_value = (2, 40, 0)
            fake_git.repository_identity.side_effect = (identity, identity)
            fake_git.run_read_only_git.return_value = (status, b"", 0)

            result = await LocalGitInspectionAdapter(
                cast(LocalGitWorktreeAdapter, fake_git)
            ).inspect(repository)

            self.assertEqual(result.repository.head_sha, head)
            self.assertEqual(fake_git.repository_identity.await_count, 2)

    async def test_final_identity_recheck_rejects_head_or_repository_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            repository = _new_repository(Path(directory))
            head = _git(repository, "rev-parse", "HEAD")
            initial = WorktreeRepositoryIdentity(
                common_dir=repository / ".git",
                source_worktree=repository,
                git_dir=repository / ".git",
                head_sha=head,
            )
            status = b"# branch.oid " + head.encode() + b"\0# branch.head main\0"
            changed_identities = (
                (
                    "head",
                    WorktreeRepositoryIdentity(
                        common_dir=repository / ".git",
                        source_worktree=repository,
                        git_dir=repository / ".git",
                        head_sha="b" * 40,
                    ),
                ),
                (
                    "repository",
                    WorktreeRepositoryIdentity(
                        common_dir=Path(directory) / "other-repository" / ".git",
                        source_worktree=repository,
                        git_dir=repository / ".git",
                        head_sha=head,
                    ),
                ),
                (
                    "source worktree",
                    WorktreeRepositoryIdentity(
                        common_dir=repository / ".git",
                        source_worktree=Path(directory) / "other-worktree",
                        git_dir=repository / ".git",
                        head_sha=head,
                    ),
                ),
            )

            for label, final in changed_identities:
                with self.subTest(identity=label):
                    fake_git = AsyncMock()
                    fake_git.git_version.return_value = (2, 40, 0)
                    fake_git.repository_identity.side_effect = (initial, final)
                    fake_git.run_read_only_git.return_value = (status, b"", 0)

                    with self.assertRaises(GitInspectionError) as raised:
                        await LocalGitInspectionAdapter(
                            cast(LocalGitWorktreeAdapter, fake_git)
                        ).inspect(repository)

                    self.assertEqual(
                        raised.exception.kind,
                        GitInspectionFailureKind.IDENTITY_MISMATCH,
                    )
                    self.assertEqual(fake_git.repository_identity.await_count, 2)


class GitInspectionProtocolTests(unittest.TestCase):
    def test_porcelain_v2_parser_handles_branch_rename_conflict_and_untracked_records(self) -> None:
        sha = b"a" * 40
        output = b"\0".join(
            (
                b"# branch.oid " + sha,
                b"# branch.head main",
                b"# branch.upstream origin/main",
                b"# branch.ab +2 -1",
                b"1 .M N... 100644 100644 100644 " + sha + b" " + sha + b" path with spaces.txt",
                b"2 R. N... 100644 100644 100644 " + sha + b" " + sha + b" R100 renamed.txt",
                b"old name.txt",
                b"u UU N... 100644 100644 100644 100644 "
                + sha
                + b" "
                + sha
                + b" "
                + sha
                + b" conflict.txt",
                b"1 .M SC.. 160000 160000 160000 " + sha + b" " + sha + b" nested-module",
                b"? new.txt",
                b"",
            )
        )

        status = parse_git_status_porcelain(output)

        self.assertEqual(status.staged_count, 1)
        self.assertEqual(status.unstaged_count, 2)
        self.assertEqual(status.untracked_count, 1)
        self.assertEqual(status.unmerged_count, 1)
        self.assertEqual(status.entries[1].kind.value, "renamed")
        self.assertEqual(status.entries[1].original_path, "old name.txt")
        self.assertEqual(status.entries[2].kind.value, "unmerged")
        self.assertTrue(status.entries[3].submodule)

    def test_malformed_porcelain_and_excess_entries_fail_closed(self) -> None:
        with self.assertRaises(WorktreeError) as malformed:
            parse_git_status_porcelain(b"# branch.oid invalid\0")
        self.assertEqual(malformed.exception.kind, WorktreeFailureKind.PROTOCOL)

        sha = b"b" * 40
        header = b"# branch.oid " + sha + b"\0# branch.head main\0"
        ordinary = b"1 .M N... 100644 100644 100644 " + sha + b" " + sha + b" file\0"
        with self.assertRaises(WorktreeError) as excessive:
            parse_git_status_porcelain(header + ordinary * 513)
        self.assertEqual(excessive.exception.kind, WorktreeFailureKind.OUTPUT_LIMIT)

    def test_diff_bounds_are_explicitly_truncated_and_never_complete(self) -> None:
        projection = project_git_diff(
            b"x" * (MAX_GIT_INSPECTION_DIFF_SECTION_BYTES + 1),
            redaction_values=(),
        )

        self.assertEqual(projection.completeness.value, "truncated")
        self.assertEqual(projection.byte_count, MAX_GIT_INSPECTION_DIFF_SECTION_BYTES)

    def test_diff_redacts_a_secret_that_crosses_the_section_boundary(self) -> None:
        secret = "BOUNDARY_SECRET_SENTINEL"
        prefix = "x" * (
            MAX_GIT_INSPECTION_DIFF_SECTION_BYTES - len("api_key=") - len(secret) + len(secret) // 2
        )
        raw = f"api_key={prefix}{secret}\n".encode()

        projection = project_git_diff(raw, redaction_values=())

        self.assertGreater(len(raw), MAX_GIT_INSPECTION_DIFF_SECTION_BYTES)
        self.assertEqual(projection.completeness.value, "truncated")
        self.assertIn("[REDACTED]", projection.text)
        self.assertNotIn(secret, projection.text)
        self.assertNotIn("BOUNDARY_SECRET", projection.text)
        self.assertEqual(
            projection.byte_count,
            len(projection.text.encode("utf-8", "surrogateescape")),
        )

    def test_binary_projection_keeps_metadata_and_discards_binary_patch_payload(self) -> None:
        projection = project_git_diff(
            b"\n".join(
                (
                    b"diff --git a/deleted.bin b/deleted.bin",
                    b"deleted file mode 100644",
                    b"Binary files a/deleted.bin and /dev/null differ",
                    b"GIT binary patch",
                    b"literal 99",
                    b"not-public-binary-payload",
                )
            ),
            redaction_values=(),
        )

        self.assertEqual(projection.binary_paths, ("deleted.bin",))
        self.assertIn("Binary files a/deleted.bin and /dev/null differ", projection.text)
        self.assertNotIn("GIT binary patch", projection.text)
        self.assertNotIn("not-public-binary-payload", projection.text)
        self.assertTrue(projection.redacted)


class GitInspectionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_service_rejects_relative_workspace_and_tool_has_no_path_argument(
        self,
    ) -> None:
        service = AsyncMock()
        application = GitInspectionService(cast(GitInspectionApplication, service))
        with self.assertRaises(GitInspectionError) as raised:
            await application.inspect(Path("relative"))
        self.assertEqual(raised.exception.kind, GitInspectionFailureKind.PATH_UNSAFE)

        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            root = _new_repository(Path(directory))
            real_adapter = LocalGitInspectionAdapter(LocalGitWorktreeAdapter())
            result = await real_adapter.inspect(root, GitInspectionView.STATUS)
            service.inspect = AsyncMock(return_value=result)
            tool = GitInspectTool(cast(GitInspectionApplication, service))
            tool_result = await tool.execute({}, ToolContext(root))

            self.assertFalse(tool.side_effecting)
            self.assertIsInstance(tool_result, ToolResult)
            service.inspect.assert_awaited_once_with(root, GitInspectionView.ALL)
            self.assertNotIn("path", tool.definition.input_schema["properties"])

    async def test_tool_registry_exposes_git_inspection_only_for_a_local_normal_capability(
        self,
    ) -> None:
        service = cast(GitInspectionApplication, AsyncMock())
        self.assertNotIn("git_inspect", default_tool_registry().names())
        self.assertIn("git_inspect", default_tool_registry(git_inspection=service).names())
        self.assertNotIn(
            "git_inspect",
            default_tool_registry(
                git_inspection=service,
                client_file_system=SimpleNamespace(supports_read=False, supports_write=False),
            ).names(),
        )
        self.assertIn(
            "git_inspect",
            default_tool_registry(
                git_inspection=service, allowed_tool_names=("git_inspect",)
            ).names(),
        )


class GitInspectionCliTests(unittest.TestCase):
    def test_git_view_is_not_silently_ignored_by_configuration_inspect(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "only valid for inspect git"):
            run_inspect_command(
                SimpleNamespace(inspect_kind=None, view="status", json=False, cwd=Path.cwd()),
                cast(
                    object,
                    SimpleNamespace(load_config=lambda _cwd: SimpleNamespace(cwd=Path.cwd())),
                ),
            )

    def test_inspect_git_parser_and_cli_use_the_typed_application_projection(self) -> None:
        parsed = build_parser().parse_args(["inspect", "git", "--view", "status", "--json"])
        self.assertEqual(parsed.inspect_kind, "git")
        self.assertEqual(parsed.view, "status")
        self.assertTrue(parsed.json)
        self.assertIsNone(build_parser().parse_args(["inspect"]).inspect_kind)

        class Services:
            def load_config(self, cwd: Path | None) -> SimpleNamespace:
                assert cwd is not None
                return SimpleNamespace(cwd=cwd)

            def create_git_inspection_service(
                self,
                _config: object,
            ) -> GitInspectionApplication:
                return self.service

        with tempfile.TemporaryDirectory(prefix="neuro-git-inspect-") as directory:
            root = _new_repository(Path(directory))
            service = GitInspectionService(LocalGitInspectionAdapter(LocalGitWorktreeAdapter()))
            services = Services()
            services.service = service
            args = SimpleNamespace(
                inspect_kind="git",
                view="status",
                json=True,
                cwd=root,
            )
            stream = StringIO()
            with redirect_stdout(stream):
                self.assertEqual(run_inspect_command(args, cast(object, services)), 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["view"], "status")
            self.assertEqual(payload["status"]["entries"], [])


if __name__ == "__main__":
    unittest.main()
