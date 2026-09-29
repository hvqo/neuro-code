# ADR 0185: TUI Typography and Reading Hierarchy V1B

[简体中文](../../zh-CN/adr/0185-tui-typography-and-reading-hierarchy-v1b.md) · **English**

- Status: Accepted
- Date: 2026-09-30

## Context

V1A established the color and surface contract, but presentation still applies
bold to entire user and error messages, nearly every Markdown heading and list
marker, tool headings, status labels, and technical metadata. These independent
rules compete with assistant prose. A colored or bold path, count, or duration
can appear as important as a finding. The same problem is more noticeable in
long mixed Chinese/English answers, where frequent weight changes interrupt
continuous reading.

## Typography contract

| Role | Treatment |
| --- | --- |
| Primary reading | Assistant prose and user text use regular weight and the V1A primary/body foreground. The user surface, not bold, identifies the speaker. |
| Secondary reading | Tool summaries, status text, quotations, and lower-level headings use regular weight with restrained foregrounds. |
| Metadata | Durations, paths, IDs, counts, hints, and incidental technical tokens use muted or secondary regular text. A technical token is not emphasis by itself. |
| Semantic state | Error, warning, success, and focus retain V1A semantic colors/boundaries; an entire sentence does not become bold merely because it reports a state. |
| Explicit emphasis | Markdown `strong` and the leading document heading may use bold. Focus uses the existing selection/reverse or boundary affordance rather than generic bold. |

Markdown H1 is the sole bold heading and stays on the left reading axis. H2 remains primary and regular; H3 and
deeper headings step down through the existing neutral text roles. Body,
paragraphs, lists, and quote text stay regular. List markers and rules recede;
block quotations do not italicize whole Chinese passages. Inline code has a
restrained foreground only and **never** a filled chip background. Fenced code
and diffs retain their separate surfaces, with syntax colors confined to code
and without blanket bold tokens. This is a hierarchy of reading roles, not a
new palette or font-size system.

Graphite, Porcelain, System, and other themes share these weight and semantic
role decisions; switching themes only resolves the same roles to different
palette values. System keeps its adaptive surfaces and terminal-native focus
fallback. This decision does not change Composer dimensions or behavior,
conversation width, spacing, Header/Footer structure, Runtime, Context, or
Session behavior.

## Validation

The V0/V1A deterministic Textual snapshot harness remains the visual contract.
V1B explicitly updates affected snapshots and adds a mixed Chinese/English
long-answer fixture. Typography tests check rendered semantic styles across
Graphite, Porcelain, and System, including the inline-code no-background rule.
A side-by-side gallery compares committed V1A snapshots to V1B for long
Markdown, user/assistant, tool activity, errors, and mixed-language reading.
Snapshots establish reproducibility; a real terminal is still needed to judge
System's actual ANSI palette and long-form reading comfort.
