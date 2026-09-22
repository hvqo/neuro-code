"""Clipboard image resource-ownership tests.

剪贴板图片资源所有权测试.

Ctrl+V image attachment must keep working, but the interface must own the temp
files it creates: oversized payloads are rejected before a temp file exists, a
failed attachment never reports success, and owned resources are released on
removal, after a successful send, and during teardown.  User-supplied `/attach`
files are never TUI-owned and must survive remove/send.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.sessions.attachments import (
    MAX_IMAGE_ATTACHMENT_BYTES,
    AttachmentError,
)
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.clipboard import ClipboardImage
from neuro_code.interfaces.tui.controllers import turns as turns_module
from tests.test_tui import AttachmentCapturingTuiConversation

_PNG = b"\x89PNG\r\n\x1a\n" + b"payload" * 8


class _ScriptedClipboardReader:
    def __init__(self, image: ClipboardImage | None = None) -> None:
        self.image = image

    def read_image(self) -> ClipboardImage | None:
        return self.image


def _clipboard_image(size: int | None = None) -> ClipboardImage:
    data = _PNG if size is None else b"\x89PNG" + b"x" * size
    return ClipboardImage(media_type="image/png", data=data)


class ClipboardOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_clipboard_image_attaches_and_is_owned(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image())
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+v")
            for _ in range(20):
                await pilot.pause()
                if app._clipboard_temp_paths:
                    break

            owned = tuple(app._clipboard_temp_paths)
            self.assertEqual(len(owned), 1)
            self.assertTrue(owned[0].exists())
            self.assertTrue(any("Clipboard image attached" in e.text for e in app.entries))
            self.assertFalse(any("Attachment rejected" in e.text for e in app.entries))

    async def test_oversized_clipboard_image_is_rejected_without_temp_creation(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image(MAX_IMAGE_ATTACHMENT_BYTES + 1))
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        real_mkstemp = tempfile.mkstemp
        calls: list[object] = []

        def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            calls.append(args)
            return real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]

        async with app.run_test(size=(120, 40)) as pilot:
            with patch.object(turns_module.tempfile, "mkstemp", side_effect=recording_mkstemp):
                await pilot.press("ctrl+v")
                await pilot.pause()
                await pilot.pause()

            self.assertEqual(calls, [])
            self.assertEqual(tuple(app._clipboard_temp_paths), ())
            self.assertTrue(any("Attachment rejected" in e.text for e in app.entries))
            self.assertFalse(any("Clipboard image attached" in e.text for e in app.entries))

    async def test_invalid_clipboard_attachment_does_not_report_success(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image())
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            with patch.object(
                turns_module,
                "build_attachments",
                side_effect=AttachmentError("synthetic rejection"),
            ):
                await pilot.press("ctrl+v")
                await pilot.pause()
                await pilot.pause()

            self.assertTrue(any("Attachment rejected" in e.text for e in app.entries))
            self.assertFalse(any("Clipboard image attached" in e.text for e in app.entries))
            self.assertEqual(tuple(app._clipboard_temp_paths), ())

    async def test_removing_clipboard_attachment_cleans_its_owned_resource(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image())
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+v")
            for _ in range(20):
                await pilot.pause()
                if app._clipboard_temp_paths:
                    break
            owned = next(iter(app._clipboard_temp_paths))
            self.assertTrue(owned.exists())

            await pilot.click("#attachment-remove-0")
            for _ in range(20):
                await pilot.pause()
                if not app._pending_attachment_paths:
                    break

            self.assertFalse(owned.exists())
            self.assertEqual(tuple(app._clipboard_temp_paths), ())
            self.assertEqual(app._pending_attachment_paths, ())

    async def test_sending_clipboard_attachment_cleans_its_owned_resource(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image())
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+v")
            for _ in range(20):
                await pilot.pause()
                if app._clipboard_temp_paths:
                    break
            owned = next(iter(app._clipboard_temp_paths))

            prompt = app.query_one("#prompt", turns_module.PromptInput)
            prompt.value = "look"
            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause()
                if not app._clipboard_temp_paths:
                    break

            self.assertTrue(runner.captured_parts)
            self.assertFalse(owned.exists())
            self.assertEqual(tuple(app._clipboard_temp_paths), ())

    async def test_rewound_turn_keeps_resources_and_restores_attachments(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image())
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+v")
            for _ in range(20):
                await pilot.pause()
                if app._clipboard_temp_paths:
                    break
            owned = next(iter(app._clipboard_temp_paths))

            # A rewound turn must keep the owned resource and re-queue the
            # attachment, so the restored draft can be resent with its image.
            app._submitted_attachment_paths = (str(owned),)
            app._turn_pristine_rewound = True
            app._finalize_attachment_resources()
            self.assertTrue(owned.exists())

            await app._restore_submitted_attachments()
            self.assertEqual(app._pending_attachment_paths, (str(owned),))

            # Once the turn is durable, the resource is released.
            app._turn_pristine_rewound = False
            app._finalize_attachment_resources()
            self.assertFalse(owned.exists())
            self.assertEqual(tuple(app._clipboard_temp_paths), ())

    async def test_user_attach_file_survives_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "shot.png"
            image.write_bytes(_PNG)
            runner = AttachmentCapturingTuiConversation()
            app = NeuroCodeApp(
                runner,
                provider_name="fixture",
                model_name="fixture-model",
                cwd=root,
            )
            async with app.run_test(size=(120, 40)) as pilot:
                prompt = app.query_one("#prompt", turns_module.PromptInput)
                prompt.value = f"/attach {image}"
                await pilot.press("enter")
                for _ in range(20):
                    await pilot.pause()
                    if app._pending_attachment_paths:
                        break

                self.assertEqual(app._pending_attachment_paths, (str(image),))
                # A user-supplied file is never TUI-owned.
                self.assertEqual(tuple(app._clipboard_temp_paths), ())

                await pilot.click("#attachment-remove-0")
                for _ in range(20):
                    await pilot.pause()
                    if not app._pending_attachment_paths:
                        break

                self.assertTrue(image.exists(), "user attachment must never be deleted")
                self.assertEqual(app._pending_attachment_paths, ())

    async def test_pending_clipboard_resources_are_cleaned_on_shutdown(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image())
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+v")
            for _ in range(20):
                await pilot.pause()
                if app._clipboard_temp_paths:
                    break
            owned = next(iter(app._clipboard_temp_paths))
            self.assertTrue(owned.exists())
            app.exit()

        self.assertFalse(owned.exists())

    async def test_cleanup_is_safe_when_repeated(self) -> None:
        reader = _ScriptedClipboardReader(_clipboard_image())
        runner = AttachmentCapturingTuiConversation()
        app = NeuroCodeApp(
            runner,
            provider_name="fixture",
            model_name="fixture-model",
            cwd=Path("/tmp"),
            clipboard_image_reader=reader,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+v")
            for _ in range(20):
                await pilot.pause()
                if app._clipboard_temp_paths:
                    break

            app._release_clipboard_resources()
            app._release_clipboard_resources()
            self.assertEqual(tuple(app._clipboard_temp_paths), ())


if __name__ == "__main__":
    unittest.main()
