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
`TerminalInputNormalizer` applies a fixed order: a native modified-key event,
then a version-scoped rule from `TerminalInputCompatibilityRegistry`, then the
Ctrl+J / F2 legacy fallback. It always returns one action: `SEND`, `NEWLINE`, or
`PASS_THROUGH`; the last leaves ordinary TextArea input and editing to Textual.
Other Enter events keep the existing submit behavior. `TerminalKeyboardCapability`
continues to record only an observed native modified Enter; it is not inferred
from terminal branding. The
`KONSOLE_VERSION` marker identifies only this versioned compatibility rule, not
general enhanced-key support.

Konsole 25.12.x is a verified compatibility rule. Konsole 25.12.3's default
keytab emits SS3 `ESC O M` for Shift+Return. Textual normalizes this to
`key="enter", character=None`, while ordinary Return is
`key="enter", character="\\r"`. The registry maps only the no-character Enter
or keypad-enter event to Newline for the verified Konsole 25.12 release range.
The ordinary Return event still submits. Konsole exports a dedicated numeric
`KONSOLE_VERSION` to its child session; the resolver accepts only its strict
six-digit encoding for 25.12.0 through 25.12.x. It does not infer identity from
`$TERM`. Missing/malformed markers, and known multiplexer or SSH environments,
fail closed. New terminal rules can be added to the registry without adding
terminal-name branches to `PromptInput`.

Each registry rule is reviewable data: terminal family, inclusive minimum and
exclusive maximum version, observed Textual key/character, observed wire
sequence, resulting action, known tradeoff, evidence links, and regression-test
references. The raw sequence is evidence for the rule; Textual remains the one
runtime parser, and the normalizer matches its normalized event.

The SS3 sequence does not encode whether the physical key was Shift+Return or
keypad Enter. On the matched Konsole release, both therefore insert a newline;
the application cannot distinguish them. Help discloses this tradeoff. This
rule does not change terminal profiles or keytabs. Native CSI-u `shift+enter`
still takes precedence; unrecognized paths keep Enter as Send and Ctrl+J / F2
as Help-only fallbacks. A real Konsole test remains necessary.

Sources: [Konsole version environment export](https://github.com/KDE/konsole/blob/v25.12.3/src/session/SessionManager.cpp#L1254-L1286),
[Konsole 25.12.3 default keytab](https://github.com/KDE/konsole/blob/v25.12.3/data/keyboard-layouts/default.keytab),
[Textual 1.0.0 input parser](https://github.com/Textualize/textual/blob/v1.0.0/src/textual/_xterm_parser.py),
[Kitty protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/).

### Application-side negotiation feasibility

The 2026-09-30 audit used installed Textual **1.0.0** and Konsole **25.12.3**.
Textual's POSIX `LinuxDriver.start_application_mode()` already pushes Kitty
disambiguation (`CSI >1u`) before starting its input thread. It pops the mode
(`CSI <u`) before leaving the alternate screen. Focus alone is not a keyboard
capability response, and pushing again on every focus would unbalance the stack.
The application therefore reuses the driver lifecycle, rather than adding a
competing reader or sending additional protocol modes.

To check the actual installed Konsole path, an offscreen Qt harness invoked
`Vt102Emulation` in `libkonsoleprivate.so.25.12.3`, with UTF-8, reset terminal
state and the default key translator. It fed application escape sequences to
`receiveData()`, injected Qt Return/Shift+Return events into `sendKeyEvent()`,
and captured `sendData()`. This exercises the real emulator and key translator;
it is not a GUI/physical-key acceptance test or a fake transport.

| Probe | Observed reply / key output |
| --- | --- |
| Device attributes `CSI c` (positive control) | `CSI ?62;1;4c` |
| Focus reporting enabled, then focus gained (positive control) | `CSI I` |
| Kitty query `CSI ?u`, before/after `CSI >1u` | No reply |
| modifyOtherKeys query `CSI ?4m`, before/after `CSI >4;2m` | No reply |
| Return, before/after either enable request | CR (`0d`) |
| Shift+Return, before/after either request or focus | SS3 `ESC O M` (`1b4f4d`) |

The matching [VT emulator source](https://github.com/KDE/konsole/blob/v25.12.3/src/Vt102Emulation.cpp)
has no Kitty keyboard flags or modifyOtherKeys handler; key dispatch still uses
the keytab. [modifyOtherKeys is an xterm protocol](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html),
not a universal capability. These requests cannot recover the missing Shift
modifier in this Konsole release. This conclusion is version-specific, not an
inference from `$TERM` and not a claim about future Konsole releases.

The terminal input compatibility layer does not add a terminal reader, protocol
negotiation request, startup timeout, focus probe, profile mutation, or
IME/paste interception. The current Windows driver does not negotiate Kitty,
and a terminal or multiplexer advertising support is insufficient unless the
active driver/parser actually delivers the distinct event. Extra Kitty flags
for release events, alternate keys or associated text are outside this
parser's contract and are not enabled speculatively.

This changes shell geometry and prompt guidance only. V1A colors and adaptive
System surfaces, V1B typography, permission behavior, runtime status values,
and durable session history keep their existing owners.

## Validation

Deterministic Textual screenshots cover Graphite, Porcelain, and System at
120×40, 100×32, and 80×24, including focused/idle single-line and long draft
states. Geometry tests assert the reading-area allocation, reading axis,
bottom status placement, modal fit, theme-switch stability, and draft
growth/shrinkage. Normalizer and PromptInput tests cover ordinary Enter,
enhanced Shift+Enter, exact Konsole version bounds, unrecognized terminal
fallback, registry extension, and Ctrl+J/F2. Real-driver PTY regressions verify
protocol push/pop and teardown, Konsole SS3 normalization followed by ordinary
submission, native modified Enter, and bracketed multiline Chinese paste.
Manual Konsole acceptance must additionally confirm the physical keypad Enter
tradeoff and preserve IME, history, selection, and layout behavior.
