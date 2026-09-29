# Neuro Code TUI visual baselines

This V0 harness exports deterministic SVG screenshots from the real Textual TUI
widgets. It does not start a Session or Provider and does not make network calls.
Fixtures pin the clock, provider/model labels, workspace path, and trace IDs. The
baselines cover 9 fixtures × 3 viewports × 3 themes = 81 screenshots.
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
