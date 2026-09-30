# ADR 0186: TUI Composer and Shell Layout V1C

[简体中文](../../zh-CN/adr/0186-tui-composer-and-shell-layout-v1c.md) · **English**

- Status: Accepted
- Date: 2026-09-30

## Context

After V1A and V1B, the 120×40 and 100×32 empty Composer still reserves nine
rows for a one-line draft; the Header reserves three. A minimum six-row prompt
surface, a gap above its actions, a gap above the runtime bar, and outer bottom
padding consume reading space even though `PromptInput` already measures its
wrapped document. At 80×24 the shorter five-row Composer remains a large
fraction of the available screen. The empty conversation makes this allocation
especially visible.

## Decision

### Layout contract

The visible hierarchy is conversation content, Composer, session metadata,
then branding. The Header and bottom runtime bar each occupy one row. Model,
effort, mode, context usage, and workspace have one owner in the bottom bar;
a new session starts with an empty transcript instead of a duplicate Ready
banner. A resumed session keeps only its session identity notice in the
transcript. The brand is quiet and does not move when the draft grows.

The prompt text and bottom metadata share a reading axis. The Header is at
most one cell away from it, including compact chrome. Long conversation and
tool activity scroll within the transcript rather than enlarging the shell.
A permission modal overlays the shell without reallocating its rows. Theme
switches change the palette, not these geometry rules.

### Composer and keyboard

The Composer uses a bounded prompt surface plus a single bottom runtime-status
row. The empty prompt surface occupies three rows
at normal widths and two in compact chrome; it grows with visible draft lines.
The prompt editor is capped by the existing eight-line limit and a quarter of
the terminal height, with a two-line minimum budget for short terminals. Longer
drafts scroll within the editor. The transcript takes every row released by a
short draft, remains top anchored in an empty session, and keeps its reading
axis. The status row stays at the bottom and retains its existing data and
tooltips. Attachments, active-turn status, and command hints keep their owned
rows when visible.

Default `Enter` submits the draft. `Shift+Enter` inserts a newline when the
terminal forwards a distinct modified-key event; `Ctrl+J`, `F2`, and the
focusable Newline button remain available when it does not. The always-visible
hint lists the reliable fallback; the Newline button tooltip explains
conditional `Shift+Enter`. The optional user-selected Enter-newline mode remains
intact. The compact shortcut hint
stays visible while editing and shares the existing action row, so it adds no
height or focus-dependent movement. The tooltip does not claim that every
terminal distinguishes `Shift+Enter` from `Enter`.

This changes shell geometry and prompt guidance only. V1A colors and adaptive
System surfaces, V1B typography, permission behavior, runtime status values,
and durable session history keep their existing owners.

## Validation

Deterministic Textual screenshots cover Graphite, Porcelain, and System at
120×40, 100×32, and 80×24, including focused/idle single-line and long draft
states. Geometry tests assert the reading-area allocation, reading axis,
bottom status placement, modal fit, theme-switch stability, and draft
growth/shrinkage; keyboard tests check newline versus submit. A real terminal
still needs a manual `Shift+Enter`
check because terminal key reporting is outside Textual's control. The V1B
snapshots remain available as the committed before baseline for a V1C gallery.
