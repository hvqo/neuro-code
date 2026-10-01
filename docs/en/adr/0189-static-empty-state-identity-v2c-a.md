# ADR 0189: Static Empty State Identity V2C-A

[简体中文](../../zh-CN/adr/0189-static-empty-state-identity-v2c-a.md) · **English**

- Status: Accepted
- Date: 2026-10-01

## Context

An empty conversation lacks a central visual anchor. The previously selected
Braille mark came from an incorrect reference. The corrected 212×212 RGBA source
has SHA256 `8334fe13506ca923f09b16933c6c7e5713346d349ada6eb0675ef97021398a6e`.
It includes a hexagonal badge and six rotating central blades. All obsolete source
rows/masks are removed; previous logo snapshots are withdrawn to the main baseline.
The lifecycle/layout work is preserved. The user rejected the hexagonal border and
selected B: only the six central blades. A/C remain exploration records only.

## Decision

`EmptyStateIdentity` owns the empty binding's presentation gate and geometry. Its
artwork seam consumes only fixed terminal rows, never the original desktop PNG.
Production consumes only `empty_state_logo.LOGO_ROWS`, the corrected-source core
mark without a hexagonal outline. The exploration records A full badge, B core
mark and C core with a sparse weak outline. Offline color segmentation isolates the core and
border; the filled background is excluded and aspect ratio is retained. CI needs
no image dependency; Pillow is only used by an optional offline conversion script.

Resting uses existing `TEXT_DIM` / `text-dim` plus dim intensity; no logo palette,
preference, provider call, transcript item or persisted identity state is added.
Activated peak is a static gallery preview using existing secondary foreground
without dim; the hybrid outline stays sparse and dim. Returning to resting restores
the exact prior bytes. No input activation, animation, timer or reveal is shipped.

A screen overlay is centered inside `TranscriptScroll.content_region`, excluding
Header, Composer and Footer. It has no layout allocation or scroll extent. The
transcript emits a presentation resize notification, so draft growth and panel
changes reposition the symbol without polling, runtime coupling or new timers.

The terminal-size policy chooses 32×16 at >=120×40, 24×12 at >=100×32 and 16×8
otherwise. Hide below 70 columns / 22 rows, or when the actual conversation rectangle
cannot accommodate the asset plus 8 columns / 4 rows of clearance. Never squeeze
Composer or crop the symbol to fit. Resize recomputes from canonical static rows.

A fresh empty binding shows the logo; meaningful user/assistant content immediately
latches it off, including streaming and restoration. Tool/error activity also hides
it so it cannot act as a watermark. Ordinary system notices do not consume the gate.
Transcript replacement resets the gate then reconstructs it from restored entries;
existing/resumed conversations therefore remain hidden. The overlay belongs to the
main screen, so modal lifecycle cannot reset that binding's gate.

No animation or reveal is implemented. A static symbol avoids an additional motion
source, remains deterministic and respects the approved scope. Syntax Theme,
Markdown, keyboard normalization, Composer geometry and runtime behavior are frozen.

## Verification and limitations

Tests cover all three themes/viewports, unchanged shell geometry/scroll extent,
first-content disappearance, actual startup resume, binding reset, modal ownership,
draft-growth recentering, insufficient space and large→compact→large byte stability.
Official empty/draft snapshots are updated to the selected borderless core mark.
Corrected-source A/B/C screenshots remain separate exploration records.
First-message/restored-history fixtures are retained.
The gallery covers all three themes/viewports, resting/peak/returned-resting, focus,
first message, resumed history and resize; no live provider or terminal probe.

Braille relies on a single-cell Unicode font. Browser SVGs and deterministic ANSI
fixtures cannot prove real Konsole font strokes, glyph width or terminal palette.
Manual acceptance must check Graphite/Porcelain/System at all three sizes, focus,
Chinese multiline/paste, first send, resumed history and resize before merge.
