# ADR 0193: Stable streaming code and one final syntax transition

**English** · [简体中文](../../zh-CN/adr/0193-streaming-code-final-highlight.md)

- Date: 2026-10-04
- Status: Accepted; awaiting Konsole review
- Scope: Assistant fenced-code presentation

## Problem and evidence

Main re-lexes growing code on each view commit. Incomplete strings and comments
can be classified differently when later text arrives, recoloring existing glyphs.
The previous local experiment also mistook an EOF closing candidate for a stable
close: `code\n` → `code\n` plus three backticks → an appended `oops` produced
PLAIN → SYNTAX → PLAIN. MarkdownIt was correct to revise its EOF interpretation;
the presentation lifecycle incorrectly treated a provisional parse as final.

## Decision

Each AssistantMessage owns one `FencePresentation` metadata registry beside its
existing `FenceRenderCache`. Identity is the message generation plus the opener's
normalized source offset. Parser object IDs bind only the current parse; they are
never persisted as cross-revision identities. Replacing canonical source resets
the generation. Restored messages start complete; disposal clears both owners.

For parser-mapped, source-confirmed top-level fences:

```text
ACTIVE (stable plain code) → FINALIZED (full syntax) → CACHED (same syntax)
```

Streaming finalization requires an explicit closing line terminated by a newline.
An unterminated EOF closing candidate stays ACTIVE because the next delta can
invalidate it. Response completion finalizes an EOF fence even without an explicit
close, without adding a marker or altering model text. Finalization is monotonic
and recorded once per identity. Cache misses, eviction, theme and width changes
never reset this lifecycle. A parse captures its foreground decision before it is
installed, so the old view cannot change its foreground while the new view is built.

ACTIVE uses the existing Rich Syntax surface, padding and wrapping with the theme's
plain Text style, bypassing Pygments. Finalized code uses the normal Rich/Pygments
renderer. Unknown/nested/indented or unmapped structures keep the existing renderer.
Full Markdown parsing remains authoritative. There is no new parser, rendered-result
cache, timer, Runtime event, animation algorithm or stream pacing setting.

The existing message-local LRU retains 32 entries / estimated 8 MiB. Render keys
retain code, language, Syntax Theme, width, styles and color capability. Height/max-
height are excluded: entries are pre-crop segments, and measure/paint height budgets
must not cause duplicate syntax work. Normal eviction and changed render conditions
may require another render, but never return a finalized block to plain mode.

## Verification and terminal acceptance

Tests replay every delta for Python, Rust, TypeScript, JSON, shell and Diff, including
multiline strings/comments, incomplete strings, invalidated EOF closing candidates,
multiple fences and prose tails. They assert stable ACTIVE foreground, one finalization,
static Rich/Pygments equivalence, unchanged text/background/cells/height, lifecycle
isolation, resize/theme/Syntax Theme changes, eviction and cancel/error cleanup.
The dedicated benchmark compares identical synthetic tapes with main rendering;
headless CPU/commit measurements do not claim real Konsole scanout performance.

Run the actual Neuro Code TUI in Konsole; Enter starts and Ctrl+C exits after review:

```bash
uv run python -m tests.visual.code_flicker.replay --mode stable --speed slow --theme system
uv run python -m tests.visual.code_flicker.replay --mode stable --speed normal --theme system
uv run python -m tests.visual.code_flicker.replay --mode baseline --speed normal --theme system
uv run python -m tests.visual.code_flicker.benchmark --output /tmp/neuro-code-flicker-benchmark.json
```

The terminal replay emits view-commit lifecycle records to
`/tmp/neuro-code-fence-replay.json`. The 200-line Python block stays plain while
growing, makes one foreground transition on closure, and remains syntax-styled
while following Markdown arrives. Main and stable modes use the same delta timing.
EOF candidates without newline can remain plain until response completion. Final
highlighting can still produce one cold render; full Markdown/layout costs remain.
