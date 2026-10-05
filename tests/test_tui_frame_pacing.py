"""Replay audit regressions, not wall-clock performance assertions."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from io import StringIO
from time import perf_counter

import pytest
from rich.text import Text

from neuro_code.interfaces.tui.widgets import WorkloadStatus
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app
from tests.visual.text_arrival.frame_pacing import Profile, load_recording, measure


def test_production_commit_and_animation_contract():
    from neuro_code.interfaces.tui.text_arrival import (
        DURATION_SECONDS,
        FRAME_SECONDS,
        MAX_ACTIVE_GLYPHS,
    )
    from neuro_code.interfaces.tui.widgets import VIEW_COMMIT_SECONDS

    assert VIEW_COMMIT_SECONDS == 0.025
    assert pytest.approx(1 / 24) == FRAME_SECONDS
    assert VIEW_COMMIT_SECONDS != FRAME_SECONDS
    assert DURATION_SECONDS == 0.160
    assert MAX_ACTIVE_GLYPHS == 8


def test_recorded_delta_boundaries_and_timing_are_preserved(tmp_path):
    path = tmp_path / "recording.jsonl"
    items = [{"at_ms": 0, "text": "whole 中文 delta"}, {"at_ms": 51.3, "text": " next"}]
    path.write_text("\n".join(json.dumps(i) for i in items))
    assert load_recording(path) == items
    path.write_text('{"at_ms":1,"text":"a"}\n{"at_ms":0,"text":"b"}')
    with pytest.raises(ValueError, match="monotonic"):
        load_recording(path)
    path.write_text('{"at_ms":-1,"text":"a"}')
    with pytest.raises(ValueError, match="nonnegative"):
        load_recording(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (100, 32), (80, 24)])
async def test_status_content_repaints_without_layout_or_geometry_changes(size):
    from unittest.mock import patch

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=size) as pilot:
        status = app.query_one("#turn-activity", WorkloadStatus)
        status.display = True
        status.update(Text("Existing pulse / Generating 0.1s"))
        await pilot.pause()
        before = {
            name: app.query_one(name).region for name in ["#transcript", "#composer", "#prompt"]
        }
        height = status.size.height
        with patch.object(status, "refresh", wraps=status.refresh) as refresh:
            content = Text("Existing pulse / Generating 0.2s")
            status.update(content)
            assert status.renderable is content
            assert not any(call.kwargs.get("layout") for call in refresh.call_args_list)
        await pilot.pause()
        assert status.size.height == height == 1
        assert before == {name: app.query_one(name).region for name in before}
        status.display = False
        await pilot.pause()
        assert app.query_one("#transcript").region.height > before["#transcript"].height


@pytest.mark.asyncio
@pytest.mark.parametrize("animated", [False, True])
async def test_real_controller_replay_profile_keeps_canonical_and_cleans_clocks(animated):
    recording = [{"at_ms": i * 5, "text": "中文 English 完整 delta。"} for i in range(12)]
    result = await measure("mixed", recording, animated)
    assert result["input_origin"] == "external-recording"
    assert result["followed_tail"]
    assert result["counts"]["view_commit"] < len(recording)
    assert result["counts"]["markdown_parse"] == result["counts"]["view_commit"]
    assert result["canonical_latency_ms"]["max"] is not None
    assert len([e for e in result["trace"] if e["phase"] == "canonical"]) == len(recording)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX native driver audit")
def test_writer_probe_observes_real_file_write_and_flush():
    from textual.drivers._writer_thread import WriterThread

    from tests.visual.text_arrival.native_pacing import MeasuredOutput

    output = StringIO()
    profile = Profile()
    profile.active = True
    profile.start = perf_counter()
    writer = WriterThread(MeasuredOutput(output, profile))
    writer.start()
    writer.write("完整正文")
    writer.write(" next")
    writer.stop()
    assert output.getvalue() == "完整正文 next"
    assert profile.counts["writer_file_write"] == 2
    assert profile.counts["writer_file_flush"] >= 1


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX native driver audit")
async def test_native_replay_uses_original_timing_and_finishes_without_provider(tmp_path):
    from neuro_code.interfaces.tui.widgets import PromptInput
    from tests.visual.text_arrival.native_pacing import NativePacingReplay

    path = tmp_path / "original.jsonl"
    path.write_text('{"at_ms":0,"text":"中文整块"}\n{"at_ms":10,"text":" next delta"}')
    args = Namespace(
        theme="graphite",
        off=True,
        auto_exit=False,
        recording=path,
        case="mixed",
        output=tmp_path / "result.json",
    )
    app = NativePacingReplay(args, Profile())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptInput)
        event = PromptInput.Submitted(prompt, "replay")
        await app.on_prompt_input_submitted(event)
        await app.workers.wait_for_complete()
        assert event._no_default_action
        assert not app._replay_running
        assert app._pending_assistant is None
        assert app._entries[-1].text == "中文整块 next delta"
        result = json.loads(args.output.read_text())
        assert result["input_origin"] == "external-recording"
        assert result["followed_tail"]
