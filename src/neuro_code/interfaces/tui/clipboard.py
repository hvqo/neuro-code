"""Native clipboard support for the terminal interface.

终端界面的原生系统剪贴板支持。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

_CLIPBOARD_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ClipboardWriteResult:
    """The outcome of a best-effort native clipboard write.

    尽力写入原生系统剪贴板的结果。
    """

    native_copied: bool
    method: str | None = None

    def __post_init__(self) -> None:
        if self.native_copied != (self.method is not None):
            raise ValueError("native clipboard method must match the success state")


class ClipboardWriter(Protocol):
    """Writes text to the host system clipboard when one is available.

    在可用时将文本写入宿主系统剪贴板。
    """

    def copy(self, text: str) -> ClipboardWriteResult:
        """Copy ``text`` without raising a user-visible TUI failure.

        复制 ``text``,但不能因此向用户暴露 TUI 失败。
        """


class ClipboardCommandRunner(Protocol):
    """Runs one trusted clipboard program with an encoded text payload.

    使用编码后的文本负载运行一个受信任的剪贴板程序。
    """

    def __call__(self, command: Sequence[str], payload: bytes) -> bool:
        """Return whether the native program reported success.

        返回原生程序是否报告成功。
        """


@dataclass(frozen=True, slots=True)
class _ClipboardCommand:
    """A platform clipboard command and its input encoding.

    平台剪贴板命令及其输入编码。
    """

    method: str
    executable: str
    arguments: tuple[str, ...]
    encoding: str = "utf-8"
    byte_order_mark: bytes = b""

    def encode(self, text: str) -> bytes:
        """Encode text for this command without exposing it in command arguments.

        为该命令编码文本,不在命令参数中暴露文本。
        """

        return self.byte_order_mark + text.encode(self.encoding)


def _run_clipboard_command(command: Sequence[str], payload: bytes) -> bool:
    """Run a clipboard command with bounded, silent standard streams.

    使用受限且静默的标准流运行剪贴板命令。
    """

    try:
        completed = subprocess.run(
            command,
            check=False,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_CLIPBOARD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _clipboard_commands(
    platform_name: str,
    environment: Mapping[str, str],
) -> tuple[_ClipboardCommand, ...]:
    """Return ordered native clipboard candidates for the active desktop.

    为当前桌面环境返回按优先级排序的原生剪贴板候选命令。
    """

    if platform_name.startswith("win"):
        return (
            _ClipboardCommand(
                method="windows-clip",
                executable="clip.exe",
                arguments=(),
                encoding="utf-16le",
                byte_order_mark=b"\xff\xfe",
            ),
        )
    if platform_name == "darwin":
        return (_ClipboardCommand("macos-pbcopy", "pbcopy", ()),)

    wayland = bool(environment.get("WAYLAND_DISPLAY"))
    x11 = bool(environment.get("DISPLAY"))
    wayland_command = _ClipboardCommand(
        "wayland-wl-copy",
        "wl-copy",
        ("--type", "text/plain;charset=utf-8"),
    )
    xclip_command = _ClipboardCommand(
        "x11-xclip",
        "xclip",
        ("-selection", "clipboard", "-in"),
    )
    xsel_command = _ClipboardCommand("x11-xsel", "xsel", ("--clipboard", "--input"))
    if wayland:
        return (wayland_command, xclip_command, xsel_command)
    if x11:
        return (xclip_command, xsel_command, wayland_command)
    return ()


class SystemClipboardWriter:
    """Best-effort native clipboard writer with terminal-safe fallback reporting.

    尽力写入原生系统剪贴板,并为终端回退路径提供可靠状态。
    """

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        platform_name: str | None = None,
        find_executable: Callable[[str], str | None] | None = None,
        run_command: ClipboardCommandRunner | None = None,
    ) -> None:
        self._environment = dict(os.environ if environment is None else environment)
        self._platform_name = sys.platform if platform_name is None else platform_name
        self._find_executable = shutil.which if find_executable is None else find_executable
        self._run_command = _run_clipboard_command if run_command is None else run_command

    def copy(self, text: str) -> ClipboardWriteResult:
        """Write text to a native clipboard program if the desktop supports one.

        如果桌面环境支持,则将文本写入原生剪贴板程序。
        """

        for candidate in _clipboard_commands(self._platform_name, self._environment):
            executable = self._find_executable(candidate.executable)
            if executable is None:
                continue
            try:
                copied = self._run_command(
                    (executable, *candidate.arguments),
                    candidate.encode(text),
                )
            except (OSError, subprocess.TimeoutExpired):
                copied = False
            if copied:
                return ClipboardWriteResult(native_copied=True, method=candidate.method)
        return ClipboardWriteResult(native_copied=False)


@dataclass(frozen=True, slots=True)
class ClipboardImage:
    """One image captured from the system clipboard.

    从系统剪贴板捕获的一张图片.
    """

    media_type: str
    data: bytes


class ClipboardImageReader(Protocol):
    """Reads one image from the host system clipboard when available.

    在可用时从宿主系统剪贴板读取一张图片.
    """

    def read_image(self) -> ClipboardImage | None:
        """Return the clipboard image, or ``None`` when none is available.

        返回剪贴板图片;没有图片时返回 ``None``.
        """


@dataclass(frozen=True, slots=True)
class _ClipboardImageCommand:
    """A platform command that prints one clipboard image to stdout.

    将剪贴板图片输出到标准输出的平台命令.
    """

    method: str
    executable: str
    arguments: tuple[str, ...]
    media_type: str = "image/png"
    post_process: str | None = None


_WINDOWS_IMAGE_POWERSHELL = (
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
    "Add-Type -AssemblyName System.Drawing | Out-Null; "
    "$image = [System.Windows.Forms.Clipboard]::GetImage(); "
    "if ($image -ne $null) { "
    "$stream = New-Object System.IO.MemoryStream; "
    "$image.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png); "
    "$bytes = $stream.ToArray(); "
    "[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length) }"
)


def _clipboard_image_commands(
    platform_name: str,
    environment: Mapping[str, str],
) -> tuple[_ClipboardImageCommand, ...]:
    """Return ordered native image-clipboard readers for the active desktop.

    为当前桌面环境返回按优先级排序的原生图片剪贴板读取命令.
    """

    if platform_name.startswith("win"):
        return (
            _ClipboardImageCommand(
                "windows-powershell-image",
                "powershell",
                ("-NoProfile", "-STA", "-Command", _WINDOWS_IMAGE_POWERSHELL),
            ),
        )
    if platform_name == "darwin":
        return (
            _ClipboardImageCommand(
                "macos-pngf",
                "osascript",
                ("-e", "the clipboard as «class PNGf»"),
                post_process="macos-pngf-hex",
            ),
        )
    wayland = bool(environment.get("WAYLAND_DISPLAY"))
    x11 = bool(environment.get("DISPLAY"))
    wayland_command = _ClipboardImageCommand(
        "wayland-wl-paste-image",
        "wl-paste",
        ("--type", "image/png"),
    )
    xclip_command = _ClipboardImageCommand(
        "x11-xclip-image",
        "xclip",
        ("-selection", "clipboard", "-t", "image/png", "-o"),
    )
    if wayland:
        return (wayland_command, xclip_command)
    if x11:
        return (xclip_command, wayland_command)
    return ()


def _run_clipboard_image_command(
    command: Sequence[str],
) -> bytes | None:
    """Run one image-clipboard reader and return its stdout bytes.

    运行一个图片剪贴板读取命令并返回其标准输出字节.
    """

    try:
        completed = subprocess.run(
            list(command),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_CLIPBOARD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


def _decode_macos_pngf(stdout: bytes) -> bytes | None:
    """Decode the ``«data PNGf<hex>»`` payload printed by osascript.

    The guillemets differ between encodings, so the match anchors on the stable
    ``data PNGf`` marker and the hex digits instead of the surrounding quotes.

    解码 osascript 输出的 ``«data PNGf<hex>»`` 负载.引号字符随编码不同,因此匹配
    锚定在稳定的 ``data PNGf`` 标记与十六进制数字上,而非两侧引号.
    """

    text = stdout.decode("utf-8", errors="ignore")
    match = re.search(r"data PNGf([0-9a-fA-F]+)", text)
    if match is None:
        return None
    try:
        return bytes.fromhex(match.group(1))
    except ValueError:
        return None


class SystemClipboardImageReader:
    """Best-effort native clipboard image reader with silent failure modes.

    尽力读取原生剪贴板图片,失败时保持静默.
    """

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        platform_name: str | None = None,
        find_executable: Callable[[str], str | None] | None = None,
        run_command: Callable[[Sequence[str]], bytes | None] | None = None,
    ) -> None:
        self._environment = dict(os.environ if environment is None else environment)
        self._platform_name = sys.platform if platform_name is None else platform_name
        self._find_executable = shutil.which if find_executable is None else find_executable
        self._run_command = _run_clipboard_image_command if run_command is None else run_command

    def read_image(self) -> ClipboardImage | None:
        """Return one clipboard image, or ``None`` when unavailable.

        返回一张剪贴板图片;不可用时返回 ``None``.
        """

        for candidate in _clipboard_image_commands(self._platform_name, self._environment):
            executable = self._find_executable(candidate.executable)
            if executable is None:
                continue
            try:
                stdout = self._run_command((executable, *candidate.arguments))
            except (OSError, subprocess.TimeoutExpired):
                continue
            if stdout is None:
                continue
            data = stdout
            if candidate.post_process == "macos-pngf-hex":
                decoded = _decode_macos_pngf(stdout)
                if decoded is None:
                    continue
                data = decoded
            return ClipboardImage(media_type=candidate.media_type, data=data)
        return None
