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
V2A adds eight syntax/code-prose fixtures and a syntax-settings fixture: the
22-fixture × 3-theme × 3-viewport matrix is 198 screenshots. Fifteen System
palette screenshots and 21 curated selection previews bring the total to 234.
Whitespace-only line-end padding from the SVG serializer is normalized; rendered
text and geometry remain unchanged.
V2A also pins colored output when constructing fixture apps, independent of the
caller's `NO_COLOR`. Earlier committed baselines inherited `NO_COLOR` from the
development shell and were grayscale; all 123 earlier snapshots are explicitly
regenerated with real palette colors. This is a harness correction, not a UI
palette or layout change. Production continues to honor the user's `NO_COLOR`.
The before/after gallery therefore includes this output-filter difference as
well as the new code token colors.

## V2A Syntax Theme review

The syntax fixtures cover Python, Rust, JSON, Shell, Diff, unknown language,
long code, code with surrounding prose/inline code, and the live picker.
All use the existing renderer without sessions/network. Auto and six explicit
syntax choices have preview cards at 100×32 in Graphite, Porcelain, and a
detected dark System palette. Separate System dark/light/unknown fixtures
cover Python, Diff and the picker; CI never probes the developer's terminal.

Update deliberately, then confirm a normal rerun passes:

```bash
NEURO_TUI_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_visual_snapshots.py -q
uv run pytest tests/test_tui_visual_snapshots.py -q
uv run python tests/visual/render_gallery.py --syntax --before-ref 52cb90c7b9242c4ac618b0781c10b41e253ee618 --after-label V2A --output /tmp/neuro-code-tui-v2a-syntax-gallery.html
xdg-open /tmp/neuro-code-tui-v2a-syntax-gallery.html
```

New syntax cards have no V1C counterpart; the existing long-Markdown cards
provide a direct before/after comparison. The gallery is a local artifact,
not a new visual framework or production surface.

Manual acceptance in a real terminal:

- Open `/settings` → Appearance → Syntax Theme. Preview every choice, Save,
  restart, and confirm the same independent choice is restored.
- Switch Graphite/Porcelain/System while retaining a syntax choice. Check
  readable token colors, neutral surrounding prose, unchanged code surface,
  and unchanged Composer/user geometry and draft/cursor.
- Read Python, Rust, JSON, Shell and Diff at 120×40, 100×32 and 80×24; inspect
  comments, strings/numbers, classes/functions, diff added/removed/hunk/context.
- Check unknown-language code and long wrapping. `AGENTS.md` inline code must
  stay foreground-only; no chips or per-token backgrounds.
- Check System on real dark/light palettes and low-color/unknown terminals.
  The terminal fallback retains the saved preference but uses default/ANSI
  token colors. Snapshot ANSI values cannot prove real palette contrast.
- Cancel preview and verify the original syntax choice returns without writes.

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

## V2B Assistant Markdown reading rhythm

Variant C is adopted in production (2026-10-01). The A/B/C experiment renderer,
variant branches and experimental tests are removed. Eleven source-only reading
fixtures are now part of the official harness: Chinese/English/mixed paragraphs,
H1–H6, lists/nesting, prose/code, quotes, tables, long answer and streaming final state.
As of this update, 33 fixtures × 3 themes × 3 viewports give 297 screenshots;
15 System palette cases and 21 syntax-choice cases bring the total to 333.

H1/H2 use UI accent, H1 bold; H3 primary emphasis, H4 primary, H5 emphasis, H6 secondary.
Syntax Theme does not control headings. Consecutive top-level prose paragraphs have
2 blank rows outside compact mode and retain 1 blank row in compact mode. The main
shell's existing responsive state decides; wrapped lines and all other transitions
are unchanged. Production widget tests cover resize and streaming without manually
replacing/refreshing the renderer. Native incomplete-Markdown reparse remains.

```bash
NEURO_TUI_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_visual_snapshots.py -q
uv run pytest tests/test_tui_visual_snapshots.py tests/test_tui_markdown.py -q
uv run python tests/visual/render_gallery.py --before-ref f7dc55cb067ee6ea53504deb5d1dee7780fb522b --after-label V2B --fixtures long-markdown mixed-language-long-answer markdown-chinese-paragraphs markdown-headings markdown-long-answer --output /tmp/neuro-code-tui-v2b-gallery.html
xdg-open /tmp/neuro-code-tui-v2b-gallery.html
```

Final terminal acceptance: compare Graphite/Porcelain/System; scan H1–H6; read long
Chinese/English prose at 120×40 / 100×32 / 80×24; resize between them while a response
streams; verify list/quote/table/code spacing, inline-code foreground-only styling
and Syntax Theme switching. Confirm Composer/input and source-copy behavior stay intact.

## V2C-A Empty State Identity: corrected-source borderless core

The previous source was incorrect. Its terminal rows/masks are removed, and its
logo snapshot changes are withdrawn to main. Lifecycle, centering, resize, draft
geometry and first-content/history gates remain. The user selected the central
six-blade core without the hexagonal border; production rows are in
`empty_state_logo.py`. The official baseline has
351 SVGs (333 existing cards + 18 first-message/restored-history cards).
Empty/draft cards are explicitly updated to the selected core; the A/B/C gallery
remains an exploration record, not three production variants.

Correct source: 212×212 RGBA, SHA256
`8334fe13506ca923f09b16933c6c7e5713346d349ada6eb0675ef97021398a6e`.
The new exploration stores fixed Braille rows: A full badge, B core mark, C core
plus sparse weak outline. Original background is excluded; aspect ratio retained.
Resting uses TEXT_DIM + dim. Activated peak is secondary without dim, only a static
gallery preview; no timer, reveal, input binding or continuous motion is shipped.

```bash
uv run pytest tests/test_tui_empty_identity.py tests/test_tui_visual_snapshots.py -q
uv run python -m tests.visual.empty_identity.render_gallery --output /tmp/neuro-code-v2c-a-corrected-logo
```

The corrected gallery has 162 static state screenshots and 27 resize frames;
formal snapshots are never written by this script. Sizes remain 32×16 / 24×12 /
16×8 at 120×40 / 100×32 / 80×24, hiding below 70×22 or insufficient space.

Optional offline regeneration requires Pillow in a separate system Python (not
an application dependency); it rereads and hashes the source rather than trusting
its filename. CI consumes the checked-in rows, never the desktop PNG:

```bash
python3 tests/visual/empty_identity/convert_reference.py /path/to/neuro-code-logo.png
```

Real Konsole acceptance must check Graphite/Porcelain/System across all sizes;
compare contour recognition, quiet Resting and controlled peak, focus, Chinese
multiline/paste, first send, restored history and large→compact→large resize.
SVG/ANSI cannot prove actual terminal glyph strokes, font width or palette.

## V2C-B — production Empty State Reveal

The selected E Torsion Lock ships 12 immutable Braille frames with the user-revised
non-uniform 6000ms timing. Default snapshots remain static; the reveal harness
freezes the production clock to capture six states, without changing application
behavior: Resting, pre-torsion, max torsion, lock/peak, rebound, returned Resting.
Graphite/Porcelain/System × 120×40/100×32/80×24 adds 54 SVG baselines.
The unchanged static geometry now rests at semantic text-dim 28% alpha + dim;
RGB palettes blend toward canvas, while unknown ANSI defaults retain terminal dim.
Existing empty-state baselines are intentionally updated for this quieter contrast.

```bash
NEURO_TUI_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_empty_reveal_snapshots.py -q
uv run python tests/visual/render_gallery.py --output /tmp/neuro-reveal-gallery.html
uv run python -m tests.visual.empty_reveal.render_preview --output /tmp/neuro-reveal.html
uv run python -m tests.visual.empty_reveal.render_preview --terminal --theme system
```

The HTML player contains actual production Textual frame captures, with Play once,
preview-only Repeat/speed and key frames. The native provider-free preview runs the
real click/timer path. Repeat/speed controls are never production behavior.
Konsole manual checks: no autoplay; first/repeated click; Composer focus and CJK/paste
while playing; Enter cancellation; size-class changes and theme switch; static
NO_COLOR fallback; quiet resting after return. Inspect all three themes and sizes.
Snapshots cannot prove actual Braille font shape, ANSI palette, terminal repaint cost
or real scheduler cadence. See bilingual ADR 0190 for lifecycle/capability rules.
