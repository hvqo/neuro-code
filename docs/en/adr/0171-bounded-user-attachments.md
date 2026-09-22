# ADR 0171: Bounded user attachments

[简体中文](../../zh-CN/adr/0171-bounded-user-attachments.md) · **English**

- Status: Accepted
- Date: 2026-09-22
- Scope: User-provided file/image attachments on turn input (TUI and CLI)

## Context

The runtime has always accepted `ContentPart` payloads on a user turn, and the
provider adapters already serialize inline images with per-provider media and
size rules. The ACP entry point exposes that pipeline. The interactive TUI and
the headless CLI had no way to attach anything, and the request estimator
counted a message's base64 image payload as text, so one multi-megabyte image
inflated the estimate by hundreds of thousands of phantom tokens and could
wrongly block the turn in `ContextPreflight`.

## Decision

**Estimation projection.** `domain/conversation/request.py` now projects media
parts as bounded accounting facts — media type, decoded byte count, and a short
digest — instead of embedding the encoded payload, and `estimate_model_request_tokens`
adds a fixed `IMAGE_PART_TOKEN_ESTIMATE` (1,536) per image part. Fingerprints
stay deterministic and content-sensitive because the digest covers the decoded
bytes.

**Attachment pipeline.** `application/sessions/attachments.py` owns the bounded
conversion from user-supplied paths to content parts: images (suffix-based
media type, ≤5 MiB each, ≤10 MiB total, ≤8 per turn) become data-URI IMAGE
parts; decodable text files (≤64 KiB each, ≤128 KiB total, ≤8 per turn) are
inlined into the composed prompt behind an `[Attached file: …]` header;
everything else is rejected with a readable reason. Paths resolve against the
active workspace and `~` expands. Attachments are user input: they never touch
the sandbox or the approval flow.

**Entries.** The TUI gains `/attach <PATH>...` with an attachment tray above
the composer (click ✕ to remove) and `Ctrl+V` pastes the system clipboard
image through a new `ClipboardImageReader` port (xclip/wl-paste, osascript
PNGf, PowerShell); with no image on the clipboard `Ctrl+V` falls back to
TextArea's text paste. Submitting with an empty prompt is allowed when
attachments are pending. The CLI gains repeatable `--attach <PATH>`. ACP is
unchanged and now shares the same bounds vocabulary.

**Message shape.** A turn with media parts carries the composed prompt as its
single TEXT part next to the media parts, satisfying the existing
`Message` invariant that `content` equals the TEXT-part projection. Turns
without media parts are byte-identical to plain turns.

## Consequences

- Attaching a multi-megabyte image no longer blocks the turn; the estimate is
  stable and small regardless of image size.
- Attachment contents are user-owned data: they are persisted with the session,
  are not indexed by session search, and are never redacted or treated as
  agent-initiated reads.
- Clipboard capture shells out to platform tools that may be absent; the
  shortcut then reports that no clipboard image is available and falls back to
  text paste.
- The per-provider media allowlists still decide what actually reaches a
  provider; unsupported media fails at the provider boundary as before.
