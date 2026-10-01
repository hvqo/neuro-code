# ruff: noqa: RUF001
"""Generate the corrected-reference gallery; never update official snapshots."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
from pathlib import Path
from unittest.mock import patch

from tests.test_tui_visual_snapshots import canonicalize_svg
from tests.visual.empty_identity.exploration import (
    ASSETS,
    THEMES,
    VARIANTS,
    VIEWPORTS,
    IdentityExplorationApp,
)
from tests.visual.render_gallery import _embedded_svg
from textual.pilot import Pilot
from textual.widgets import Static

from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.widgets import PromptInput

STATES = (
    "fresh-resting",
    "activated-peak",
    "returned-resting",
    "focused-composer",
    "first-message",
    "resumed-history",
)


def fixed_clock(app: NeuroCodeApp) -> None:
    app.query_one("#clock", Static).update("13:37")


async def settle(pilot: Pilot[IdentityExplorationApp]) -> None:
    await pilot.pause()
    await pilot.pause()


async def render_gallery(output: Path) -> Path:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    metrics = []
    frames: dict[tuple[str, str, str], list[str]] = {}

    def record(app: IdentityExplorationApp, state: str) -> None:
        width, height = app.size
        theme = app.identity_theme.value
        viewport = f"{width}x{height}"
        name = f"{state}__{theme}__{viewport}__{app.variant}"
        svg = canonicalize_svg(app.export_screenshot(title="Neuro Code corrected Logo exploration"))
        (output / f"{name}.svg").write_text(svg, encoding="utf-8")
        geometry = app.geometry()
        geometry["state"] = state
        metrics.append(geometry)
        frames.setdefault((state, theme, viewport), []).append(
            f"<section><h3>Variant {app.variant}</h3>"
            + f'<a href="{name}.svg" target="_blank">'
            + _embedded_svg(svg.encode(), html.escape(name))
            + "</a><pre>"
            + html.escape(json.dumps(geometry, ensure_ascii=False, indent=2))
            + "</pre></section>"
        )

    environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(NeuroCodeApp, "_update_clock", fixed_clock),
    ):
        for theme in THEMES:
            for viewport in VIEWPORTS:
                for state in STATES:
                    for variant in VARIANTS:
                        app = IdentityExplorationApp(variant, theme)
                        async with app.run_test(size=viewport) as pilot:
                            if state != "focused-composer":
                                app.query_one("#prompt", PromptInput).blur()
                            if state == "first-message":
                                app._write_entry(
                                    "user", "Review this repository. / 请审查这个项目。"
                                )
                            elif state == "resumed-history":
                                app._replace_transcript(
                                    (
                                        Message(Role.USER, "请继续之前的审查。"),
                                        Message(
                                            Role.ASSISTANT,
                                            "Restored conversation evidence is visible.",
                                        ),
                                    )
                                )
                            elif state == "activated-peak":
                                app.set_peak_preview(True)
                            elif state == "returned-resting":
                                app.set_peak_preview(True)
                                await settle(pilot)
                                app.set_peak_preview(False)
                            await settle(pilot)
                            record(app, state)
            for variant in VARIANTS:
                app = IdentityExplorationApp(variant, theme)
                async with app.run_test(size=VIEWPORTS[0]) as pilot:
                    for index, viewport in enumerate((VIEWPORTS[0], VIEWPORTS[2], VIEWPORTS[0])):
                        await pilot.resize_terminal(*viewport)
                        await settle(pilot)
                        record(app, f"resize-{index + 1}")

    (output / "geometry.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    cards = []
    for (state, theme, viewport), panels in frames.items():
        cards.append(
            f'<article data-theme="{theme}" data-viewport="{viewport}" data-state="{state}">'
            f"<h2>{html.escape(state)} · {theme} · {viewport}</h2>"
            f'<div class="trio">{"".join(panels)}</div></article>'
        )
    content = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>Neuro Code corrected Logo A/B/C</title><style>
body{background:#17191c;color:#e0e2e5;font:15px system-ui;margin:24px}
h1,h2,h3{font-weight:500}p{max-width:1000px;line-height:1.6;color:#adb3bc}
.filters{position:sticky;top:0;background:#17191c;padding:12px 0;z-index:1}
select{margin-right:12px;background:#252930;color:#eee;padding:8px;border:1px solid #555}
.trio{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
section{background:#21252a;padding:12px;border-radius:6px}img{width:100%;height:auto}
pre{font-size:11px;white-space:pre-wrap;color:#a6acb5}article{margin:32px 0}
[hidden]{display:none}@media(max-width:1000px){.trio{grid-template-columns:1fr}}
</style><h1>Corrected Logo · A / B / C</h1>
<p>A：完整六边形轮廓和中央六片结构；B：仅中央结构；C：中央结构 + 稀疏弱外框。
原图实心背景已排除，所有尺寸保持原图比例。Resting 使用现有 TEXT_DIM + dim；
Activated peak 使用现有 secondary，仅为静态预览，无动画、按键或持续动效。
用户已选 B 中央结构，去掉六边形外框；A/C 仅保留为探索记录。System 为确定性 ANSI 表示，不能代替真实 Konsole 字体与 palette 验收。</p>"""
    content += f"<p>新 source SHA256：<code>{ASSETS['source_sha256']}</code> · 212×212 RGBA</p>"
    content += '<div class="filters">'
    for key, values in (
        ("theme", [theme.value for theme in THEMES]),
        ("viewport", [f"{w}x{h}" for w, h in VIEWPORTS]),
        ("state", [*STATES, "resize-1", "resize-2", "resize-3"]),
    ):
        content += f'<select id="{key}"><option value="">All {key}</option>'
        content += "".join(f"<option>{value}</option>" for value in values) + "</select>"
    content += "</div>" + "".join(cards)
    content += """<script>for(const s of document.querySelectorAll('select'))s.onchange=()=>{
for(const c of document.querySelectorAll('article'))c.hidden=['theme','viewport','state'].some(k=>{
const v=document.getElementById(k).value;return v&&c.dataset[k]!==v})}</script></html>"""
    path = output / "index.html"
    path.write_text(content, encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(asyncio.run(render_gallery(args.output)))
