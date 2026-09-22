"""Clipboard image reader tests.

剪贴板图片读取器测试.
"""

from __future__ import annotations

import unittest

from neuro_code.interfaces.tui.clipboard import SystemClipboardImageReader

_MACOS_PNGF = "«data PNGf" + b"\x89PNGdata".hex() + "»"


class SystemClipboardImageReaderTests(unittest.TestCase):
    def test_reads_the_first_available_platform_image(self) -> None:
        commands: list[list[str]] = []

        def run_command(command: list[str]) -> bytes | None:
            commands.append(command)
            return b"png-bytes"

        reader = SystemClipboardImageReader(
            platform_name="linux",
            environment={"WAYLAND_DISPLAY": "wayland-0"},
            find_executable=lambda name: "/usr/bin/" + name,
            run_command=run_command,
        )

        image = reader.read_image()

        self.assertIsNotNone(image)
        self.assertEqual(image.media_type, "image/png")
        self.assertEqual(image.data, b"png-bytes")
        self.assertEqual(commands[0][0], "/usr/bin/wl-paste")

    def test_missing_executables_yield_none(self) -> None:
        reader = SystemClipboardImageReader(
            platform_name="linux",
            environment={},
            find_executable=lambda _name: None,
            run_command=lambda _command: b"png-bytes",
        )

        self.assertIsNone(reader.read_image())

    def test_failing_commands_fall_through(self) -> None:
        attempts: list[str] = []

        def run_command(command: list[str]) -> bytes | None:
            attempts.append(command[0])
            return None

        reader = SystemClipboardImageReader(
            platform_name="linux",
            environment={"DISPLAY": ":0"},
            find_executable=lambda name: "/usr/bin/" + name,
            run_command=run_command,
        )

        self.assertIsNone(reader.read_image())
        self.assertEqual(attempts, ["/usr/bin/xclip", "/usr/bin/wl-paste"])

    def test_macos_pngf_payload_is_decoded(self) -> None:
        reader = SystemClipboardImageReader(
            platform_name="darwin",
            environment={},
            find_executable=lambda name: "/usr/bin/" + name,
            run_command=lambda _command: _MACOS_PNGF.encode("utf-8"),
        )

        image = reader.read_image()

        self.assertIsNotNone(image)
        self.assertEqual(image.data, b"\x89PNGdata")

    def test_windows_uses_powershell(self) -> None:
        seen: list[list[str]] = []
        reader = SystemClipboardImageReader(
            platform_name="win32",
            environment={},
            find_executable=lambda name: "C:/Windows/" + name,
            run_command=lambda command: seen.append(list(command)) or b"png-bytes",
        )

        image = reader.read_image()

        self.assertIsNotNone(image)
        self.assertEqual(seen[0][0], "C:/Windows/powershell")
        self.assertIn("-STA", seen[0])


if __name__ == "__main__":
    unittest.main()
