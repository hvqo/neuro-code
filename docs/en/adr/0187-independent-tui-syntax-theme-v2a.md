# ADR 0187: Independent TUI Syntax Theme V2A

[简体中文](../../zh-CN/adr/0187-independent-tui-syntax-theme-v2a.md) · **English**

- Status: Accepted (architecture; visual acceptance pending)
- Date: 2026-10-01

## Context

Rich Markdown already delegates fenced-code lexing to Pygments. Neuro's old
`_MonochromePygmentsStyle` and `_PaletteSyntaxTheme` translated token colors
through `UiTheme`, so UI accent/status changes also changed code colors. There
was no independent syntax preference or code preview. Inline code's existing
foreground-only contract and the V1A/V1B/V1C UI contracts must remain stable.

## Decision

Persist a shared `SyntaxTheme` identifier separately from `UiTheme`. The
presentation-owned registry uses installed Pygments styles: GitHub Dark, One
Dark, Monokai, Dracula, Friendly Light and Solarized Light, plus Auto. These
provide familiar dark and light options without copied palette tables or a new
lexer/dependency. The registry is a fixed allowlist, not a plugin/theme editor.

Rendering follows fenced language → existing Pygments lexer → semantic token →
independent syntax resolver → Rich Syntax. CommonMark fence metadata is stripped
from the language identifier; unknown languages remain ordinary code. Python,
Rust, JSON, Shell and Diff have deterministic fixtures. Keyword/type, strings,
numbers, comments, names/functions/classes/builtins/decorators, operators and
punctuation retain the lexer's semantics. Diff insertion, deletion, header and
hunk roles use the selected style; if an upstream diff role equals context,
reuse a distinguishable semantic foreground from that same style.

The UI remains the sole owner of the code block's surface and surrounding
padding. Token styles have no background fills. Syntax themes cannot change
prose, inline code, tool/status UI, Composer, keyboard handling or shell geometry.
Code remains regular weight; curated comment italics may remain. The existing
Rich code-block padding, wrapping, selection and message source text are retained.

Auto chooses GitHub Dark for code-surface RGB luminance below 0.179, otherwise
Friendly Light. This is the light/dark contrast crossover, not terminal-name
detection. Explicit selections remain stored unchanged when the UI changes.
Token foregrounds keep upstream colors when contrast against the actual
UI-owned RGB surface is ≥4.5:1; otherwise a bounded blend toward white/black
keeps hue while making the token readable. No RGB claim is made for unknown ANSI
palettes. System reuses V1A's existing terminal palette resolution: reliable
TrueColor/ANSI256 surfaces enable the RGB path; unknown/ANSI16 surfaces use
default foreground with limited ANSI token colors and dim comments. The saved
explicit choice is retained and the preview identifies the fallback. Terminal
palette behavior is not changed or probed again.

Appearance Settings exposes a separate Syntax Theme entry. A Select and a
representative Python preview update immediately, including existing and
streaming fenced blocks. Save persists, Cancel restores the opening selection;
neither preview nor theme switching changes durable conversation, draft or
cursor. Save failure keeps the visible selection and reports an error, matching
the existing UI-theme contract. The atomic preferences port owns the write.

The optional `syntax_theme` JSON field remains schema version 1. Missing,
invalid or unreadable values resolve to Auto; other preference writes preserve
the syntax choice and vice versa. Startup reloads it independently of `theme`.

## Verification and limits

Tests cover real lexer/source preservation, token contrast and foreground-only
styles, diff roles, System capability fallback, preference migration/restart,
live preview/cancel/save/failure and geometry across three UI themes/viewports.
The visual harness adds eight code/prose fixtures and a syntax settings fixture
at 120×40, 100×32 and 80×24, explicit selection previews, and injected
dark/light/unknown System palettes. The gallery supports focused V1C/V2A review.
Fixture construction removes ambient `NO_COLOR` only within the harness so
screenshots preserve actual token colors. Earlier baselines inherited a grayscale
filter; their explicit regeneration corrects the harness without changing UI
palettes or production `NO_COLOR` behavior. A rendered-SVG regression verifies
environment independence and actual keyword/string foregrounds.

Snapshot RGB/ANSI output cannot prove every real terminal palette. ANSI fallback
uses terminal colors whose contrast needs manual inspection. Mature lexer
categories do not guarantee semantic analysis: for example a Python use-site
identifier may be `Name`, not `Name.Function`. V2A performs highlighting, not
code interpretation. Custom theme editing and further visual redesign are deferred.
