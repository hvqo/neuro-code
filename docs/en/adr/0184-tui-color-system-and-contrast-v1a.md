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
of these roles. They do not create another palette authority. Where a theme uses
filled surfaces, the final mapping distinguishes the canvas, selection, visible
boundaries, and foreground text from their backgrounds. System deliberately
uses fewer fills instead of simulating a dark palette. Focus is stronger than
an ordinary boundary. Assistant prose is the main reading layer; user, tool,
status, and composer colors maintain a quieter order without changing geometry
or font-weight policy. Syntax colors remain isolated from conversation colors.

Graphite and Porcelain are the measured dark/light reference themes. For their
ordinary text on all relevant surfaces, use WCAG 4.5:1 as a regression guard;
normal boundaries and focus use approximately 3:1. The ordered
primary/secondary/muted contrast is also guarded. Subtle separators can be
quieter. These checks reject accidental collapses, not certify visual quality
or every terminal rendering.

System is a terminal-aware adaptive palette, not a transparent/default-only
theme. A small startup adapter makes one bounded OSC 10/11 query before
Textual owns terminal input. When both defaults are returned with sufficient
contrast and output is TrueColor or ANSI256, surfaces and secondary text are
derived from the actual foreground/background pair. ANSI16, unsupported
queries, malformed replies, and low-contrast pairs fail soft to terminal
defaults, visible ANSI-white panel rules, dim secondary text, reverse
selection, and ANSI-blue focus. Probe failure adds at most 60 ms and does not
prevent startup. System never uses `ansi_bright_black` as a broad fill.

Canvas, subtle/selected surface, Composer, and User Message roles remain
separate where the output capability can represent them. The System-only
Composer rule reuses its existing top inset, so the measured geometry and text
coordinates remain unchanged; focus recolors that rule with the adaptive
accent, or ANSI bright blue in fallback mode. Inline Markdown code retains the
V1A foreground-only/no-background contract; fenced code and diffs retain their
independent styling. When the terminal palette is unavailable, RGB contrast
cannot be claimed; deterministic semantic-role tests cover that fallback.
Textual SVGs use injected dark, light, or unknown fixtures and still do not
represent every real terminal palette. Real-terminal review remains necessary.

V1A updates the 81 V0 snapshots and adds six deterministic System screenshots
for dark, light, and unknown palettes across conversation and Settings views.
This correction changes System palette resolution and surface/border colors
only. It preserves the previously approved inline-code fix and does not change
composer height, width, margins, message geometry, header, Markdown hierarchy,
Tool Activity, Settings/Trace layout, Runtime, Context, or persistence. The
Composer's existing one-cell top inset becomes its rule; the inner content
coordinates stay fixed.

## Consequences and validation

Theme selection continues to change only palette, not layout semantics.
Snapshots protect the three V0 themes at three viewports; six additional
snapshots pin the adaptive/fallback palette fixtures. Contrast and role tests
protect final values, while theme-switch and focus tests check widget regions
and affordance colors. The gallery can compare committed V0 SVGs with V1A side
by side using `--before-ref`; palette-only samples appear as V1A-only cards.
See `tests/visual/README.md` for commands and the ANSI caveat.
The remaining visual-system phases can change geometry only under a separate
review.
