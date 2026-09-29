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
terminal palette. V1A should first restore semantic-role contrast and improve
System theme adaptation. V0 records this issue without changing production visuals.

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

## Update snapshots

Only update after intentional visual review. Regenerate the full matrix with:

```bash
NEURO_TUI_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_tui_visual_snapshots.py -q
uv run python tests/visual/render_gallery.py
```

Review the gallery and `git diff --stat` before accepting regenerated files. Run
the normal regression suite again without the environment variable to verify the
new baseline. Snapshot updates are explicit; tests never rewrite them by default.
