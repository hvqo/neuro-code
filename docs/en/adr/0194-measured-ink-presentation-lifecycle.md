# ADR 0194: Measured Ink presentation lifecycle

**English** · [简体中文](../../zh-CN/adr/0194-measured-ink-presentation-lifecycle.md)

- Date: 2026-10-05
- Status: Accepted implementation; native visual acceptance pending
- Scope: Presentation lifecycle only; supersedes ADR 0191's receive-time expiry and resize cancellation

## Evidence and decision

The visual signal audit reproduced two failures. A scrollbar changed message
width while terminal size stayed constant; `on_resize → stop_arrival` discarded
unseen arrivals. Cold render/layout took 286–420ms, longer than the exploration's
280ms receive-based lifespan. These are lifecycle defects, not color design.

Keep the existing production effect unchanged: 20fps / 180ms / twelve glyphs,
25ms independent view commits, canonical deltas immediately received intact.
No new variant, palette, timer per glyph, lexer or fence behavior.

## Source lifecycle

`Arrival.received_at` records ordering/diagnostics. `ArrivalTimeline.births[start]`
records the first **presented_at**. States are derived, not a second state store:

- RECEIVED: registered range, no presentation birth and no animation clock.
- PRESENTED: a source-proven, whole glyph first occurs in `render_lines(crop)`'s
  visible strip crop. `start_visual(source, now)` records birth once. Age is
  `now - presented_at`, never time since receipt.
- SETTLED: presented age exceeds duration. Canonical style is restored and the
  shared clock stops. Keep bounded birth tombstones so rebuild, scroll/repaint,
  theme refresh and appended combining marks cannot restart old glyphs.

The crop is Textual's presentation boundary, not an acknowledgement of physical
GPU scanout. A partial wide glyph replaced by padding cannot start presentation.
Enumerating cached sources never starts a birth. Off-screen glyphs remain pending;
previously presented glyphs keep aging while off-screen.

Pending provenance is bounded by 256 ranges and the last 384 source code points,
not by animation duration. Source progress/count can evict old pending ranges;
no unbounded delayed-animation backlog. Births share that source window. A real
message-generation boundary, complete/cancel/error/discard, hiding, restore or
unmount clears the timeline. Animation-off and reduced-capability paths remain static.

## Geometry and style ownership

Both internal width changes and actual app/terminal resize invalidate line layout
and dirty row identities only. Preserve pending ranges, existing births and the
single clock; the next visible render rebuilds source-to-row mapping. No terminal
name heuristics and no need to guess why a widget width changed. A content commit
also discards row geometry but retains source lifecycle.

UI/syntax theme and localization refresh use `restyle_stream` for the same source
generation. Render styles/cache keys resolve again; birth times remain unchanged.
Final response/restore uses canonical `update` and clears streaming state. Theme
changes do not restart settled text. FenceRenderCache and fence presentation are
unchanged; syntax only receives its existing cache-key invalidation.

## Validation and replay

Tests cover 0/50/150/300/400/500ms stalls, a 400ms stall with 280ms duration,
156→155→156 width events at constant viewport, real scrollbar overflow,
120×40→100×32→80×24→120×40, whole/cropped CJK glyphs, unseen sources, theme
refresh, combining marks, generation reset, bounded state and zero idle text work.

The explicit test tool uses production mapping/lifecycle and a synthetic intact
540-line fence + prose tape. Sentinel is test-only reverse+bold for 500ms, with
the production twelve-glyph cap. It is never a setting or production variant.

```bash
uv run python -m tests.visual.text_arrival.lifecycle_replay --sentinel --theme system
uv run python -m tests.visual.text_arrival.lifecycle_replay --off --theme system
uv run python -m tests.visual.text_arrival.lifecycle_replay --headless --output /tmp/ink-lifecycle.json
```

Enter plays, Ctrl+C exits; `--recording /path/deltas.jsonl` preserves whole deltas
and timing. Metrics/frames go outside the repo. Native replay observes the existing
writer's file writes/flushes; headless CPU/serialization is not Konsole perception.
Tick-only parse/layout remains zero; settled assistant refresh/parse/timers are zero.
The independent existing Agent pulse may continue. Fixing previously lost animation
adds intended row repaints; it is not a free CPU optimization or a remedy for
subtle color curves. Full Markdown parse and all canonical styles remain unchanged.
