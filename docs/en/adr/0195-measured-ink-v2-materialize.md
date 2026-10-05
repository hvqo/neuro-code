# ADR 0195: Measured Ink V2 Materialize

**English** · [简体中文](../../zh-CN/adr/0195-measured-ink-v2-materialize.md)

- Date: 2026-10-05
- Status: Accepted implementation; final production-terminal acceptance pending
- Scope: Assistant prose text-arrival presentation only

## Decision

Use the user-selected A22 Materialize effect for eligible newly presented prose
graphemes. The fixed parameters are ΔL*=22 maximum, 160ms lifetime, 24fps, at most
eight active glyphs, and minimum foreground/background contrast of 4.5:1. The
25ms/40Hz body commit cadence remains independent. Canonical deltas remain whole
and immediate; no Runtime event, model output, code-streaming, or Agent pulse changes.

At first presentation, a glyph's rendered foreground moves toward the actual
canvas/background by at most 22 CIELAB L* units, then follows a monotonic path back
to its canonical foreground. There is no overshoot. At or after 160ms the original
Rich style is used exactly. A newly arriving glyph cannot alter an existing birth
time or restart a settled glyph.

## Color and capability contract

Build a plan from the concrete canonical foreground and the actual visible
background. Graphite/Porcelain use their resolved canvas fill; System requires a
verified terminal palette. TrueColor interpolates in CIELAB and emits RGB. ANSI256
quantizes candidate colors before checking output differences and contrast; the
first frame must differ from canonical and the path must retain at least two
distinct visible quantized colors. If contrast, monotonicity, or visible separation cannot
be proven, that glyph stays canonical. ANSI16, unknown capabilities, and unresolved
colors are static. A contrast-limited path may use less than 22 L*; it never exceeds
the limit or the contrast floor.

Only foreground is overlaid. Rich backgrounds, metadata, and other style
attributes are retained. Existing source proof remains unchanged: only safe,
whole graphemes in literal top-level plain prose are eligible. Headings, formatted
spans, links, inline/fenced code, syntax and diff colors, complex/unsafe graphemes,
and uncertain mappings stay static. The per-paragraph metadata tail remains 12
source glyphs; the active animation cap is eight. Active source identities are
retained across a streamed Markdown rebuild until they settle, without changing
their first-presentation births.

## Lifecycle and refresh ownership

ADR 0194 owns the lifecycle: arrival receipt registers a source range, first
visible presentation sets birth, and the shared clock overlays cached rendered
lines until settle. This ADR changes only the visual plan. Animation OFF creates
no arrival timeline work, timer, or repaint. At 24fps, only affected rows refresh;
ticks do not parse Markdown or request layout. The timer stops when the active set
settles. Resize, theme changes, cancellation, completion, and disposal retain ADR
0194's cleanup and source-identity behavior. Fenced-code streaming remains governed
by ADR 0193 and is excluded from Measured Ink.

## Verification and acceptance

Regression tests cover first-presentation birth, A22 color movement and monotonic
settle, exact canonical restoration, contrast, ANSI256 separation, fail-closed
capabilities, the eight-glyph cap, stable births across append rebuilds, CJK cell
geometry, protected Markdown, OFF zero-refresh behavior, and the existing lifecycle
suite. Manual production acceptance must compare the enabled effect and OFF in a
real Konsole session; headless tests cannot prove terminal palette perception.

```bash
uv run neuro
```

Enable or disable `text_arrival_animation` under Appearance → Input before comparing.
