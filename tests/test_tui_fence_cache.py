"""Closed-fence cache equivalence, bounds and message lifecycle regressions."""

from dataclasses import replace
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.syntax import Syntax
from rich.theme import Theme

from neuro_code.interfaces.tui.fence_cache import FenceRenderCache, fence_render_key
from neuro_code.interfaces.tui.syntax import resolve_syntax_theme
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, AssistantMessage, TranscriptScroll
from neuro_code.shared.syntax_theme import SyntaxTheme
from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui_text_arrival import wait_for_committed_view
from tests.visual.showcases import make_app


def render(
    source, cache=None, width=80, theme="monokai", style="none", color="truecolor", complete=True
):
    console = Console(width=width, color_system=color, theme=Theme({"markdown.code_block": style}))
    md = AssistantMarkdown(source, code_theme=theme, hyperlinks=False, response_complete=complete)
    md.fence_cache = cache
    return console.render_lines(md, console.options, pad=False)


@pytest.mark.parametrize(
    "source",
    [
        "```python\nx = 1\n```\n\n中文 **strong** [link](https://example.com)",
        "~~~diff\n-old\n+new\n~~~~\n",
        "```unknown-language\nplain content\n```",
        '```rust\nfn main() {}\n```\n\n```json\n{"key":42}\n```\n\n```sh\necho hello\n```',
        "```python\nx = 1\r\n```\r\n正文",
        "   ```python\n   x = 1\n   ```\n",
        "- code\n\n  ```python\n  x = 1\n  ```\n\n- next",
        "> ```python\n> x = 1\n> ```\n",
    ],
)
def test_closed_fence_exact_segments_and_warm_highlight_reuse(source):
    cache = FenceRenderCache()
    expected = render(source)
    tail = source + "\n\n尾部 **正文** `inline`。"
    expected_tail = render(tail)
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        assert render(source, cache) == expected
        before = spy.call_count
        assert render(tail, cache) == expected_tail
        if AssistantMarkdown(source)._closed_fences:
            assert spy.call_count == before
        else:
            assert spy.call_count == 2 * before


def test_closed_540_lines_highlights_once_over_prose_revisions():
    cache = FenceRenderCache()
    source = "```python\n" + "x = 1\n" * 540 + "```\n\n"
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        for i in range(10):
            render(source + "中文正文。" * (i + 1), cache)
        assert spy.call_count == 1
    assert len(cache) == 1


@pytest.mark.parametrize(
    "source",
    [
        "```python\nx = 1\n",
        "```python\nx = 1\n``\n",
        "```python\nx = 1\n```oops\n",
        "```python\nx = 1\n    ```\n",
        "````python\nx = 1\n```\n",
        "- ```python\n  x = 1\n\nnext\n",
        "    x = 1\n",
    ],
)
def test_open_or_unproven_fence_never_cached_even_when_unchanged(source):
    cache = FenceRenderCache()
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        render(source, cache, complete=False)
        render(source, cache, complete=False)
        assert spy.call_count == (
            0 if AssistantMarkdown(source, response_complete=False)._active_fences else 2
        )
    assert len(cache) == 0


def test_code_language_theme_width_style_color_key_misses():
    cache = FenceRenderCache()
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        variants = [
            ("python", "x=1", 80, "monokai", "none", "truecolor"),
            ("python", "x=2", 80, "monokai", "none", "truecolor"),
            ("json", "x=2", 80, "monokai", "none", "truecolor"),
            ("json", "x=2", 80, "friendly", "none", "truecolor"),
            ("json", "x=2", 60, "friendly", "none", "truecolor"),
            ("json", "x=2", 60, "friendly", "dim", "truecolor"),
            ("json", "x=2", 60, "friendly", "dim", "256"),
        ]
        for number, (lang, code, width, theme, style, color) in enumerate(variants, 1):
            render(f"```{lang}\n{code}\n```", cache, width, theme, style, color)
            assert spy.call_count == number
        lang, code, width, theme, style, color = variants[0]
        render(f"```{lang}\n{code}\n```", cache, width, theme, style, color)
        assert spy.call_count == len(variants)


def test_resolved_theme_key_and_unknown_theme_fail_closed():
    console = Console()
    dark = resolve_syntax_theme(SyntaxTheme.AUTO, "#111111", "#dddddd")
    light = resolve_syntax_theme(SyntaxTheme.AUTO, "#ffffff", "#222222")
    assert fence_render_key("x", "py", dark, console, console.options) != fence_render_key(
        "x", "py", light, console, console.options
    )
    assert fence_render_key("x", "py", object(), console, console.options) is None


def test_bounded_lru_and_oversized_entry():
    cache = FenceRenderCache(max_entries=2)
    a, b, c = [f"```python\nx = {i}\n```" for i in range(3)]
    render(a, cache)
    render(b, cache)
    render(a, cache)
    render(c, cache)
    assert len(cache) == 2
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        render(a, cache)
        assert spy.call_count == 0
        render(b, cache)
        assert spy.call_count == 1
    tiny = FenceRenderCache(max_bytes=1)
    render(a, tiny)
    assert len(tiny) == tiny.estimated_bytes == 0
    cache.clear()
    assert len(cache) == cache.estimated_bytes == 0
    disabled = FenceRenderCache(max_entries=0)
    render(a, disabled)
    assert len(disabled) == 0


def test_identical_multiple_fences_share_result_but_different_languages_do_not():
    cache = FenceRenderCache()
    source = "```python\nx = 1\n```\n\n" * 2 + "```text\nx = 1\n```\n"
    expected = render(source)
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        assert render(source, cache) == expected
        assert spy.call_count == 2
    assert len(cache) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
@pytest.mark.parametrize("animate", [False, True])
async def test_message_lifecycle_resize_theme_animation_and_scroll(theme, animate):
    app = make_app(theme, fixture="empty-conversation")
    source = "```python\n" + "x = 1\n" * 100 + "```\n\n中文正文。"
    app._agent_preferences = replace(app._agent_preferences, text_arrival_animation=animate)
    with patch.object(AssistantMessage, "_motion_allowed", return_value=True):
        async with app.run_test(size=(120, 40)) as pilot:
            app._begin_pending_assistant()
            pending = app._pending_assistant
            transcript = app.query_one("#transcript", TranscriptScroll)
            for size in [(120, 40), (100, 32), (80, 24), (120, 40)]:
                await pilot.resize_terminal(*size)
                source += "更多中文。"
                app._update_pending_assistant(source)
                await wait_for_committed_view(pending, transcript, pilot)
                assert pending.content == source
                actual = pending.renderable
                expected = app._render_entry("assistant", source)
                expected.cache_stream_view(animate=animate)
                options = app.console.options.update(width=pending.content_size.width)
                assert app.console.render_lines(
                    actual, options, pad=False
                ) == app.console.render_lines(expected, options, pad=False)
            assert len(pending.fence_cache) > 0
            cache = pending.fence_cache
            app._apply_ui_theme(UiTheme.PORCELAIN)
            await pilot.pause()
            assert pending.fence_cache is cache
            transcript.scroll_to(y=0, animate=False, immediate=True)
            await pilot.pause()
            before = transcript.scroll_y
            app._update_pending_assistant(source + "末尾")
            await wait_for_committed_view(pending, transcript, pilot)
            assert transcript.scroll_y == before
            pending.stop_arrival()
            assert pending._arrival_timer is pending._stream_view_timer is None
            await pending.remove()
            assert len(cache) == cache.estimated_bytes == 0


def test_caches_are_owned_by_messages_and_content_replacement_is_safe():
    source = "```python\nx = 1\n```"
    first = AssistantMessage(AssistantMarkdown(source), content=source)
    second = AssistantMessage(AssistantMarkdown(source), content=source)
    assert first.fence_cache is not second.fence_cache
    render(source, first.fence_cache)
    assert len(second.fence_cache) == 0
    changed = AssistantMarkdown("```python\nx = 2\n```")
    first.update(changed)
    assert changed.fence_cache is first.fence_cache
    console = Console()
    baseline = AssistantMarkdown(changed.markup)
    assert console.render_lines(changed, console.options, pad=False) == console.render_lines(
        baseline, console.options, pad=False
    )


def test_byte_budget_evicts_and_closure_can_be_invalidated_by_append():
    from rich.segment import Segment

    console = Console()
    a = fence_render_key("a", "text", "monokai", console, console.options)
    b = fence_render_key("b", "text", "monokai", console, console.options)
    cache = FenceRenderCache()
    cache.put(a, (Segment("a"),))
    first_size = cache.estimated_bytes
    cache.max_bytes = first_size
    cache.put(b, (Segment("b"),))
    assert len(cache) == 1
    assert cache.get(a) is None
    assert cache.get(b) is not None
    cache.put(b, (Segment("b" * 100_000),))
    assert len(cache) == cache.estimated_bytes == 0
    source = "```python\nx = 1\n```"
    render(source, cache)
    cache = FenceRenderCache()
    render(source, cache)
    with patch.object(Syntax, "highlight", autospec=True, side_effect=Syntax.highlight) as spy:
        # The previously closing marker becomes code, so the old result is unsafe.
        render(source + "not a closing marker", cache)
        assert spy.call_count == 1


@pytest.mark.asyncio
async def test_conversation_switch_releases_owner_cache():
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(100, 32)) as pilot:
        app._write_entry("assistant", "```python\nx = 1\n```")
        await pilot.pause()
        message = app._entry_widgets[-1]
        cache = message.fence_cache
        assert len(cache) > 0
        app._replace_transcript([])
        await pilot.pause()
        assert len(cache) == cache.estimated_bytes == 0
