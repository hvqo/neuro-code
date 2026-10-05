# ADR 0191: Measured Ink streaming presentation

**English** · [简体中文](../../zh-CN/adr/0191-measured-ink-streaming-presentation.md)

- Date: 2026-10-03
- Status: Accepted historical baseline; visual parameters superseded by ADR 0195
- Scope: Assistant TUI presentation only

## Decision

The initial implementation adopted exploration D: 20fps / 180ms / at most twelve
recent plain-prose graphemes. Those visual parameters are historical; the current
A22 Materialize contract is defined by ADR 0195. Received provider chunks update canonical source immediately
and intact. No typewriter queue, artificial character release, Runtime event change,
extra cursor, marker or Agent pulse change. The view may coalesce commits within a
25ms budget (40Hz), both with animation on and off, independently of the animation
clock. This is a scheduling budget, not a guaranteed display rate: source
pauses, parsing/rendering and terminal backpressure still affect frame pacing. Animation
follows arrival; it never
controls delivery. Appearance → Input provides one inherited boolean
`text_arrival_animation`; absent preferences resolve to enabled. Existing version-1
preference files remain readable. No advanced timing controls are exposed.

## Provenance and Unicode

`text_arrival.py` accepts only exactly provable top-level literal paragraphs.
Markdown-it source lines, inline content, reconstructed text/softbreak children and
Rich Text must agree. No substring search, guessed offsets or response rewriting.
Formatted paragraphs, headings, links, escaped/entity text, inline/fenced code,
syntax tokens, diff, lists, quotes and tables remain canonical. The entire formatted
paragraph stays static rather than risking a partial semantic override. Custom
non-body semantic styles also fail closed. A short-lived foreground overlay leaves
backgrounds and geometry untouched; final rendering is exactly canonical.

[regex](https://pypi.org/project/regex/) supplies Unicode UAX #29 `\X` grapheme
segmentation, replacing the exploration helper. Source indices are code points,
not terminal cells. Whole safe Latin/Han graphemes receive metadata; Rich/Textual
retain wrapping and cell widths. Emoji, ZWJ, flags and complex shaping remain static.
At most twelve eligible tail glyphs per paragraph receive metadata, restricted to a
384-code-point source window; the current global active overlay cap is eight glyphs
under ADR 0195.
First presentation of a proven visible glyph fixes its birth, including appended
combining marks; receive time is diagnostic only. See [ADR 0194](0194-measured-ink-presentation-lifecycle.md).

## Cache and refresh ownership

`AssistantMessage` owns one bounded arrival timeline, one shared animation clock,
one cancellable asyncio one-shot view deadline and one current Markdown view.
The deadline queues into the widget message pump and always delivers when overdue;
Textual 1.x one-shot timers default to skipping late callbacks and cannot own a
required final view commit. Generation guards cancel already-queued stale callbacks.
The deadline is relative
to the preceding commit; subsequent deltas do not postpone it. Animation ticks never
flush pending content. A single pending post-layout scroll follows committed growth,
retains end-follow intent through consecutive layouts, and is cancelled by upward
history navigation or transcript replacement. Status text updates repaint the existing
fixed-height slot without requesting layout; show/hide and real geometry changes
retain normal Textual layout behavior. Agent pulse content and rhythm are unchanged. Content is immutable within each `AssistantMarkdown` instance;
a content commit creates a new instance and reparses once. Its one-entry line cache
keys width, compact policy, output color capability, base style, resolved syntax theme
and resolved Markdown semantic styles. Theme/syntax/width/viewport changes invalidate
or replace the current view. Motion age is not a cache key. Animation-only ticks do
not instantiate Markdown or parse source; they overlay cached source-tagged strips
and request local affected-row refresh without layout. Textual still composes screen
output; local dirty regions are not a claim of zero compositor cost.

Completion, cancel/error/discard, transcript restore, new response, view hiding
and unmount stop both timers, invalidate outstanding callbacks and clear arrivals.
Resize/reflow and theme/syntax refresh invalidate presentation geometry/styles,
not source lifecycle. The clock starts on first presentation and stops after the
last presented glyph expires; bounded pending/settled identities do not poll.
[ADR 0194](0194-measured-ink-presentation-lifecycle.md) supersedes receive-time expiry. Headless, NO_COLOR, ANSI16/unknown output remain
static. Off mode retains coalescing and caching but creates no animation ranges.

## Verification and limits

Deterministic tests cover intact deltas, cache equivalence, source proof, Unicode,
protected Markdown and custom semantic styles, per-glyph expiry, local ticks, overdue
deadline delivery, explicit commit/layout barriers (message idle is not a timer barrier), theme,
resize, completion/cancel/error/restore and preference inheritance. The retained test-only replay tools exercise the full controller/event path at the
fixed production cadence, with animation on/off. They report commit/visible-update
intervals, exclusive CPU, Markdown/Syntax, layout/scroll, compositor and refresh
counts. Synthetic stress tapes are labelled; optional JSONL recordings preserve
`at_ms` and intact `text` deltas. Runtime event timestamps are not provider socket
receipt times. Headless serialization is not terminal output; `native_pacing` probes
native queueing and writer-thread writes/flushes. `pty_pacing` uses a controlling
POSIX PTY with a fast reader, not a Konsole emulator. Instrumentation exists only
inside explicit test tools, with scoped hooks; it is never loaded by production.
The duplicate early replay and multi-cadence exploration controls were removed.

### Why 25ms / 40Hz

The previous shared 50ms clock limited body commits to about 20Hz. The selected
25ms budget improves typical presentation intervals without increasing the
Measured Ink clock. In one recorded mixed/CJK segment (629 intact deltas, about
13.9s), native-driver replay measured:

| Body budget | Commit P50 / P95 / P99 / max (ms) | CPU seconds |
|---|---|---:|
| Previous 50ms | 50.5 / 51.8 / 60.7 / 69.6 | 2.61 |
| Selected 25ms | 25.5 / 28.2 / 58.6 / 70.9 | 4.03 |

At 25ms, visible tail updates were 25.6 / 40.7 / 57.5 / 81.1ms. CPU increased about
54% (about 28% of one core across the replay); this is a conscious responsiveness
trade-off, not a free optimization. Higher tested rates had diminishing returns.
Measurements include probes and exclude real Konsole paint/font/GPU cost; they
are not Provider performance or guaranteed visual-continuity claims.

Remaining bottlenecks: each content commit still parses full Markdown; long code
still performs full Syntax rendering. Synthetic large-code replay observed a
67.5ms Syntax render and roughly 80ms inclusive layout. Some parser tails overlapped
18–20ms GC pauses. Faster scheduling alone does not solve these long frames.
Animation ticks caused zero Markdown parses; after arrival expiry the text timers
stopped, with zero assistant refresh/layout/parse in the measured idle window.
The independent existing Agent pulse may still run until its turn finishes.

```bash
uv run pytest tests/test_tui_text_arrival.py tests/test_tui_frame_pacing.py -q
uv run python -m tests.visual.text_arrival.frame_pacing --output /tmp/frame-pacing.json
uv run python -m tests.visual.text_arrival.native_pacing --theme system --case long-cjk
# Run in Konsole, press Enter. --off disables only the arrival overlay.
# --recording /path/to/deltas.jsonl preserves recorded delta boundaries/timing.
uv run python -m tests.visual.text_arrival.pty_pacing --output /tmp/native-pacing
```

Konsole perception, font/CJK shaping and remote-terminal repaint cost still require
human review. Long responses still parse full Markdown at view commits; this is not
an incremental parser. Exploration A/B/C and prototype global render monkeypatches
are not part of production. No settled snapshot or UI palette change is intended.
