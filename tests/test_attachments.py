"""Bounded user-attachment conversion tests.

有界用户附件转换测试.
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from neuro_code.application.sessions.attachments import (
    MAX_IMAGE_ATTACHMENT_BYTES,
    build_attachments,
    compose_turn_input,
)
from neuro_code.domain.conversation.messages import ContentPart, ContentPartKind

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 8


class BuildAttachmentTests(unittest.TestCase):
    def test_image_attachment_becomes_a_data_uri_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "shot.png"
            image.write_bytes(_PNG_BYTES)

            attachments = build_attachments([image], workspace=root)

            self.assertEqual(len(attachments), 1)
            attachment = attachments[0]
            self.assertEqual(attachment.kind, "image")
            self.assertEqual(attachment.media_type, "image/png")
            self.assertEqual(attachment.size_bytes, len(_PNG_BYTES))
            self.assertEqual(
                attachment.part,
                ContentPart.from_image(
                    f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode('ascii')}"
                ),
            )

    def test_text_attachment_is_inlined_into_the_composed_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("hello notes", encoding="utf-8")

            attachments = build_attachments(["notes.txt"], workspace=root)

            self.assertEqual(attachments[0].kind, "text")
            prompt, parts = compose_turn_input("fix this", attachments)
            self.assertIn("[Attached file: notes.txt]", prompt)
            self.assertIn("hello notes", prompt)
            # Text-only attachments ride inside the prompt; no parts needed.
            self.assertEqual(parts, ())

    def test_prompt_and_images_keep_the_text_projection_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shot.png").write_bytes(_PNG_BYTES)

            attachments = build_attachments(["shot.png"], workspace=root)
            prompt, parts = compose_turn_input("what is this", attachments)

            self.assertEqual(prompt, "what is this")
            self.assertEqual(parts[0].kind, ContentPartKind.TEXT)
            self.assertEqual(parts[0].text, "what is this")
            self.assertEqual(parts[1].kind, ContentPartKind.IMAGE)
            # The domain invariant: content equals the TEXT-part projection.
            self.assertEqual(
                parts[0].text,
                "\n".join(part.text for part in parts if part.kind is ContentPartKind.TEXT),
            )

    def test_empty_prompt_with_only_media_parts_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shot.png").write_bytes(_PNG_BYTES)

            attachments = build_attachments(["shot.png"], workspace=root)
            prompt, parts = compose_turn_input("", attachments)

            self.assertEqual(prompt, "")
            self.assertEqual([part.kind for part in parts], [ContentPartKind.IMAGE])

    def test_relative_paths_resolve_against_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "docs"
            nested.mkdir()
            (nested / "a.txt").write_text("x", encoding="utf-8")

            attachments = build_attachments(["docs/a.txt"], workspace=root)

            self.assertEqual(attachments[0].path, nested / "a.txt")

    def test_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "big.png").write_bytes(b"\x89PNG" + b"x" * (MAX_IMAGE_ATTACHMENT_BYTES))
            (root / "empty.txt").write_text("", encoding="utf-8")
            (root / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")
            (root / "note.txt").write_text("ok", encoding="utf-8")
            (root / "weird.image").write_bytes(b"plain text content")

            cases = [
                ([root / "missing.png"], "not a file"),
                ([root], "not a file"),
                ([root / "empty.txt"], "empty"),
                ([root / "big.png"], "too large"),
                ([root / "binary.bin"], "not decodable"),
                ([""], "must not be empty"),
            ]
            for paths, expected in cases:
                with (
                    self.subTest(paths=[str(path) for path in paths]),
                    self.assertRaisesRegex(ValueError, expected),
                ):
                    build_attachments(paths, workspace=root)

            # Unknown suffixes are treated as text when they decode.
            unknown = build_attachments([root / "weird.image"], workspace=root)
            self.assertEqual(unknown[0].kind, "text")


if __name__ == "__main__":
    unittest.main()
