# Neuro Code TUI visual baselines

This V0 harness exports deterministic SVG screenshots from the real Textual TUI
widgets. It does not start a Session or Provider and does not make network calls.
Fixtures pin the clock, provider/model labels, workspace path, and trace IDs. The
V0 baselines covered 9 fixtures × 3 viewports × 3 themes = 81 screenshots.
V1B adds a mixed Chinese/English long answer for a 10-fixture, 90-screenshot
matrix, plus the six deterministic System terminal-palette screenshots.
V1C adds focused single-line, idle single-line, and long Composer fixtures for
a 13-fixture, 117-screenshot matrix, plus the same six System palette
screenshots (123 total).
Whitespace-only line-end padding from the SVG serializer is normalized; rendered
text and geometry remain unchanged.

## Known V0 baseline limitation

The snapshots expose a System theme contrast defect: surface, selection, border,
muted text, and Composer roles collapse to the same or similar ANSI
`bright_black`. This is the pre-redesign baseline, not visual-quality approval.
System snapshots show Textual's deterministic ANSI rendering, not every user's
terminal palette. V0 recorded this issue without changing production visuals;
V1A changes the color mapping and explicitly regenerates all 81 baselines.
System role tests protect against broad fills that assume an ANSI color has a
particular brightness, but cannot promise a WCAG ratio for an unknown terminal
palette.

## Real-terminal manual check

System palette screenshots inject three deterministic inputs: dark RGB,
light RGB, and unknown/ANSI fallback. They never issue terminal queries in CI.
At interactive startup Neuro Code makes one optional OSC 10/11 query with a
60 ms deadline before Textual starts reading input; reliable RGB plus
TrueColor/ANSI256 enables derived surfaces, while unsupported/low-capability
terminals use default fills, visible ANSI rules, dim text, and ANSI focus.
The SVG snapshots still cannot reproduce the active Konsole palette, so use
the actual terminal after selecting `/settings` → Appearance → **System**:

1. Inspect the canvas, Composer, a historical User Message, Settings, Permission
   approval, Tool Activity, and an Error. Confirm ordinary panels do not become
   broad gray fills, including when the terminal profile maps bright black to a
   light gray.
2. Inspect an assistant message containing inline code such as `AGENTS.md`, a
   repository path, and a command. Confirm each remains inline text without a
   filled chip background. Check a fenced code block separately; it may retain
   its independent code treatment.
3. Move keyboard focus through Settings and a Permission choice, then select
   text in the Composer. Confirm focus, selection, borders, and dim hints remain
   distinguishable against the terminal's own default foreground/background.
4. If available, repeat with both a dark and a light Konsole color scheme. Take
   screenshots from the actual terminal; do not treat the browser gallery as a
   simulation of either palette. Confirm the adaptive Composer surface remains
   easy to locate and keyboard focus remains clear without adding a top rule in
   either state; Composer contents should not move.

## Run the regression suite

```bash
uv run pytest tests/test_tui_visual_snapshots.py -q
```

The test compares the serialized Textual screen exactly with
`tests/visual/snapshots/`. On mismatch, it writes the received SVG under
`/tmp/neuro-code-tui-visual-received/` and leaves the approved baseline untouched.

## View the baseline

Generate a standalone HTML gallery that embeds every SVG:

```bash
uv run python tests/visual/render_gallery.py
```

The script prints its output path, normally
`/tmp/neuro-code-tui-visual-gallery.html`. Open that file in a browser to compare
fixture, theme, and viewport labels. To select a different output path:

```bash
uv run python tests/visual/render_gallery.py --output /tmp/neuro-code-tui-visual-gallery.html
```

For side-by-side V0 versus V1A review, read the committed V0 snapshots directly
from the last V0 `main` commit. This does not create a worktree or modify either
baseline:

```bash
uv run python tests/visual/render_gallery.py --before-ref fe191e4b2ddd947d90e588456f7b74fbaa95a7b0 --output /tmp/neuro-code-tui-v1a-compare.html
```

Open `/tmp/neuro-code-tui-v1a-compare.html` in a browser. Each card contains
the same fixture, theme, and viewport before and after. For System, compare
semantic structure in this renderer and also inspect your actual terminal;
the browser SVG is not a simulation of your terminal's ANSI palette. The six
`system-palette-*` cards are V1A-only deterministic dark/light/fallback
samples because V0 had no equivalent palette fixtures.

For focused V1A-before/V1B-after review, the following command reads the
committed V1A snapshots from the V1B branch point. It includes the five
reading scenarios; the mixed-language fixture is new and shows an explicit
missing-before label. No baseline or worktree is modified:

```bash
uv run python tests/visual/render_gallery.py \
  --before-ref 743022d5dede4227cdabaf0daf6ac7472ce50db1 \
  --after-label V1B \
  --fixtures long-markdown user-assistant tool-activity error mixed-language-long-answer \
  --output /tmp/neuro-code-tui-v1b-reading-compare.html
```

Open `/tmp/neuro-code-tui-v1b-reading-compare.html` in a browser. Compare
regular prose, H1/H2/H3, lists, quotes, inline and fenced code, tool metadata,
and error text at all three viewports and themes. A real-terminal reading pass
is still needed for System ANSI palettes and long Chinese/English paragraphs.
The gallery removes Rich SVG's fixed `textLength` from embedded images so
browser CJK fallback glyphs do not overlap; committed snapshot bytes and their
exact regression comparison remain unchanged.

For V1B-before/V1C-after shell review, run:

```bash
uv run python tests/visual/render_gallery.py \
  --before-ref 3a9abd3cc8a60d38e6ff4ec0872df2d52f52f8ff \
  --after-label V1C \
  --fixtures empty-conversation single-line-composer idle-composer multiline-composer long-composer long-markdown tool-activity permission \
  --output /tmp/neuro-code-tui-v1c-shell-compare.html
```

Open `/tmp/neuro-code-tui-v1c-shell-compare.html` to compare Header, reading
area, Composer, and bottom status at 120×40, 100×32, and 80×24. The three new
Composer fixtures have no V1B cards. In a real terminal,
type a short draft, several lines, and a draft longer than the editor cap;
check the surface remains locatable and the status stays at the bottom. Confirm
`Enter` sends, `Shift+Enter` inserts a newline if the terminal reports it as a
distinct key, and `Ctrl+J` / `F2` provide a usable fallback listed in F1 / `/help`.
The Send button shares the prompt row; shortcut hints never reserve a row.
Help distinguishes observed native modified Enter from an unconfirmed path.
Konsole 25.12.x is recognized only by its dedicated, strict `KONSOLE_VERSION`
marker. Its no-character SS3 Enter maps to newline, so both Shift+Return and a
physical keypad Enter insert a newline on that verified release range; the
wire encoding cannot distinguish them. Unknown, remote, and known multiplexed
paths fail closed: plain Enter sends and Ctrl+J / F2 remain available in Help.
Neuro Code does not change terminal profiles or keytabs. Test a dark and light
System terminal palette as well as one RGB theme; SVG screenshots alone cannot
verify terminal key reporting.

### Keyboard feasibility and real-driver checks

Textual 1.0.0's POSIX driver already sends Kitty `CSI >1u` at startup and
`CSI <u` on exit. More startup/focus requests cannot add an unsupported protocol
to a terminal. The installed Konsole 25.12.3 emulator was tested directly via
its native library: Kitty and modifyOtherKeys queries received no reply; enable
requests left Shift+Return as `ESC O M`. Device-attribute and focus responses
provided positive controls. Konsole also exports `KONSOLE_VERSION`; the
compatibility registry uses this dedicated version marker rather than `$TERM`.
See [ADR 0186](../../docs/en/adr/0186-tui-composer-and-shell-layout-v1c.md#application-side-negotiation-feasibility)
for source references, version boundaries and the exact probe matrix.

Run the provider-free real POSIX driver regression with:

```bash
uv run pytest tests/test_tui_terminal_keyboard.py -k real_driver -q
```

It uses a PTY, the production PromptInput and Textual LinuxDriver (not
`run_test`'s headless driver). It checks startup/teardown, real input delivery,
unknown-terminal SS3 Send, exact Konsole-marker SS3 newline followed by ordinary
Enter Send, enhanced Shift+Enter and bracketed multiline Chinese paste. PTY
injection proves the application normalization path, not the physical key
encoding. For manual acceptance on Konsole 25.12.x, open F1 / `/help` and
confirm the compatibility warning. Press Shift+Return mid-draft and confirm a
newline; press plain Return and confirm it submits; press physical keypad Enter
and confirm it inserts a newline under the documented quirk. Also test paste,
selection replacement, history restore and an active IME. Ctrl+J / F2 remain
Help-only fallbacks, and Neuro Code never edits the user's profile.

To update only those six palette samples after an intentional visual change:

```bash
NEURO_TUI_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_visual_snapshots.py -k system_terminal_palette -q
uv run python tests/visual/render_gallery.py --before-ref fe191e4b2ddd947d90e588456f7b74fbaa95a7b0 --output /tmp/neuro-code-tui-v1a-compare.html
```

## Update snapshots

Only update after intentional visual review. Regenerate the full matrix with:

```bash
NEURO_TUI_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_visual_snapshots.py -q
uv run python tests/visual/render_gallery.py
```

Review the gallery and `git diff --stat` before accepting regenerated files. Run
the normal regression suite again without the environment variable to verify the
new baseline. Snapshot updates are explicit; tests never rewrite them by default.
