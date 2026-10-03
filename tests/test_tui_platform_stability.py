"""Regression tests for stable, cross-platform TUI presentation contracts."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from neuro_code.interfaces.tui.controllers.runtime import _path_for_display


def test_workspace_path_display_uses_stable_slashes_for_posix_and_windows() -> None:
    posix = PurePosixPath("/workspace/neuro-code/src")
    windows = PureWindowsPath(r"C:\workspace\neuro-code\src")
    windows_fixture = PureWindowsPath(r"\workspace\neuro-code")

    assert _path_for_display(posix) == "/workspace/neuro-code/src"
    assert _path_for_display(windows) == "C:/workspace/neuro-code/src"
    assert _path_for_display(windows_fixture) == "/workspace/neuro-code"
