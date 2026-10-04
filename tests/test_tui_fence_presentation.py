"""Streaming code color is monotonic across parser rebuilds and lifecycle events."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.markdown import CodeBlock
from rich.syntax import Syntax

from neuro_code.interfaces.tui.fence_cache import FenceRenderCache, fence_render_key
from neuro_code.interfaces.tui.fence_presentation import FencePhase, FencePresentation
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, AssistantMessage, _FencedCodeBlock
from neuro_code.shared.syntax_theme import SyntaxTheme
from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui_text_arrival import wait_for_committed_view
from tests.visual.code_flicker.replay import CASES, python_replay, stress_deltas
from tests.visual.showcases import make_app


def revision(source, lifecycle, cache, *, complete=False, width=70, theme="monokai"):
    md = AssistantMarkdown(source, code_theme=theme, response_complete=complete)
    md.bind_fence_presentation(lifecycle, complete=complete)
    md.fence_cache = cache
    console = Console(width=width, color_system="truecolor")
    rows = console.render_lines(md, console.options, pad=False)
    return md, rows


def lexical_calls(spy):
    return sum(
        getattr((call.args[0].lexer or call.args[0].default_lexer), "name", "").lower()
        not in {"text", "text only"}
        for call in spy.call_args_list
    )


@pytest.mark.parametrize("language", list(CASES))
def test_every_delta_keeps_active_plain_and_finalizes_once(language):
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    source, transitions, active_color = "", [], None
    identities, token_ids = set(), set()
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        for delta in stress_deltas(language):
            source += delta
            md, rows = revision(source, lifecycle, cache)
            assert md.markup == source
            for token_id, record in md._fence_records.items():
                identities.add(record.identity)
                token_ids.add(token_id)
                transitions.append(record.foreground_mode)
                if record.phase is FencePhase.ACTIVE:
                    assert lexical_calls(spy) == 0
                    assert record.finalizations == 0
                    colors = {
                        segment.style.color
                        for row in rows
                        for segment in row
                        if segment.text.strip() and segment.style
                    }
                    if colors:  # The opener-only revision has no code glyphs yet.
                        assert len(colors) == 1
                        active_color = colors if active_color is None else active_color
                        assert colors == active_color
                else:
                    assert record.finalizations == 1
                    assert lexical_calls(spy) == 1
        assert len(identities) == 1
        assert len(token_ids) > 1  # New parser objects do not create new fence lifecycles.
        assert transitions == sorted(transitions)  # PLAIN precedes SYNTAX; no backtrack.
        assert lifecycle.records[next(iter(identities))].phase is FencePhase.CACHED


def test_partial_closer_at_eof_remains_active_even_when_parser_calls_it_closed():
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    source = "```python\nx = 1\n```"
    md, _ = revision(source, lifecycle, cache)
    assert md._closed_fences
    assert md._active_fences
    record = next(iter(md._fence_records.values()))
    identity = record.identity
    assert record.phase is FencePhase.ACTIVE
    for suffix in ["oops", "\n", "~~~\n"]:
        source += suffix
        md, _ = revision(source, lifecycle, cache)
        assert next(iter(md._fence_records.values())).identity == identity
        assert record.phase is FencePhase.ACTIVE
    revision(source + "```\n", lifecycle, cache)
    assert record.phase is FencePhase.CACHED
    assert record.finalizations == 1


@pytest.mark.parametrize("language", list(CASES))
@pytest.mark.parametrize("ending", ["", "```"])
def test_unclosed_response_complete_is_single_atomic_static_equivalent(language, ending):
    source = f"```{language}\n" + "".join(CASES[language]) + ending
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    _, active = revision(source, lifecycle, cache)
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        md, final = revision(source, lifecycle, cache, complete=True)
        assert lexical_calls(spy) == 1
        record = next(iter(md._fence_records.values()))
        assert record.finalizations == 1
        assert record.phase is FencePhase.CACHED
        _, repeated = revision(source, lifecycle, cache, complete=True)
        assert lexical_calls(spy) == 1
        assert repeated == final
    with patch.object(_FencedCodeBlock, "__rich_console__", CodeBlock.__rich_console__):
        _, normal = revision(source, FencePresentation(), FenceRenderCache(), complete=True)
    assert final == normal
    assert md.markup == source

    def text(rows):
        return ["".join(segment.text for segment in row) for row in rows]

    def cells(rows):
        return [sum(segment.cell_length for segment in row) for row in rows]

    def backgrounds(rows):
        return [
            tuple(
                segment.style.bgcolor if segment.style else None
                for segment in row
                for _ in range(segment.cell_length)
            )
            for row in rows
        ]

    assert text(active) == text(final)
    assert cells(active) == cells(final)
    assert backgrounds(active) == backgrounds(final)


def test_stable_first_fence_remains_cached_while_second_grows():
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    source = "```python\nx = 1\n```\n\n```rust\n"
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        for delta in [
            "",
            "/*",
            " comment",
            " */\n",
            "fn main() {}\n",
            "```\n",
            "\nprose",
            " continues",
        ]:
            source += delta
            md, _ = revision(source, lifecycle, cache)
            records = list(md._fence_records.values())
            assert records[0].phase is FencePhase.CACHED
            assert records[0].finalizations == 1
            assert len(records) == 2
            assert lexical_calls(spy) == (2 if records[1].phase is not FencePhase.ACTIVE else 1)
        assert [r.finalizations for r in records] == [1, 1]


@pytest.mark.parametrize(
    "source", ["> ```python\n> x = 1\n", "- ```python\n  x = 1\n", "    ```python\nx = 1\n"]
)
def test_unprovable_source_mapping_uses_normal_renderer(source):
    md, _ = revision(source, FencePresentation(), FenceRenderCache())
    assert not md._fence_records


def test_cache_conditions_and_eviction_never_restart_lifecycle():
    lifecycle, cache = FencePresentation(), FenceRenderCache(max_entries=1)
    source = "```python\nx=1\n```\n"
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        for width, theme in [(70, "monokai"), (40, "monokai"), (40, "dracula"), (70, "monokai")]:
            md, _ = revision(source, lifecycle, cache, width=width, theme=theme)
            record = next(iter(md._fence_records.values()))
            assert record.phase is FencePhase.CACHED
            assert record.finalizations == 1
        assert lexical_calls(spy) == 4
        assert len(cache) == 1


def test_height_budget_is_not_part_of_pre_crop_segments():
    console = Console(width=80)

    def key(options):
        return fence_render_key("x=1", "python", "monokai", console, options)

    assert key(console.options.update(height=20)) == key(console.options.update(height=40))
    assert key(console.options.update(width=40)) != key(console.options)


def test_source_anchor_uses_newlines_only_and_normalizes_crlf():
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    source = "prose\r\n\r\n```python\r\ntext = 'a\u2028b'\r\n"
    md, _ = revision(source, lifecycle, cache)
    record = next(iter(md._fence_records.values()))
    assert record.identity.source_start == len("prose\n\n")
    revision(source + "```\r\n", lifecycle, cache)
    assert record.phase is FencePhase.CACHED
    assert record.finalizations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("animate", [True, False])
@pytest.mark.parametrize("theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
async def test_real_view_commit_theme_resize_complete_and_prose(animate, theme):
    app = make_app(theme, fixture="empty-conversation")
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=animate)
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        async with app.run_test(size=(120, 40)) as pilot:
            source = "```python\n" + "value = 1\n" * 100
            app._update_pending_assistant(source)
            pending = app._pending_assistant
            assert isinstance(pending, AssistantMessage)
            transcript = app.query_one("#transcript")
            await wait_for_committed_view(pending, transcript, pilot)
            identity = next(iter(pending.fence_presentation.records))
            for delta in ['text = "', "hello", '"\n', "```", "oops\n"]:
                source += delta
                app._update_pending_assistant(source)
                await wait_for_committed_view(pending, transcript, pilot)
                assert next(iter(pending.fence_presentation.records)) == identity
                assert pending.fence_presentation.records[identity].phase is FencePhase.ACTIVE
                assert lexical_calls(spy) == 0
            await pilot.resize_terminal(80, 24)
            app._apply_ui_theme(
                UiTheme.PORCELAIN if theme is not UiTheme.PORCELAIN else UiTheme.GRAPHITE
            )
            app._apply_syntax_theme(SyntaxTheme.MONOKAI)
            await pilot.pause()
            assert pending.content == source
            assert pending.renderable._active_fences
            assert lexical_calls(spy) == 0
            source += "```\n"
            app._update_pending_assistant(source)
            await wait_for_committed_view(pending, transcript, pilot)
            assert lexical_calls(spy) == 1
            for delta in ["\nText ", "after code."]:
                source += delta
                app._update_pending_assistant(source)
                await wait_for_committed_view(pending, transcript, pilot)
                assert lexical_calls(spy) == 1
            app._finish_pending_assistant(source)
            await pilot.pause()
            assert pending.content == pending.renderable.markup == source
            assert pending._stream_view_timer is pending._arrival_timer is None
            assert lexical_calls(spy) == 1
            assert pending.fence_presentation.records[identity].finalizations == 1
            await pending.remove()
            assert not pending.fence_presentation.records
            assert not len(pending.fence_cache)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["cancel", "error"])
async def test_turn_failure_clears_open_fence_and_next_message_gets_new_owner(exit_kind):
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:

        async def fail():
            app._update_pending_assistant("```python\nvalue = 1\n")
            await pilot.pause()
            pending = app._pending_assistant
            saved.append(pending)
            if exit_kind == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("deterministic test error")

        saved = []
        if exit_kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await app._run_agent_turn(fail)
        else:
            await app._run_agent_turn(fail)
        await pilot.pause()
        pending = saved[0]
        assert app._pending_assistant is None
        assert not pending.fence_presentation.records
        assert not len(pending.fence_cache)
        assert pending._arrival_timer is pending._stream_view_timer is None
        app._update_pending_assistant("```python\nnew = 1\n")
        replacement = app._pending_assistant
        assert replacement is not pending
        assert replacement.fence_presentation is not pending.fence_presentation


@pytest.mark.asyncio
async def test_reload_finalizes_open_fence_and_content_replacement_resets_generation():
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        app._write_entry("assistant", "```python\nx = 1")
        await pilot.pause()
        restored = app._entry_widgets[-1]
        assert restored._response_complete
        assert next(iter(restored.fence_presentation.records.values())).phase is FencePhase.CACHED
        app._update_pending_assistant("```python\nx = 1\n```\n")
        pending = app._pending_assistant
        await wait_for_committed_view(pending, app.query_one("#transcript"), pilot)
        old = next(iter(pending.fence_presentation.records))
        app._update_pending_assistant("```rust\nfn main() {}")
        await wait_for_committed_view(pending, app.query_one("#transcript"), pilot)
        new = next(iter(pending.fence_presentation.records))
        assert new.generation > old.generation
        assert pending.fence_presentation.records[new].phase is FencePhase.ACTIVE


def test_replay_has_200_lines_intact_close_and_following_prose():
    deltas = python_replay()
    assert len(deltas) > 200
    source = "".join(deltas)
    md, _ = revision(source, FencePresentation(), FenceRenderCache(), complete=True)
    assert len(md._fence_records) == 1
    assert source.endswith("代码配色保持稳定。\n")


def test_main_relexing_changes_existing_incomplete_comment_foreground():
    from neuro_code.interfaces.tui.syntax import resolve_syntax_theme

    theme = resolve_syntax_theme(SyntaxTheme.MONOKAI, "#181818", "#dddddd")
    console = Console()
    incomplete = Syntax("/* comment", "typescript", theme=theme).highlight("/* comment")
    complete = Syntax("/* comment */", "typescript", theme=theme).highlight("/* comment */")
    assert (
        incomplete.get_style_at_offset(console, 0).color
        != complete.get_style_at_offset(console, 0).color
    )


def test_building_final_revision_cannot_recolor_previous_visible_plain_view():
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    source = "```typescript\n/* comment"
    old, old_rows = revision(source, lifecycle, cache)
    newer = AssistantMarkdown(source + " */\n```\n", response_complete=False)
    newer.bind_fence_presentation(lifecycle, complete=False)
    newer.fence_cache = cache
    console = Console(width=70, color_system="truecolor")
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        # Lifecycle already finalized, but the current view's decision is frozen.
        assert next(iter(old._fence_records.values())).phase is FencePhase.FINALIZED
        assert console.render_lines(old, console.options, pad=False) == old_rows
        assert lexical_calls(spy) == 0
        console.render_lines(newer, console.options, pad=False)
        assert lexical_calls(spy) == 1


@pytest.mark.parametrize("theme", list(SyntaxTheme))
def test_incomplete_string_finalization_keeps_production_surface(theme):
    from neuro_code.interfaces.tui.syntax import resolve_syntax_theme

    resolved = resolve_syntax_theme(theme, "#181818", "#dddddd")
    source = "```typescript\n/* comment not yet closed"
    lifecycle, cache = FencePresentation(), FenceRenderCache()
    _, active = revision(source, lifecycle, cache, theme=resolved)
    _, final = revision(source, lifecycle, cache, complete=True, theme=resolved)
    for before, after in zip(active, final, strict=True):
        assert "".join(s.text for s in before) == "".join(s.text for s in after)

        def backgrounds(row):
            return [s.style.bgcolor if s.style else None for s in row for _ in range(s.cell_length)]

        assert backgrounds(before) == backgrounds(after)
