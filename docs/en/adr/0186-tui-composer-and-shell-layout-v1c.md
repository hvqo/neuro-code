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
row. The empty prompt surface occupies two rows
at normal widths and one in compact chrome; it grows with visible draft lines.
The prompt editor is capped by the existing eight-line limit and a quarter of
the terminal height, with a two-line minimum budget for short terminals. Longer
drafts scroll within the editor. The transcript takes every row released by a
short draft, remains top anchored in an empty session, and keeps its reading
axis. The status row stays at the bottom and retains its existing data and
tooltips. Attachments, active-turn status, and command hints keep their owned
rows when visible.

Default `Enter` submits the draft. The Send button shares the editor row;
there is no dedicated actions/hint row or Newline button. Keyboard fallbacks
live in F1 / `/help`. The optional user-selected Enter-newline mode remains.

Textual 1.x owns terminal input and Kitty disambiguation on POSIX (`CSI >1u`),
normalizes CSI-u `13;2u` to `shift+enter`, and owns paste, editing and teardown.
`TerminalKeyboardCapability` records only observed normalized modified Enter;
requesting a protocol, a terminal name or an environment variable is not proof
of support. No additional terminal reader, protocol parser or startup wait is
introduced. A distinct `shift+enter` inserts a selection-aware newline. Legacy
CR and SS3 keypad Enter remain Send; Ctrl+J / F2 remain newline fallbacks.

The audited Konsole 25.12.3 default keytab sends Shift+Return as SS3 `ESC O M`,
which Textual maps to keypad Enter. This is not a reliable Shift encoding and
cannot be globally reinterpreted without breaking keypad Enter. That release
has no Kitty keyboard negotiation in its VT emulator. A user may explicitly
map Shift+Return to `\E[13;2u` in a terminal key profile, or use a version and
input path that supports enhanced reporting; Neuro does not change profiles.
Kitty and Ghostty can report CSI-u; WezTerm requires its Kitty protocol option.
Windows Terminal support is version-dependent, and Textual 1.x's Windows driver
does not enable Kitty negotiation: distinct events already delivered are handled,
but unconfirmed paths retain fallback. Help reports observed/unconfirmed capability,
never blanket support based on terminal branding. Multiplexers and terminal shortcuts
can also alter the input path. Real-terminal acceptance remains necessary.

Sources: [Konsole 25.12.3 default keytab](https://github.com/KDE/konsole/blob/v25.12.3/data/keyboard-layouts/default.keytab),
[Kitty protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/),
[WezTerm option](https://wezterm.org/config/lua/config/enable_kitty_keyboard.html).

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
