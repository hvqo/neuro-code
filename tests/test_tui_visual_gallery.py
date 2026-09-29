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
