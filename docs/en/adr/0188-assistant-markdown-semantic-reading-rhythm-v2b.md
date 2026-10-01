# ADR 0188: Assistant Markdown Semantic Hierarchy and Reading Rhythm V2B

[简体中文](../../zh-CN/adr/0188-assistant-markdown-semantic-reading-rhythm-v2b.md) · **English**

- Status: Accepted (Variant C selected; production terminal acceptance pending)
- Date: 2026-10-01

## Context

V1B kept headings neutral. Long answers were difficult to scan, while consecutive
prose paragraphs felt dense. Rich already supplies one blank row between independent
paragraphs; adding a row everywhere costs too much at 80×24. A/B/C visual exploration
confirmed a responsive choice: accented headings with extra prose spacing only in
ordinary viewports. Experimental implementations are removed from the final code.

## Decision

The UI semantic Markdown theme owns H1 accent + bold, H2 accent, H3 primary emphasis,
H4 primary, H5 emphasis and H6 secondary. Only H1 is bold unless the model explicitly
requests inline emphasis. Headings have no background or link underline. All UI
palettes resolve the same roles; Syntax Theme selection cannot alter them.

`AssistantMarkdown` registers a paragraph element that adds one `Segment.line()`
only when the current and preceding top-level blocks are paragraphs. Token depth
excludes list items, nested lists and blockquotes. Rich retains the original layout
for headings, lists, fenced/indented code, quotes and tables. Softbreaks and wrapping
remain compact. No response string is edited, and no conversation margin is changed.

The transcript controller supplies the main shell's existing `compact-chrome` state
through a small callback, including when a modal is active. There is no terminal-name
check or second responsive threshold. The current shell marks width <80 or height <28
as compact. Thus 120×40 and 100×32 show two blank paragraph rows; 80×24 keeps Rich's
original single blank row. A standalone renderer defaults to ordinary spacing.

Eligible boundaries are derived once from each parsed source. At rendering time the
current shell policy decides whether to emit each gap. No mutable spacer list or
cumulative padding is retained. Textual reflows the same widget on resize; streaming
reparses the accumulated source through the existing controller. Restoring a viewport
restores the same segment projection. Model text, copy selection, session history,
Syntax Theme, code surfaces/tokens, Composer/input and Runtime remain unchanged.

## Verification and limits

Production tests cover all UI heading roles and Syntax Theme independence, known RGB
accent contrast, CJK/English wrapping, exact one/two-row paragraph gaps, unchanged
non-prose segments, streaming token append and actual message/pending widget resize.
The resize sequence includes 120×40 →80×24 →120×40 and a height-only compact boundary.
No test swaps in a variant renderable or manually refreshes away residual spacers.

The official visual harness promotes eleven source-only reading fixtures covering
Chinese, English, mixed prose, H1–H6, lists/nesting, code, quotes, tables, a long answer
and streaming final state, across three themes and three viewports. Baselines are
updated explicitly, with the existing gallery providing main/production comparison.

These changes guarantee paragraph-boundary policy, not universal streaming stability:
unclosed fences, setext headings and incomplete tables can still trigger Rich's native
reparse. System snapshots represent deterministic ANSI/RGB fixtures, not every real
terminal palette. Konsole visual acceptance remains required before merge. Empty-state
artwork, motion, new Markdown parsers and global line-height are outside this decision.
