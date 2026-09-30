"""The before/after gallery reads committed V0 snapshots without rewriting them."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

from tests.visual import render_gallery


def test_before_after_gallery_pairs_committed_and_current_snapshots(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    path = snapshot_root / "settings__system__100x32.svg"
    path.write_bytes(b"<svg>after</svg>")
    output = tmp_path / "gallery.html"

    with (
        patch.object(render_gallery, "SNAPSHOT_ROOT", snapshot_root),
        patch.object(render_gallery, "_snapshot_at_ref", return_value=b"<svg>before</svg>") as read,
    ):
        assert render_gallery.render_gallery(output, before_ref="origin/main") == output

    document = output.read_text(encoding="utf-8")
    assert "settings · system · 100x32" in document
    assert "Before (origin/main)" in document
    assert "V1A" in document
    assert base64.b64encode(b"<svg>before</svg>").decode() in document
    assert base64.b64encode(b"<svg>after</svg>").decode() in document
    read.assert_called_once_with("origin/main", path)


def test_v1b_gallery_filters_fixtures_and_labels_new_baselines(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    included = snapshot_root / "mixed-language-long-answer__system__80x24.svg"
    included.write_bytes(b"<svg>mixed</svg>")
    (snapshot_root / "settings__system__80x24.svg").write_bytes(b"<svg>settings</svg>")

    with (
        patch.object(render_gallery, "SNAPSHOT_ROOT", snapshot_root),
        patch.object(render_gallery, "_snapshot_at_ref", return_value=None),
    ):
        output = render_gallery.render_gallery(
            tmp_path / "v1b.html",
            before_ref="baseline",
            after_label="V1B",
            fixtures=frozenset({"mixed-language-long-answer"}),
        )

    document = output.read_text(encoding="utf-8")
    assert "V1B" in document
    assert "mixed-language-long-answer" in document
    assert "Not present in the before baseline" in document
    assert "settings · system" not in document


def test_gallery_keeps_snapshot_bytes_but_allows_browser_cjk_glyph_width() -> None:
    source = b'<svg><text textLength="24.4">\xe4\xb8\xad\xe6\x96\x87</text></svg>'
    embedded = render_gallery._embedded_svg(source, "Chinese fixture")

    encoded = embedded.split("base64,", 1)[1].split('"', 1)[0]
    assert base64.b64decode(encoded) == b"<svg><text>\xe4\xb8\xad\xe6\x96\x87</text></svg>"
    assert b'textLength="24.4"' in source


def test_syntax_gallery_includes_choices_palette_cases_and_prose_comparison(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    included = (
        "syntax-python__graphite__80x24",
        "syntax-choice-dracula__porcelain__100x32",
        "system-palette-unknown__syntax-settings__100x32",
        "long-markdown__system__120x40",
    )
    for name in (*included, "composer-single__graphite__80x24", "settings__system__100x32"):
        (snapshot_root / f"{name}.svg").write_bytes(b"<svg>fixture</svg>")
    with patch.object(render_gallery, "SNAPSHOT_ROOT", snapshot_root):
        output = render_gallery.render_gallery(
            tmp_path / "syntax.html", syntax_only=True, after_label="V2A"
        )
    document = output.read_text()
    for name in included:
        assert name.replace("__", " · ") in document
    assert "composer-single" not in document
    assert "settings · system" not in document
