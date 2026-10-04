"""Source lifecycle survives rendering stalls and geometry-only invalidation."""

from __future__ import annotations

import pytest
from rich.segment import Segment
from textual import events
from textual.geometry import Region, Size

from neuro_code.interfaces.tui import widgets
from neuro_code.interfaces.tui.text_arrival import ARRIVAL_META, SOURCE_WINDOW, ArrivalTimeline
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, AssistantMessage
from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui_text_arrival import begin, signature, wait_for_committed_view
from tests.test_tui_text_arrival import clock as clock
from tests.visual.showcases import make_app


@pytest.mark.parametrize("stall", [0, 0.05, 0.15, 0.3, 0.4, 0.5])
def test_pending_does_not_expire_before_first_presentation(stall):
    timeline = ArrivalTimeline()
    timeline.receive(0, 1, 10)
    timeline.prune(10 + stall)
    assert timeline.arrivals
    assert timeline.age((0, 1), 10 + stall) is None
    assert timeline.start_visual((0, 1), 10 + stall) == 0
    assert timeline.age((0, 1), 10 + stall + 0.05) == pytest.approx(0.05)
    assert timeline.start_visual((0, 1), 10 + stall + 0.2) is None
    assert timeline.births[0] == 10 + stall


def test_pending_bound_and_settled_tombstones_are_generation_local():
    timeline = ArrivalTimeline()
    for n in range(1000):
        timeline.receive(n, n + 1, n)
        assert timeline.start_visual((n, n + 1), n + 1) == 0
    timeline.prune(10000)
    assert len(timeline.arrivals) <= 256
    assert len(timeline.births) <= SOURCE_WINDOW
    assert timeline.start_visual((999, 1000), 10000) is None
    assert not timeline.eligible((0, 1), 10000)
    assert not timeline.has_active(10000)
    timeline.clear()
    timeline.receive(0, 1, 10001)
    assert timeline.start_visual((0, 1), 10002) == 0


@pytest.mark.asyncio
async def test_settle_does_not_promote_older_pending_glyphs_into_a_new_tail(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("Earlier paragraph 旧段落\n\nNewest paragraph 新段落")
        await pilot.pause()
        births = dict(pending._arrival.births)
        assert births
        clock[0] += 0.2
        pending._arrival_tick()
        pending.render_lines(Region(0, 0, pending.size.width, pending.size.height))
        assert pending._arrival.births == births
        assert pending._arrival_timer is None


@pytest.mark.asyncio
async def test_commit_readiness_drains_callbacks_queued_at_end_of_pilot_pause(clock, monkeypatch):
    from neuro_code.interfaces.tui.widgets import TranscriptScroll

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("Whole delta 完整正文")
        original = pilot.pause
        queued, completed = [], []

        async def pause_then_queue(delay=None):
            await original(delay)
            if not queued:
                queued.append(True)
                app.screen.call_next(lambda: completed.append(True))

        monkeypatch.setattr(pilot, "pause", pause_then_queue)
        await wait_for_committed_view(
            pending, app.query_one("#transcript", TranscriptScroll), pilot
        )
        assert completed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
async def test_sentinel_first_frame_survives_400ms_and_reflow(theme, clock, monkeypatch):
    from rich.style import Style

    from neuro_code.interfaces.tui import text_arrival

    monkeypatch.setattr(text_arrival, "DURATION_SECONDS", 0.5)
    monkeypatch.setattr(
        widgets, "ink_style", lambda style, *args: style + Style(reverse=True, bold=True)
    )
    app = make_app(theme, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        pending = await begin(app, pilot)
        render = pending.render_lines
        monkeypatch.setattr(
            pending,
            "render_lines",
            lambda crop: super(AssistantMessage, pending).render_lines(crop),
        )
        app._update_pending_assistant("Cold 新文字")
        await pilot.pause()
        clock[0] += 0.4
        pending.on_resize(events.Resize(Size(111, 1), Size(111, 1)))
        pending.invalidate_stream_view()
        monkeypatch.setattr(pending, "render_lines", render)
        strips = pending.render_lines(Region(0, 0, pending.size.width, pending.size.height))
        styled = [
            segment
            for strip in strips
            for segment in strip
            if segment.style and segment.style.reverse and segment.style.bold
        ]
        assert styled
        assert set(pending._arrival.births.values()) == {clock[0]}
        timer = pending._arrival_timer
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert pending._arrival_timer is timer
        assert set(pending._arrival.births.values()) == {clock[0]}


def test_280ms_duration_cannot_consume_400ms_pending(monkeypatch):
    from neuro_code.interfaces.tui import text_arrival

    monkeypatch.setattr(text_arrival, "DURATION_SECONDS", 0.28)
    timeline = ArrivalTimeline()
    timeline.receive(0, 1, 1)
    timeline.prune(1.4)
    assert timeline.start_visual((0, 1), 1.4) == 0
    assert timeline.age((0, 1), 1.6) == pytest.approx(0.2)
    assert timeline.start_visual((0, 1), 1.7) is None


@pytest.mark.asyncio
async def test_real_replay_has_visible_sentinel_and_zero_idle_work(tmp_path):
    import json

    from tests.visual.text_arrival.lifecycle_replay import cold_tape, headless, parser

    args = parser().parse_args(
        ["--headless", "--sentinel", "--speed", "3", "--output", str(tmp_path / "replay.json")]
    )
    await headless(args)
    result = json.loads(args.output.read_text())
    assert cold_tape()[0]["text"].count("\n") >= 540
    assert result["styled_source_count"] > 0
    assert result["animation_parse_count"] == 0
    assert result["animation_refresh"].get("layout", 0) == 0
    assert result["idle_after_settle"]["body_refresh"] == 0
    assert result["idle_after_settle"]["animation_tick"] == 0
    assert result["frames"][0]["samples"][0]["age_ms"] < 30
    assert any(
        sample["reverse"] and sample["bold"]
        for frame in result["frames"]
        for sample in frame["samples"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [0, 0.05, 0.15, 0.3, 0.4, 0.5])
async def test_first_visible_segment_starts_at_zero_after_render_stall(stall, clock, monkeypatch):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        pending = await begin(app, pilot)
        render = pending.render_lines
        monkeypatch.setattr(
            pending,
            "render_lines",
            lambda crop: super(AssistantMessage, pending).render_lines(crop),
        )
        app._update_pending_assistant("New prose 新文字")
        await pilot.pause()
        assert pending._arrival.arrivals
        assert not pending._arrival.births
        assert pending._arrival_timer is None
        clock[0] += stall
        ages = []
        original = widgets.ink_style

        def traced(style, age, *args):
            ages.append(age)
            return original(style, age, *args)

        monkeypatch.setattr(widgets, "ink_style", traced)
        monkeypatch.setattr(pending, "render_lines", render)
        crop = Region(0, 0, pending.size.width, pending.size.height)
        canonical = signature(
            [list(s) for s in super(AssistantMessage, pending).render_lines(crop)]
        )
        visible = signature([list(s) for s in pending.render_lines(crop)])
        assert visible != canonical
        assert ages
        assert set(ages) == {0}
        assert pending._arrival_timer is not None
        births = dict(pending._arrival.births)
        clock[0] += 0.2
        pending._arrival_tick()
        await pilot.pause()
        assert signature([list(s) for s in pending.render_lines(crop)]) == canonical
        assert pending._arrival.births == births
        assert pending._arrival_timer is None


@pytest.mark.asyncio
async def test_scrollbar_reflow_preserves_pending_birth_and_remaps_rows(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(162, 86)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("Visible prose 可见正文")
        await pilot.pause()
        assert pending.region.width == 156
        births = dict(pending._arrival.births)
        assert births
        timer = pending._arrival_timer
        pending._arrival.receive(len(pending.content), len(pending.content) + 3, clock[0])
        arrivals = tuple(pending._arrival.arrivals)
        pending._arrival_view_width = 156
        for width in [155, 156]:
            pending._arrival_rows.add(9999)  # obsolete row, never reused after reflow
            pending.on_resize(
                events.Resize(Size(width, pending.size.height), Size(width, pending.size.height))
            )
            assert app.size == Size(162, 86)
            assert tuple(pending._arrival.arrivals) == arrivals
            assert pending._arrival.births == births
            assert not pending._arrival_rows
            assert pending._arrival_timer is timer
            pending.render_lines(Region(0, 0, pending.size.width, pending.size.height))
            assert pending._arrival_rows
            assert 9999 not in pending._arrival_rows
            assert pending._arrival.births == births
        # Trigger the same width loss through a real overflow, not only events.
        source = "old paragraph\n\n" * 100 + "New tail 新文字"
        app._update_pending_assistant(source)
        clock[0] += 0.03
        pending._commit_pending_view(pending._stream_generation)
        await pilot.pause()
        assert app.size == Size(162, 86)
        assert pending.region.width == 155
        assert pending.content == source
        assert pending._arrival.births
        assert pending._arrival_timer is not None


@pytest.mark.asyncio
async def test_true_resize_theme_and_rebuild_never_rebirth(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        pending = await begin(app, pilot)
        source = ("A long paragraph 正文 " * 10).rstrip()
        app._update_pending_assistant(source)
        await pilot.pause()
        births = dict(pending._arrival.births)
        timer = pending._arrival_timer
        assert births
        for width, height in [(100, 32), (80, 24), (120, 40)]:
            clock[0] += 0.02
            pending._arrival_rows.add(9999)
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            assert pending.content == source == pending.renderable.markup
            assert pending._arrival.births == births
            assert pending._arrival_timer is timer
            assert 9999 not in pending._arrival_rows
        app._apply_ui_theme(UiTheme.PORCELAIN)
        await pilot.pause()
        assert pending._arrival.births == births
        assert pending._arrival_timer is timer
        app._update_pending_assistant(source + " appended")
        clock[0] += 0.03
        pending._commit_pending_view(pending._stream_generation)
        await pilot.pause()
        assert all(pending._arrival.births[s] == at for s, at in births.items())


@pytest.mark.asyncio
async def test_unseen_cached_sources_do_not_start_and_cropped_cells_fail_closed(clock, monkeypatch):
    from textual.strip import Strip

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        pending.content = "中"
        md = AssistantMarkdown("中")
        pending.update(md)
        pending._arrival_enabled = True
        pending._arrival.receive(0, 1, clock[0])
        md.cached_sources = ((0, 1), (9, 10))
        from rich.style import Style

        style = Style(meta={ARRIVAL_META: (0, 1)})
        monkeypatch.setattr(
            widgets.ConversationMessage,
            "render_lines",
            lambda self, crop: [Strip([Segment(" ", style)], 1)],
        )
        pending.render_lines(Region(0, 0, 1, 1))
        assert not pending._arrival.births
        assert pending._arrival_timer is None
        monkeypatch.setattr(
            widgets.ConversationMessage,
            "render_lines",
            lambda self, crop: [Strip([Segment("中", style)], 2)],
        )
        pending.render_lines(Region(0, 0, 2, 1))
        assert pending._arrival.births == {0: clock[0]}
        assert 9 not in pending._arrival.births
