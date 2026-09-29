# ADR 0184: TUI Color System and Contrast Foundation V1A

[简体中文](../../zh-CN/adr/0184-tui-color-system-and-contrast-v1a.md) · **English**

- Status: Accepted
- Date: 2026-09-29

## Context

The 81 V0 snapshots established a pre-redesign baseline. They exposed a
semantic-role collapse, especially in System: surface, selected surface,
border, muted text, and composer chrome often resolved to the same ANSI
`bright_black`. The source aliases were named separately, but their final
Textual/Rich values were not. Interaction tests could pass while the rendered
hierarchy became unreadable.

## Decision

Use one compact semantic color contract across Textual and Rich rendering:

| Layer | Roles |
| --- | --- |
| Structure | `canvas`, `surface`, `surface_subtle`, `surface_selected` |
| Reading | `text_primary`, `text_secondary`, `text_muted` |
| Boundaries | `border_subtle`, `border_normal`, `border_focus` |
| Meaning | `accent`, `success`, `warning`, `error` |

Existing `BG_*`, `FG_*`, and component variables remain compatibility aliases
of these roles. They do not create another palette authority. The final theme
mapping must distinguish the canvas from meaningful surfaces, selected from
ordinary surfaces, visible boundaries from their fill, and foreground text
from the background on which it appears. Focus is stronger than an ordinary
boundary. Assistant prose is the main reading layer; user, tool, status, and
composer colors maintain a quieter order without changing geometry or font
weight policy. Syntax colors remain isolated from conversation colors.

Graphite and Porcelain are the measured dark/light reference themes. For their
ordinary text on all relevant surfaces, use WCAG 4.5:1 as a regression guard;
normal boundaries and focus use approximately 3:1. The ordered
primary/secondary/muted contrast is also guarded. Subtle separators can be
quieter. These checks reject accidental collapses, not certify visual quality
or every terminal rendering.

System has a different responsibility: preserve legibility under an unknown
terminal palette. The terminal default foreground/background provide the
canvas and main text. One ANSI panel fill distinguishes meaningful surfaces;
it is never reused as panel foreground, border, or composer hint. Selection
uses terminal/default and reverse semantics, dim distinguishes lower-priority
text, and only limited ANSI colors communicate focus or status. An unknown
palette cannot guarantee three numeric luminance levels or WCAG ratios, so
System uses semantic-collision tests instead of fabricated RGB measurements.
Its deterministic Textual ANSI SVG represents the test renderer, not every
user's terminal palette.

V1A explicitly updates all 81 V0 snapshots. It changes only palette values,
semantic color mappings, and color intensity. It does not change composer
height, width, padding, margins, message geometry, header, Markdown hierarchy,
Tool Activity, Settings/Trace layout, Runtime, Context, or persistence.

## Consequences and validation

Theme selection continues to change only palette, not layout semantics.
Snapshots protect the three V0 themes at three viewports; contrast and role
tests protect the final values, while a theme-switch test checks widget regions.
The gallery can compare committed V0 SVGs with V1A side by side using
`--before-ref`. See `tests/visual/README.md` for commands and the ANSI caveat.
The remaining visual-system phases can change geometry only under a separate
review.
