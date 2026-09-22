"""Bounded user-attachment conversion for turn input.

用户回合输入的有界附件转换.

Attachments are *user-provided* turn input, not agent tool calls: reading them
never touches the sandbox or the approval flow, but the same size and media
bounds as the ACP inline-prompt contract apply so no entry point can push an
unbounded payload into a turn.

附件是用户提供的回合输入,而不是代理的工具调用:读取它们不经过沙箱或审批流程,
但适用与 ACP 内联提示词契约相同的尺寸与媒体上限,确保任何入口都无法把无界负载
推进回合.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from neuro_code.domain.conversation.messages import ContentPart

MAX_IMAGE_ATTACHMENTS = 8
MAX_IMAGE_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 10 * 1024 * 1024
MAX_TEXT_ATTACHMENTS = 8
MAX_TEXT_ATTACHMENT_BYTES = 64 * 1024
MAX_TEXT_TOTAL_BYTES = 128 * 1024

IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
_IMAGE_SUFFIX_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

__all__ = [
    "IMAGE_MEDIA_TYPES",
    "MAX_IMAGE_ATTACHMENTS",
    "MAX_IMAGE_ATTACHMENT_BYTES",
    "MAX_IMAGE_TOTAL_BYTES",
    "MAX_TEXT_ATTACHMENTS",
    "MAX_TEXT_ATTACHMENT_BYTES",
    "MAX_TEXT_TOTAL_BYTES",
    "Attachment",
    "AttachmentError",
    "build_attachments",
    "compose_turn_input",
]


class AttachmentError(ValueError):
    """Raised when a user attachment cannot become a bounded content part.

    当用户附件无法成为有界内容部件时抛出."""


@dataclass(frozen=True, slots=True)
class Attachment:
    """One user attachment prepared for a turn.

    为回合准备的一个用户附件."""

    path: Path
    kind: str
    media_type: str
    size_bytes: int
    part: ContentPart


def _resolve_path(raw: str | Path, workspace: Path) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise AttachmentError("attachment path must not be empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path


def _image_attachment(path: Path, size: int) -> Attachment:
    if size > MAX_IMAGE_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"image attachment is too large: {path.name} "
            f"({size} > {MAX_IMAGE_ATTACHMENT_BYTES} bytes)"
        )
    suffix = path.suffix.casefold()
    media_type = _IMAGE_SUFFIX_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise AttachmentError(f"unsupported image attachment type: {path.name}")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return Attachment(
        path=path,
        kind="image",
        media_type=media_type,
        size_bytes=size,
        part=ContentPart.from_image(f"data:{media_type};base64,{payload}"),
    )


def _text_attachment(path: Path, size: int) -> Attachment:
    if size > MAX_TEXT_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"text attachment is too large to inline: {path.name} "
            f"({size} > {MAX_TEXT_ATTACHMENT_BYTES} bytes); ask the agent to read it instead"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise AttachmentError(
            f"attachment is not decodable UTF-8 text: {path.name}; "
            "ask the agent to read it with its tools instead"
        ) from error
    return Attachment(
        path=path,
        kind="text",
        media_type="text/plain",
        size_bytes=size,
        part=ContentPart.from_text(text),
    )


def build_attachments(
    paths: Sequence[str | Path],
    *,
    workspace: Path,
) -> tuple[Attachment, ...]:
    """Resolve and validate user attachments against the bounded contract.

    Resolves paths against ``workspace``, reads every file, and enforces the
    per-kind and total bounds.  The whole list is validated so callers can keep
    a pending set and re-validate it as one unit.

    相对 ``workspace`` 解析路径,读取每个文件,并执行按类别与总量上限.
    整个列表一起校验,调用方可以维护待发送集合并将其作为一个整体重新校验.
    """

    if not paths:
        return ()
    workspace = Path(workspace).expanduser().resolve() if workspace else Path.cwd()
    attachments: list[Attachment] = []
    images = 0
    image_total = 0
    texts = 0
    text_total = 0
    for raw in paths:
        path = _resolve_path(raw, workspace)
        if not path.is_file():
            raise AttachmentError(f"attachment path is not a file: {path}")
        size = path.stat().st_size
        if size == 0:
            raise AttachmentError(f"attachment is empty: {path.name}")
        if path.suffix.casefold() in _IMAGE_SUFFIX_MEDIA_TYPES:
            images += 1
            image_total += size
            if images > MAX_IMAGE_ATTACHMENTS:
                raise AttachmentError(f"too many image attachments (max {MAX_IMAGE_ATTACHMENTS})")
            if image_total > MAX_IMAGE_TOTAL_BYTES:
                raise AttachmentError(
                    f"image attachments exceed the total limit "
                    f"({image_total} > {MAX_IMAGE_TOTAL_BYTES} bytes)"
                )
            attachments.append(_image_attachment(path, size))
        else:
            texts += 1
            text_total += size
            if texts > MAX_TEXT_ATTACHMENTS:
                raise AttachmentError(f"too many text attachments (max {MAX_TEXT_ATTACHMENTS})")
            if text_total > MAX_TEXT_TOTAL_BYTES:
                raise AttachmentError(
                    f"text attachments exceed the total limit "
                    f"({text_total} > {MAX_TEXT_TOTAL_BYTES} bytes)"
                )
            attachments.append(_text_attachment(path, size))
    return tuple(attachments)


def compose_turn_input(
    prompt: str,
    attachments: Sequence[Attachment],
) -> tuple[str, tuple[ContentPart, ...]]:
    """Merge the typed prompt with attachments into one canonical turn input.

    Text attachments are inlined into the composed prompt; image attachments
    become data-URI IMAGE parts.  A message that carries media parts must keep
    its ``content`` equal to the projection of its TEXT parts, so the composed
    prompt becomes the single TEXT part next to the images.  Turns without
    media parts return no parts at all and stay byte-identical to plain turns.

    文本附件内联进合成提示词;图片附件成为 data URI 的 IMAGE 部件.携带媒体部件的消息
    必须保持 ``content`` 等于其 TEXT 部件投影,因此合成提示词作为图片旁的唯一 TEXT
    部件.没有媒体部件的回合不返回任何部件,与普通回合完全一致.
    """

    media_parts = [attachment.part for attachment in attachments if attachment.kind == "image"]
    composed = prompt
    for attachment in attachments:
        if attachment.kind != "text":
            continue
        composed = (
            f"{composed}\n\n[Attached file: {attachment.path.name}]\n{attachment.part.text}"
            if composed
            else f"[Attached file: {attachment.path.name}]\n{attachment.part.text}"
        )
    if not media_parts:
        return composed, ()
    parts: list[ContentPart] = []
    if composed.strip():
        parts.append(ContentPart.from_text(composed))
    parts.extend(media_parts)
    return composed, tuple(parts)
