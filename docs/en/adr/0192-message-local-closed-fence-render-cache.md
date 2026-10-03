# ADR 0192: Message-local closed fence render cache

**English** · [简体中文](../../zh-CN/adr/0192-message-local-closed-fence-render-cache.md)

- Date: 2026-10-03
- Status: Accepted
- Scope: Assistant fenced-code presentation only

## Decision

Retain complete canonical source, full MarkdownIt parsing and Rich/Pygments rendering.
Each AssistantMessage owns a FenceRenderCache across immutable Markdown revisions.
Cache the original Rich Segments only when the authoritative token map and source
prove an explicit top-level fence closing marker. Open, indented and nested/parent-
autoclosed code follows the original renderer. This conservative boundary avoids
reimplementing container deindentation or an incremental parser.

Keys include exact code, normalized lexer/language, resolved Syntax Theme selection
and surface/foreground, relevant ConsoleOptions (including width, height constraints,
wrapping, encoding and legacy mode), color capability and resolved code styles.
Unknown mutable custom theme objects bypass the cache. A new content, language,
width, theme or style misses; returning to an identical presentation may reuse an
entry. Animation age, streaming revision, source position and prose changes are not
keys. Identical fences can share an entry within one message.

The LRU retains at most 32 entries and an estimated 8 MiB per message, including
source, Segments, text and style overhead. Estimates are not an exact RSS guarantee;
oversized output is rendered without retention. Eviction never affects correctness.
Unmount/disposal clears entries; conversation replacement removes the owning widget.
No process-global result cache, parser replacement, background worker or timer is
introduced. Existing current-view cache remains separate. Body cadence is unchanged
at 25ms/40Hz, Measured Ink at 20fps/180ms/12 glyph; scrolling and Runtime are unchanged.

## Validation and limits

Tests compare canonical Segments and syntax styles, closure proof, changed code,
language/theme/width/style/capability misses, bounded LRU, identical/multiple fences,
Diff, disposal, resize and animation on/off. A test-only replay compares the original
Rich fence renderer with production under identical whole-delta tapes; a fixed-work
pass separates reduced rendering work from increased commit throughput. Instrumentation
is scoped to the explicit benchmark and never imported by production.

```bash
uv run pytest tests/test_tui_fence_cache.py -q
uv run python -m tests.visual.markdown_render.benchmark --output /tmp/fence-replay.json
uv run python -m tests.visual.markdown_render.benchmark --fixed-work --output /tmp/fence-fixed.json
```

Cold large fences, growing open code, full Markdown parse and full-message
wrap/layout/compositor costs remain. Snapshot output should stay identical; synthetic
headless timing is not real Provider or Konsole latency. No stable-prefix block cache,
inline token memo, A/B exploration mode or incremental Markdown parser is shipped.
