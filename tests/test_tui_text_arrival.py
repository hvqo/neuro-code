"""Production Measured Ink: canonical source, safe provenance and bounded views."""

from __future__ import annotations

from dataclasses import replace
from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.style import Style
from textual.geometry import Region
from textual.screen import ModalScreen

from neuro_code.application.ports.agent_preferences import AgentPreferences
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.infrastructure.persistence.ui_preferences import JsonUiPreferencesStore
from neuro_code.interfaces.tui import widgets
from neuro_code.interfaces.tui.text_arrival import (
    ARRIVAL_META,
    ArrivalTimeline,
    graphemes,
    ink_style,
    paragraph_sources,
    safe_glyph,
)
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, AssistantMessage
from neuro_code.shared.syntax_theme import SyntaxTheme
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app

PROSE = [
    "A simple English response.",
    "我正在检查流式正文和中文宽字符。",
    "Neuro Code 正在检查 source 和渲染。",
]
PROTECTED = [
    "# Heading\n\n## Heading two",
    "Use **bold** and *italic*.",
    "See [Neuro](https://example.com/path).",
    "Use `AGENTS.md` here.",
    "```python\nclass Example:\n    def method(self):\n        return 42\n```",
    '```json\n{"count": 42, "key": "value"}\n```',
    "```diff\n@@ -1 +1 @@\n-old\n+new\n```",
    "> quote\n> second line",
    "- first\n  - nested\n- second",
    "| Key | Value |\n| --- | --- |\n| a | b |",
    "Escape \\* and &amp; entity.",
    "![alt](picture.png)",
    "<kbd>key</kbd>",
]


def render(source, *, cached=True, animate=True, width=80, compact=False):
    console = Console(file=StringIO(), width=width, force_terminal=True, color_system="truecolor")
    md = AssistantMarkdown(source, compact=lambda: compact, hyperlinks=False)
    if cached:
        md.cache_stream_view(animate=animate)
    lines = console.render_lines(md, console.options.update(width=width, height=None), pad=False)
    return md, lines


def signature(lines):
    return [
        [
            (
                char,
                str((s.style or Style()).color),
                str((s.style or Style()).bgcolor),
                (s.style or Style()).bold,
                (s.style or Style()).italic,
                (s.style or Style()).underline,
                (s.style or Style()).dim,
                (s.style or Style()).link,
            )
            for s in line
            for char in s.text
        ]
        for line in lines
    ]


@pytest.mark.parametrize(
    "source", PROSE + PROTECTED + ["first\nsoft line\n\nsecond", "e\u0301 👩🏽‍💻 🇨🇳 你好"]
)
@pytest.mark.parametrize(("width", "compact"), [(114, False), (94, False), (74, True)])
def test_cache_preserves_canonical_markdown_geometry_and_semantics(source, width, compact):
    _, canonical = render(source, cached=False, width=width, compact=compact)
    md, cached = render(source, width=width, compact=compact)
    assert signature(cached) == signature(canonical)
    assert md.markup == source
    tagged = [s for line in cached for s in line if s.style and ARRIVAL_META in s.style.meta]
    if source in PROTECTED:
        assert not tagged
    elif source in PROSE:
        assert tagged


def test_source_map_requires_exact_literal_proof():
    for source in [
        "    indented",
        "&amp;",
        "escaped \\*",
        "[same](same)",
        "<b>text</b>",
        "*bold*",
        "- list",
    ]:
        md = AssistantMarkdown(source)
        assert not paragraph_sources(source, md.parsed)
    md = AssistantMarkdown("plain line\nsoft break\n\n中文")
    assert list(paragraph_sources(md.markup, md.parsed).values()) == [
        (0, "plain line soft break"),
        (23, "中文"),
    ]


def test_uax29_clusters_and_static_complex_fallback():
    source = "中e\u0301👩🏽‍💻🇨🇳क्‍ष"
    clusters = [g for _, _, g in graphemes(source)]
    assert clusters == ["中", "e\u0301", "👩🏽‍💻", "🇨🇳", "क्‍ष"]
    assert [safe_glyph(g) for g in clusters] == [True, True, False, False, False]
    md, lines = render(source)
    spans = [s for line in lines for s in line if s.style and ARRIVAL_META in s.style.meta]
    assert {s.text for s in spans} == {"中", "e\u0301"}
    assert md.markup == source


def test_birth_does_not_restart_with_combining_mark_and_bounded_tail():
    timeline = ArrivalTimeline()
    timeline.receive(0, 1, 1.0)
    assert timeline.age((0, 1), 1.01) == pytest.approx(0.01)
    timeline.receive(1, 2, 1.1)
    assert timeline.age((0, 2), 1.15) == pytest.approx(0.15)
    timeline.receive(2, 3, 1.19)
    assert timeline.age((0, 3), 1.19) is None
    for i in range(500):
        timeline.receive(i, i + 1, 2.0)
        timeline.age((i, i + 1), 2.01)
    assert len(timeline.arrivals) <= 256
    assert len(timeline.births) <= 385
    timeline.clear()
    assert not timeline.births
    assert not timeline.arrivals


@pytest.mark.parametrize(
    "style", [Style(bold=True), Style(italic=True), Style(underline=True), Style(link="https://x")]
)
def test_defensive_semantic_guard(style):
    assert ink_style(style, 0, 0, "white", "grey50") == style


def test_ink_lifetime_and_rank_limit():
    style = Style(color="grey70", bgcolor="black")
    assert ink_style(style, 0, 0, "white", "grey50").color == Style.parse("white").color
    assert ink_style(style, 0.08, 0, "white", "grey50").color == Style.parse("grey50").color
    assert ink_style(style, 0.15, 0, "white", "grey50") == style
    assert ink_style(style, 0, 12, "white", "grey50") == style


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(widgets, "monotonic", lambda: now[0])
    monkeypatch.setattr(AssistantMessage, "_motion_allowed", lambda self: True)
    return now


async def begin(app, pilot):
    app._begin_pending_assistant()
    pending = app._pending_assistant
    pending.display = True
    await pilot.pause()
    return pending


async def wait_for_committed_view(pending, transcript, pilot):
    # Pilot.pause() drains queued messages, not a future view deadline. Wait
    # for that deadline and its layout callback before asserting scroll state.
    # Do not wait for is_vertical_scroll_end: a broken follow must still fail.
    for _ in range(40):
        await pilot.pause(0.05)
        if (
            not pending._stream_dirty
            and pending._stream_view_timer is None
            and not transcript._stream_follow_pending
        ):
            assert pending.renderable.markup == pending.content
            return
    raise AssertionError("stream view deadline or follow callback did not settle")


@pytest.mark.asyncio
async def test_message_idle_is_not_a_future_view_commit_barrier(clock, monkeypatch):
    from unittest.mock import Mock

    from neuro_code.interfaces.tui.widgets import TranscriptScroll

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=False)
    async with app.run_test(size=(80, 24)) as pilot:
        pending = await begin(app, pilot)
        transcript = app.query_one("#transcript", TranscriptScroll)
        app._update_pending_assistant("first")
        callbacks = []

        def hold_deadline(delay, generation):
            callbacks.append(lambda: pending._commit_pending_view(generation))
            return Mock()

        monkeypatch.setattr(pending, "_schedule_stream_commit", hold_deadline)
        app._update_pending_assistant("first\n\n" + "完整中文 tail. " * 100)
        await pilot.pause()
        assert pending._stream_dirty
        assert pending.renderable.markup != pending.content
        assert len(callbacks) == 1
        app.set_timer(0.01, callbacks[0])
        await wait_for_committed_view(pending, transcript, pilot)
        assert transcript.is_vertical_scroll_end
        assert pending._stream_view_timer is None


@pytest.mark.asyncio
async def test_overdue_commit_is_delivered_once_without_another_delta(clock, monkeypatch):
    from asyncio import get_running_loop
    from types import SimpleNamespace

    from neuro_code.interfaces.tui.widgets import TranscriptScroll

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=False)
    async with app.run_test(size=(80, 24)) as pilot:
        pending = await begin(app, pilot)
        transcript = app.query_one("#transcript", TranscriptScroll)
        app._update_pending_assistant("first")
        loop = get_running_loop()

        def overdue(delay, callback):
            # An expired deadline models clock granularity / delayed scheduling
            # without blocking the loop or changing any canonical delta.
            return loop.call_at(loop.time() - 1, callback)

        monkeypatch.setattr(
            widgets, "get_running_loop", lambda: SimpleNamespace(call_later=overdue)
        )
        with patch.object(
            pending, "_commit_stream_view", wraps=pending._commit_stream_view
        ) as commit:
            app._update_pending_assistant("first complete 中文 delta")
            assert pending._stream_view_timer is not None
            assert pending.content == "first complete 中文 delta"
            await wait_for_committed_view(pending, transcript, pilot)
            assert commit.call_count == 1
            assert pending._stream_view_timer is None
            await pilot.pause(0.1)
            assert commit.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
async def test_whole_delta_immediate_tick_no_parse_and_exact_settle(theme, clock, monkeypatch):
    app = make_app(theme, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        calls = []
        original = AssistantMarkdown.__init__

        def counted(self, *args, **kwargs):
            calls.append(1)
            original(self, *args, **kwargs)

        monkeypatch.setattr(AssistantMarkdown, "__init__", counted)
        source = "I am checking the source. 我正在检查当前仓库。"
        app._update_pending_assistant(source)
        assert pending.content == source
        assert pending.renderable.markup == source
        await pilot.pause()
        crop = Region(0, 0, pending.size.width, pending.size.height)
        canonical = signature(
            [list(s) for s in super(AssistantMessage, pending).render_lines(crop)]
        )
        clock[0] += 0.08
        before = len(calls)
        with patch.object(pending, "refresh", wraps=pending.refresh) as refresh:
            animated = signature([list(s) for s in pending.render_lines(crop)])
            changed = sum(
                a != b
                for row_a, row_b in zip(animated, canonical, strict=True)
                for a, b in zip(row_a, row_b, strict=True)
            )
            assert 0 < changed <= 12
            pending._arrival_tick()
            assert all(not c.kwargs.get("layout") for c in refresh.call_args_list)
        assert len(calls) == before
        assert len(pending._arrival.births) <= 40
        clock[0] += 0.2
        pending._arrival_tick()
        await pilot.pause()
        assert signature([list(s) for s in pending.render_lines(crop)]) == canonical
        assert pending._arrival_timer is None
        assert not pending._arrival.arrivals
        with patch.object(pending, "refresh", wraps=pending.refresh) as refresh:
            pending._arrival_tick()
            assert refresh.call_count == 0


@pytest.mark.asyncio
async def test_metadata_cache_hits_and_width_compact_theme_invalidation(clock, monkeypatch):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("first paragraph\n\nsecond paragraph 中文")
        await pilot.pause()
        md = pending.renderable
        assert isinstance(md, AssistantMarkdown)
        original = widgets._MarkdownBody.__rich_console__
        calls = []

        def counted(self, *args, **kwargs):
            calls.append(1)
            yield from original(self, *args, **kwargs)

        monkeypatch.setattr(widgets._MarkdownBody, "__rich_console__", counted)
        options = app.console.options.update(width=114, height=None)
        list(app.console.render(md, options))
        count = len(calls)
        for _ in range(5):
            list(app.console.render(md, options))
        assert len(calls) == count
        list(app.console.render(md, options.update(width=74)))
        assert len(calls) == count + 1
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert md._view_key[1] is True
        assert not pending._arrival.arrivals
        app._apply_ui_theme(UiTheme.PORCELAIN)
        assert pending.renderable is not md


@pytest.mark.asyncio
async def test_semantic_foreground_and_background_cannot_receive_ink(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        pending.stream_content(
            "warning remains red",
            lambda text: AssistantMarkdown(text, style=Style(color="red", bgcolor="black")),
            animate=True,
        )
        await pilot.pause()
        crop = Region(0, 0, pending.size.width, pending.size.height)
        canonical = signature(
            [list(s) for s in super(AssistantMessage, pending).render_lines(crop)]
        )
        clock[0] += 0.08
        assert signature([list(s) for s in pending.render_lines(crop)]) == canonical


@pytest.mark.asyncio
async def test_high_frequency_coalesces_without_queuing_canonical_and_large_delta(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("first")
        first_view = pending.renderable
        text = "first"
        for glyph in "第二段 some more glyphs":
            text += glyph
            app._update_pending_assistant(text)
            assert pending.content == text
        assert pending.renderable is first_view
        clock[0] += 0.05
        pending._commit_pending_view(pending._stream_generation)
        assert pending.renderable.markup == text
        large = text + " 多字完整 delta" * 200
        app._update_pending_assistant(large)
        assert pending.content == large
        clock[0] += 0.05
        pending._commit_pending_view(pending._stream_generation)
        assert pending.renderable.markup == large
        await pilot.pause()
        assert len(pending.renderable.cached_sources) <= 384


@pytest.mark.asyncio
async def test_complete_cancel_error_and_new_response_do_not_share_state(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("first")
        app._update_pending_assistant("first unfinished draft")
        app._finish_pending_assistant(pending.content)
        assert pending._arrival_timer is pending._stream_view_timer is None
        assert not pending._arrival.births
        assert pending.content == app._entries[-1].text == "first unfinished draft"
        second = await begin(app, pilot)
        app._update_pending_assistant("second")
        assert second is not pending
        await app._discard_pending_assistant()
        assert second._arrival_timer is second._stream_view_timer is None
        assert app._pending_assistant is None
        third = await begin(app, pilot)
        app._update_pending_assistant("third")
        await app._discard_pending_assistant()  # shared provider-error cleanup
        app._write_turn_failure(RuntimeError("failure"))
        assert third._arrival_timer is third._stream_view_timer is None
        assert not third._arrival.arrivals


@pytest.mark.asyncio
async def test_resize_theme_syntax_view_switch_and_unmount_cleanup(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("first\n\nsecond")
        for viewport in [(80, 24), (120, 40)]:
            await pilot.resize_terminal(*viewport)
            await pilot.pause()
            assert pending._arrival_timer is pending._stream_view_timer is None
            assert not pending._arrival.arrivals
            assert pending.content == pending.renderable.markup
        app._update_pending_assistant("first\n\nsecond more")
        app._apply_ui_theme(UiTheme.PORCELAIN)
        assert pending._arrival_timer is pending._stream_view_timer is None
        app._update_pending_assistant("first\n\nsecond more syntax")
        app._apply_syntax_theme(SyntaxTheme.MONOKAI)
        assert pending._arrival_timer is pending._stream_view_timer is None
        app._update_pending_assistant("first\n\nsecond more syntax screen")
        await app.push_screen(ModalScreen())
        pending._arrival_tick()
        assert pending._arrival_timer is pending._stream_view_timer is None
        await app.pop_screen()
        app._update_pending_assistant("first\n\nsecond more syntax screen unmount")
        await pending.remove()
        assert pending._arrival_timer is pending._stream_view_timer is None
        assert not pending._arrival.births


@pytest.mark.asyncio
@pytest.mark.parametrize("animated", [False, True])
async def test_idle_text_has_no_clock_refresh_or_parse(animated, monkeypatch):
    import asyncio

    monkeypatch.setattr(AssistantMessage, "_motion_allowed", lambda self: True)
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=animated)
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        assert pending._arrival_timer is pending._stream_view_timer is None
        app._update_pending_assistant("完整中文 whole delta")
        app._update_pending_assistant("完整中文 whole delta with a tail")
        await asyncio.sleep(0.4)
        await pilot.pause()
        assert pending.content == pending.renderable.markup
        assert pending._arrival_timer is pending._stream_view_timer is None
        assert not pending._arrival.arrivals
        assert not pending._arrival.births
        with (
            patch.object(pending, "refresh", wraps=pending.refresh) as refresh,
            patch.object(AssistantMarkdown, "__init__", side_effect=AssertionError("idle parse")),
        ):
            await asyncio.sleep(0.15)
            assert refresh.call_count == 0


@pytest.mark.asyncio
async def test_animation_off_persistence_inheritance_and_live_disable(clock, tmp_path):
    store = JsonUiPreferencesStore(tmp_path / "ui.json")
    assert (await store.load_agent_preferences()).text_arrival_animation is None
    await store.save_agent_preferences(AgentPreferences(text_arrival_animation=False))
    assert (await store.load_effective_agent_preferences(tmp_path)).text_arrival_animation is False
    await store.save_theme(UiTheme.SYSTEM)
    assert (await store.load_agent_preferences()).text_arrival_animation is False
    with pytest.raises(ValueError, match="boolean"):
        AgentPreferences(text_arrival_animation=1)
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=False)
        app._update_pending_assistant("new text")
        await pilot.pause()
        assert not pending._arrival.arrivals
        assert not pending.renderable.cached_sources
        assert pending._arrival_timer is None
        app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=True)
        app._update_pending_assistant("new text animation")
        assert pending._arrival_timer is not None
        app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=False)
        app._apply_agent_preferences_to_ui()
        assert pending._arrival_timer is None


@pytest.mark.asyncio
async def test_headless_no_color_fallback_and_restore_are_static(monkeypatch):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        assert not pending._motion_allowed()
        monkeypatch.setattr(type(app), "is_headless", property(lambda self: False))
        app.no_color = True
        assert not pending._motion_allowed()
        app._update_pending_assistant("static response")
        await pilot.pause()
        assert not pending._arrival.arrivals
        app._replace_transcript([])
        await pilot.pause()
        assert pending._arrival_timer is None


@pytest.mark.asyncio
async def test_real_delta_event_preserves_parts_and_tools_do_not_reanimate(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        await begin(app, pilot)
        for index, delta in enumerate(["Whole delta. ", "完整中文。"]):
            await app._handle_event(
                AgentEvent.create(index, AgentEventKind.TEXT_DELTA, {"text": delta})
            )
            assert app._pending_assistant.content == "".join(app._assistant_parts)
        await app._handle_event(AgentEvent.create(3, AgentEventKind.MODEL_STEP_STARTED, {}))
        assert app._pending_assistant is None
        assert app._entry_widgets[-1]._arrival_timer is None


@pytest.mark.asyncio
async def test_large_cjk_delta_cell_geometry_and_new_canonical_revision(clock):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(80, 24)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("中文 English 正文 " * 100)
        await pilot.pause()
        crop = Region(0, 0, pending.size.width, pending.size.height)
        canonical = super(AssistantMessage, pending).render_lines(crop)
        clock[0] += 0.08
        animated = pending.render_lines(crop)
        assert [s.cell_length for s in animated] == [s.cell_length for s in canonical]
        assert [s.text for s in animated] == [s.text for s in canonical]
        assert len(pending.renderable.cached_sources) <= 12
        # A non-append revision is a new canonical view, never a stale arrival.
        clock[0] += 0.05
        app._update_pending_assistant("新回复")
        assert pending.content == "新回复"
        assert pending.renderable.markup == "新回复"
        assert pending._arrival.arrivals[0].start == 0
        pending.stop_arrival()
        assert pending._arrival_timer is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cadence", [0.05, 0.033, 0.025, 0.022, 0.016])
@pytest.mark.parametrize("animated", [False, True])
async def test_view_deadline_is_independent_of_animation_and_not_renewed(
    cadence, animated, clock, monkeypatch
):
    monkeypatch.setattr(widgets, "VIEW_COMMIT_SECONDS", cadence)
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=animated)
    async with app.run_test(size=(100, 32)) as pilot:
        pending = await begin(app, pilot)
        app._update_pending_assistant("first")
        view = pending.renderable
        clock[0] += cadence / 4
        app._update_pending_assistant("first second")
        timer = pending._stream_view_timer
        generation = pending._stream_generation
        assert timer is not None
        clock[0] += cadence / 4
        app._update_pending_assistant("first second 第三段")
        assert pending._stream_view_timer is timer
        assert pending._stream_generation == generation
        before = pending.renderable
        pending._arrival_tick()
        assert pending.renderable is before is view
        clock[0] += cadence
        pending._commit_pending_view(generation)
        assert pending.renderable.markup == pending.content
        assert pending._stream_view_timer is None
        if not animated:
            assert pending._arrival_timer is None
        app._update_pending_assistant(pending.content + " final")
        stale = pending._stream_generation
        pending.stop_arrival()
        final = pending.renderable
        pending._commit_pending_view(stale)
        assert pending.renderable is final
        assert pending._stream_view_timer is pending._arrival_timer is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cadence", [0.05, 0.033, 0.025, 0.022, 0.016])
async def test_growing_tail_follows_layout_but_manual_history_scroll_is_preserved(
    cadence, monkeypatch
):
    import asyncio

    from neuro_code.interfaces.tui.widgets import TranscriptScroll

    monkeypatch.setattr(widgets, "VIEW_COMMIT_SECONDS", cadence)
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(80, 24)) as pilot:
        pending = await begin(app, pilot)
        text = ""
        for index in range(70):
            text += f"第 {index} 段中文和 English 连续正文。" * 3 + "\n\n"
            app._update_pending_assistant(text)
            await asyncio.sleep(0.003)
        transcript = app.query_one("#transcript", TranscriptScroll)
        await wait_for_committed_view(pending, transcript, pilot)
        assert transcript.max_scroll_y > 0
        assert transcript.is_vertical_scroll_end
        transcript.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        assert not transcript.is_vertical_scroll_end
        y = transcript.scroll_y
        for index in range(25):
            text += f"补充第 {index} 段正文。" * 3 + "\n\n"
            app._update_pending_assistant(text)
            await asyncio.sleep(0.003)
        await wait_for_committed_view(pending, transcript, pilot)
        assert transcript.scroll_y == y
        assert not transcript._stream_follow_pending
        transcript.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        text += "恢复主动跟随后继续输出。" * 100
        app._update_pending_assistant(text)
        await wait_for_committed_view(pending, transcript, pilot)
        assert transcript.is_vertical_scroll_end
        app._replace_transcript([])
        assert transcript._stream_follow_y is None
        assert not transcript._stream_follow_pending
        assert pending._stream_view_timer is pending._arrival_timer is None


@pytest.mark.asyncio
async def test_history_page_up_intent_wins_before_scroll_animation_moves():
    from neuro_code.interfaces.tui.widgets import TranscriptScroll

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(80, 24)) as pilot:
        pending = await begin(app, pilot)
        text = "中文 English history.\n\n" * 100
        app._update_pending_assistant(text)
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptScroll)
        assert transcript.is_vertical_scroll_end
        transcript.action_page_up()
        assert transcript._stream_follow_paused
        app._update_pending_assistant(text + "More whole delta. " * 100)
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert not transcript.is_vertical_scroll_end
        assert not transcript._stream_follow_pending
        assert pending.content.endswith("More whole delta. " * 100)
