# ADR 0190: Empty State Torsion Lock Reveal V2C-B

**English** · [简体中文](../../zh-CN/adr/0190-empty-state-torsion-lock-reveal-v2c-b.md)

- Date: 2026-10-02
- Status: Accepted implementation; Draft PR awaiting terminal visual acceptance
- Scope: TUI presentation only
- Predecessor: [ADR 0189](0189-static-empty-state-identity-v2c-a.md)

## Decision

Ship the reviewed **Variant E — Torsion Lock** as optional, user-initiated brand
motion. The V2C-A core geometry remains exact. At user request, resting contrast
is reduced: existing `text-dim` at 28% foreground alpha plus `dim`, blending against
the existing canvas when RGB is known. Unknown ANSI defaults retain terminal `dim`
without inventing RGB values. There is no startup playback, loop, loading meaning or Agent-state meaning.
Only a left click inside the logo widget starts one activation; repeated clicks
while running are ignored. The widget cannot own keyboard focus: a click requests
focus on the existing Composer without changing editor/input behavior.

`EmptyStateIdentity` owns canonical resting content, frame selection and one
one-shot timer. `empty_state_reveal.py` contains immutable, precomputed Braille
poses for all three accepted sizes. Neither PNG loading nor image transformation
occurs at runtime. Source provenance remains the SHA256 in `empty_state_logo.py`.
The source's connected outlines are grouped into six angular motion domains,
without cutting contours or redesigning the accepted mark. A–D candidates are
not shipped in the runtime.

## Geometry, style and time

Every frame has the existing fixed bounding box: large 32×16, medium 24×12,
small 16×8. Both endpoints exactly reuse canonical V2C-A rows. Pre-tension,
opening, finite torsion, quick lock, tiny rebound and settle provide structural
motion; brightness is secondary. Styling resolves current UI semantic components:
`text-dim` + `dim`, `text-muted`, `text-secondary`. No new palette, RGB literals,
accent flash, interpolation or continuous rotation is introduced.

The original 720ms exploration was too brief in terminal review. The same twelve
poses now use a user-requested six-second, non-uniform sequence. This changes
residence time rather than adding a loop or extra rotation. The times in milliseconds are:

```text
400 / 350 / 350 / 450 / 600 / 500 / 400 / 350 / 400 / 800 / 600 / 800 = 6000 ms
```

A monotonic elapsed clock selects the actual frame and schedules its next
boundary. A delayed event loop can skip obsolete frames rather than accumulate
timing drift. Refresh is a repaint of **only this widget**, `layout=False`;
per-frame `Static.update()` would invalidate layout and is deliberately avoided.
Textual still owns compositor/terminal output; this is not a claim that a terminal
can update without any screen-composition work.

## Cancellation and capabilities

First real content, transcript reset/resume/restore, hiding, insufficient space,
size-class change, theme change, unmount or shutdown cancels and releases the
one-shot timer, then restores canonical resting content or hides the widget.
Callbacks carry an activation generation so already-queued callbacks cannot
advance a later activation. A same-size reposition preserves progress; changing
large/medium/small cancels rather than maps animation phases across sizes.
No timer exists at rest. No worker or polling interval is created.

`NO_COLOR`, deterministic headless/snapshot mode, ANSI16 and unknown output
capability stay static. Existing Textual/Rich color capability or the existing
trusted terminal palette can authorize TrueColor/ANSI256 motion. There is no new
terminal probe or terminal-name heuristic. A visible static logo still allows
Composer focus on click. The Agent pulse is untouched; real content removes the
logo before Agent activity can coexist with its motion.

## Validation and manual review

Regression tests cover full playback, exact return, click/repeat, stale callbacks,
cancellation, focus and CJK/paste/Enter, size/theme changes, static capability
fallback, timer cleanup and unchanged shell geometry. Six visual states are
captured for Graphite/Porcelain/System at 120×40, 100×32 and 80×24 (54 baselines).
Snapshots freeze the production clock only inside the test harness.

```bash
uv run python -m tests.visual.empty_reveal.render_preview --output /tmp/neuro-reveal.html
uv run python -m tests.visual.empty_reveal.render_preview --terminal --theme system
```

The HTML player uses actual production Textual frame captures; it is a structural
preview, not proof of real terminal timing/CPU cost. Repeat/speed controls belong
only to that player. The provider-free native preview runs the real widget with
normal click and keyboard behavior. Konsole acceptance must check Braille font
alignment, the 6000ms feel, quiet resting/peak contrast, repeated clicks, typing
and multiline paste during playback, resize and first-message cancellation.
System snapshots are deterministic ANSI representations, not every terminal's
palette. No Streaming Presence/Motion is part of this decision.
