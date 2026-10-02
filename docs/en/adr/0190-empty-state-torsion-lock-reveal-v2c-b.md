# ADR 0190: Empty State Vortex Reveal V2C-B

**English** · [简体中文](../../zh-CN/adr/0190-empty-state-torsion-lock-reveal-v2c-b.md)

- Date: 2026-10-02
- Status: Accepted implementation; Draft PR awaiting terminal visual acceptance
- Scope: TUI presentation only
- Predecessor: [ADR 0189](0189-static-empty-state-identity-v2c-a.md)

## Decision and design correction

The previous twelve-pose Torsion Lock held frames for 350–800ms when stretched to
six seconds, producing visible stutter. The user's `neuro_vortex.py` now supplies
motion principles: counter-moving radial layers, 3D tilt/perspective, short trails
and one outward impulse. This replaces the old poses with continuous samples.
Keep the accepted V2C-A six-blade resting artwork. Do not import the reference's
alternative logo, RGB palette or terminal-control loop.
Reference SHA256: `9e2850264ecff7a3b5c90323e2cfeb8959eeea4d1de0367e66c7d8ed6dd49de4`.

Default resting remains existing `text-dim` at 28% alpha plus `dim`, blended with
canvas when RGB is known. Unknown ANSI retains terminal dim without guessed RGB.
There is no autoplay, loop, loading or Agent-state meaning. Only a left click in
the logo activates it; running clicks are ignored. The non-focusable widget asks
the existing Composer to recover focus without changing keyboard/IME/paste/history.

## Data, geometry, style and time

`EmptyStateIdentity` owns canonical resting content, sample selection, semantic
styles and one one-shot timer. `scripts/generate_empty_vortex.py` generates
trajectories offline from canonical Braille dots. `empty_state_vortex.json` stores
fixed samples, loaded as immutable rows/intensity by `empty_state_reveal.py`.
Runtime never reads Downloads or PNG, projects particles or executes the reference.

- Fixed boxes: large 32×16, medium 24×12, small 16×8; no layout changes.
- 24fps, 6000ms: 144 intervals of 41/42ms; 145 samples include both endpoints.
  The final sample adds no dwell time.
- Endpoint rows exactly equal canonical production Resting with zero intensity;
  completion restores the static render.
- Finite counter-moving angles in outer/middle/inner layers; tilt/perspective make
  the structure move. Short trails, one outward impulse and sparse deterministic
  data glyphs support this; no sustained spinner, random glitch or perpetual motion.
- Sixteen cell intensities are theme-neutral metadata. Known RGB interpolates
  existing resting/secondary semantic colors; unknown ANSI uses existing neutral
  foreground and dim. No dedicated palette or hardcoded RGB.
- Resolve/cache styles once per activation and combine same-style Rich text spans.

A monotonic clock selects the sample by binary search and schedules its next
boundary. Late callbacks skip obsolete samples without catch-up queues or drift.
Only the logo calls `refresh(layout=False)` per sample. Textual still owns
compositor/terminal output; this is not a claim of zero screen-composition cost.

## Cancellation and capabilities

First content, reset/resume/restore, hiding, insufficient space, size-class or
theme change, unmount/shutdown releases the timer/style cache and restores canonical
resting or hides. Generation fencing rejects stale queued callbacks. Same-size
reposition preserves progress; changed size classes do not map animation phase.
Resting has no timer, worker or polling interval.

`NO_COLOR`, headless/snapshot, ANSI16 and unknown output stay static. Existing
Textual/Rich output capability or trusted terminal palette authorizes TrueColor/
ANSI256 motion; no new probe or terminal-name heuristic. Static clicks still
recover Composer focus. First content hides the logo; Agent pulse is unchanged.

## Validation and manual review

Tests cover full playback, exact endpoints, real trajectories, 24fps cadence,
late-sample skipping, repeat/cancel, focus, CJK/paste/Enter, size/theme, fallback,
timer cleanup and unchanged shell geometry. Six states (Resting, lift, max-tilt,
reconstruction, settle, returned-resting) cover three themes × three viewports,
54 baselines. Only the test harness freezes the production clock.

```bash
uv run python scripts/generate_empty_vortex.py
uv run python -m tests.visual.empty_reveal.render_preview --output /tmp/neuro-reveal.html
uv run python -m tests.visual.empty_reveal.render_preview --terminal --theme system
```

The HTML player combines the real Textual shell with production sample data,
resolved styles and cell geometry, updating only the logo rather than decoding
full-screen images per sample. Key frames are complete Textual captures. Repeat/
speed belong only to the preview. Native preview uses the real widget/timer/input.
POSIX PTY validates scheduling/output but cannot replace Konsole visual acceptance:
continuous perspective motion, six-second rhythm, Braille/font shape, quiet endpoints,
typing, resize and cancellation. System snapshots are deterministic ANSI output,
not all terminal palettes. Streaming Motion is outside this decision.
