# ADR 0183: TUI Visual Contract and Snapshot Harness V0

[简体中文](../../zh-CN/adr/0183-tui-visual-contract-v0.md) · **English**

- Status: Accepted
- Date: 2026-09-29

## Context

Neuro Code already has semantic theme tokens, a shared conversation reading axis,
grouped tool activity, adaptive composer sizing, and headless Textual tests. The
tests protect interaction and rendering contracts, but there is no repeatable,
reviewable full-screen visual baseline across viewport sizes and themes. A visual
regression can therefore pass behavior tests while changing the overall reading
experience.

This V0 establishes screenshot regression fixtures before any visual redesign. It
does not change production styles, theme values, Markdown rules, Tool Activity,
Runtime, Agent behavior, Prompt or Context, Session behavior, or Provider behavior.

## Decision

Add deterministic SVG screenshots exported by Textual from the real TUI widgets.
The fixtures use a local fake runner, fixed labels and clock, fixed workspace
metadata, no network, and no persisted Session or Provider. The checked-in matrix
covers empty conversation, user plus assistant, long Markdown, grouped tool
activity, error, permission approval, multiline composer, Settings, and Trace at
120×40, 100×32, and 80×24 under Graphite, Porcelain, and System.

The following principles are the visual contract for later TUI work:

- **Neutral first:** ordinary conversation and interface structure use neutral
  surfaces and text.
- **Normal weight by default:** reserve bold weight for headings, selection, and
  meaningful emphasis.
- **One primary accent:** use the primary accent for interaction and focus; avoid
  competing decorative accents.
- **Semantic color:** success, warning, and error colors communicate meaningful
  state only.
- **Separate syntax color:** code syntax has its own palette and must not redefine
  the conversation palette.
- **Compact adaptive composer:** composer height follows useful input content and
  available viewport space.
- **Clear reading axis and hierarchy:** conversation, status, and activity remain
  aligned and visibly ordered.
- **Adaptive light/dark hierarchy:** maintain contrast and surface distinction in
  light, dark, and terminal-provided color environments.
- **Palette-only theme changes:** themes may change colors, but not layout
  semantics or control placement.

These are constraints for review, not a prescription to redesign the current TUI
in this phase. Codex TUI source was consulted for principles such as restrained
semantic color, adaptive composer behavior, source-based Markdown reflow, and
separate history/activity cells; no Rust implementation or directory structure is
copied.

## Known Baseline Defect

The V0 snapshots expose insufficient semantic-role contrast in the System theme:
surface, selected surface, border, dim/muted text, and Composer surface, border,
and muted text map to the same or similar ANSI `bright_black`. This is a known
pre-redesign defect retained in the baseline, not approval of the current visual
quality. System snapshots represent Textual's deterministic ANSI rendering; they
do not represent every user's actual terminal ANSI palette. V1A's first priority
is semantic-role contrast and improved System theme adaptation.

## Consequences

- Full-screen changes can be reviewed against committed, viewport-specific output.
- SVG keeps the baseline inspectable and makes text, geometry, and palette changes
  diffable without adding a rendering dependency.
- Snapshot updates require explicit opt-in and visual inspection; normal tests
  never rewrite expected output.
- Future visual redesign work should update the contract or explain deviations in
  its design review before broad component changes.

## Validation

Run the focused suite with `uv run pytest tests/test_tui_visual_snapshots.py -q`.
See `tests/visual/README.md` for deterministic baseline regeneration and gallery
inspection instructions. This ADR does not authorize production visual changes.
