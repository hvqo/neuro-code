"""Estimation and projection tests for multimodal request payloads.

多模态请求负载的估算与投影测试.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest

from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.messages import ContentPart, Message, Role
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.conversation.request import (
    IMAGE_PART_TOKEN_ESTIMATE,
    build_model_request_payload,
    estimate_model_request_tokens,
)
from neuro_code.domain.tools import ToolDefinition


def _image_part(size: int, fill: bytes = b"\x89PNG") -> ContentPart:
    payload = base64.b64encode((fill * ((size // len(fill)) + 1))[:size]).decode("ascii")
    return ContentPart.from_image(f"data:image/png;base64,{payload}")


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="read_file",
        description="read a file",
        input_schema={"type": "object", "properties": {}},
    )


class InlineImageEstimationTests(unittest.TestCase):
    def test_inline_image_payload_is_bounded_and_estimated_with_allowance(self) -> None:
        image = _image_part(200 * 1024)
        context = ModelContext(
            (Message(Role.USER, "", content_parts=(image,)),),
            "provider",
            "model",
        )
        payload = build_model_request_payload(
            context=context,
            tools=(_tool(),),
            provider="provider",
            model="model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
        )

        # The encoded payload must not embed the base64 image data.
        encoded = json.dumps(payload)
        self.assertNotIn(image.url, encoded)
        self.assertNotIn(base64.b64encode(b"\x89PNG" * 512).decode("ascii")[:64], encoded)

        item = payload["context"][0]
        part = item["content_parts"][0]
        self.assertEqual(part["kind"], "image")
        inline = part["url"]
        self.assertEqual(inline["media_type"], "image/png")
        self.assertEqual(inline["data_bytes"], 200 * 1024)
        self.assertEqual(
            inline["data_sha256"],
            hashlib.sha256(
                base64.b64decode(image.url.removeprefix("data:image/png;base64,"))
            ).hexdigest()[:16],
        )

        estimate = estimate_model_request_tokens(payload)
        self.assertLess(estimate.estimated_input_tokens, 20_000)
        self.assertGreaterEqual(estimate.estimated_input_tokens, IMAGE_PART_TOKEN_ESTIMATE)

    def test_each_image_part_adds_one_allowance(self) -> None:
        context = ModelContext(
            (Message(Role.USER, "", content_parts=(_image_part(1024), _image_part(2048))),),
            "provider",
            "model",
        )
        payload = build_model_request_payload(
            context=context,
            tools=(),
            provider="provider",
            model="model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
        )
        baseline = estimate_model_request_tokens(
            build_model_request_payload(
                context=ModelContext((Message(Role.USER, "two"),), "provider", "model"),
                tools=(),
                provider="provider",
                model="model",
                context_affinity=None,
                reasoning_effort=ReasoningEffort.HIGH,
            )
        )
        estimate = estimate_model_request_tokens(payload)
        # The delta is the two image allowances plus the tiny bounded
        # placeholders the projection emits for them.
        delta = estimate.estimated_input_tokens - baseline.estimated_input_tokens
        self.assertGreaterEqual(delta, 2 * IMAGE_PART_TOKEN_ESTIMATE)
        self.assertLess(delta, 2 * IMAGE_PART_TOKEN_ESTIMATE + 200)

    def test_inline_image_fingerprint_is_deterministic_and_content_sensitive(self) -> None:
        def fingerprint(image: ContentPart) -> str:
            context = ModelContext(
                (Message(Role.USER, "", content_parts=(image,)),),
                "provider",
                "model",
            )
            payload = build_model_request_payload(
                context=context,
                tools=(),
                provider="provider",
                model="model",
                context_affinity=None,
                reasoning_effort=ReasoningEffort.HIGH,
            )
            return json.dumps(payload, sort_keys=True)

        self.assertEqual(fingerprint(_image_part(4096)), fingerprint(_image_part(4096)))
        self.assertNotEqual(
            fingerprint(_image_part(4096, fill=b"\x89PNG")),
            fingerprint(_image_part(4096, fill=b"\x00\x01\x02")),
        )

    def test_audio_parts_are_projected_without_their_payload(self) -> None:
        audio = ContentPart.from_audio(
            base64.b64encode(b"audio-bytes" * 100).decode("ascii"), "audio/wav"
        )
        context = ModelContext((Message(Role.USER, "", content_parts=(audio,)),))
        payload = build_model_request_payload(
            context=context,
            tools=(),
            provider="provider",
            model="model",
            context_affinity=None,
            reasoning_effort=ReasoningEffort.HIGH,
        )
        self.assertNotIn("audio-bytes", json.dumps(payload))
        part = payload["context"][0]["content_parts"][0]
        self.assertEqual(part["data_bytes"], len(b"audio-bytes" * 100))


if __name__ == "__main__":
    unittest.main()
