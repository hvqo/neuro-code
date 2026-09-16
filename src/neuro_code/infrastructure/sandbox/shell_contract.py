"""Shared local shell contract for launchers and model-facing guidance.

本地 Shell 启动器与面向模型指引共享的执行契约.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Final

POSIX_SYSTEM_SHELL: Final = Path("/bin/sh")
WINDOWS_SYSTEM_SHELL_NAME: Final = "cmd.exe"


class LocalShellDialect(StrEnum):
    """The shell dialect used by the canonical local-process launchers."""

    POSIX_SH = "posix-sh"
    WINDOWS_CMD = "windows-cmd"
    PLATFORM_NATIVE = "platform-native"


def current_shell_dialect() -> LocalShellDialect:
    """Return the dialect selected by the host platform process adapter."""

    if os.name == "posix":
        return LocalShellDialect.POSIX_SH
    if os.name == "nt":
        return LocalShellDialect.WINDOWS_CMD
    return LocalShellDialect.PLATFORM_NATIVE


def posix_system_shell_executable() -> str | None:
    """Return the explicit POSIX shell used by system-shell subprocesses."""

    return (
        str(POSIX_SYSTEM_SHELL) if current_shell_dialect() is LocalShellDialect.POSIX_SH else None
    )


def model_shell_guidance() -> str:
    """Render bounded, truthful shell and command-observability guidance."""

    dialect = current_shell_dialect()
    if dialect is LocalShellDialect.POSIX_SH:
        contract = (
            "Commands run with POSIX `/bin/sh` semantics, not Bash. Do not assume Bash-only "
            "syntax such as `set -o pipefail`. If Bash-specific behavior is required and Bash "
            "is available, invoke it explicitly, for example `bash -c '...'`."
        )
    elif dialect is LocalShellDialect.WINDOWS_CMD:
        contract = (
            "Commands run through the trusted Windows `cmd.exe` shell. Do not assume POSIX or "
            "Bash syntax; use Windows-compatible command syntax and invoke another shell "
            "explicitly when it is available."
        )
    else:
        contract = (
            "Commands use the platform-native system shell. Do not assume POSIX or Bash syntax; "
            "invoke a specific shell explicitly when required."
        )
    return (
        f"{contract} Independent quality checks should be separate commands so each keeps its "
        "own exit status and output; avoid one giant fragile shell expression. Do not hide "
        "long-running test or build output behind a pipeline such as `pytest ... | tail`; when "
        "managed task controls are available, start a managed task, inspect incremental output "
        "with `task_output`, and wait for the terminal result with `wait_tasks`."
    )


__all__ = [
    "POSIX_SYSTEM_SHELL",
    "WINDOWS_SYSTEM_SHELL_NAME",
    "LocalShellDialect",
    "current_shell_dialect",
    "model_shell_guidance",
    "posix_system_shell_executable",
]
