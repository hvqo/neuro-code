"""Assistant Markdown semantic hierarchy and responsive reading rhythm."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segment

from neuro_code.interfaces.tui.syntax import contrast_ratio
from neuro_code.interfaces.tui.theme import markdown_theme, syntax_theme, textual_theme_for
from neuro_code.interfaces.tui.widgets import AssistantMarkdown, AssistantMessage
from neuro_code.shared.syntax_theme import SyntaxTheme
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.markdown_fixtures import READING_FIXTURES
from tests.visual.showcases import make_app


def owner(theme: UiTheme, syntax: SyntaxTheme = SyntaxTheme.AUTO) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(theme=theme.textual_name, _syntax_theme=syntax))


def rows(markdown: Markdown, console: Console, width: int) -> list[str]:
    return [
        "".join(segment.text for segment in line).rstrip()
        for line in console.render_lines(markdown, console.options.update(width=width, height=None))
    ]


@pytest.mark.parametrize("theme", list(UiTheme))
def test_heading_semantics_are_ui_owned_and_syntax_independent(theme: UiTheme) -> None:
    themes = [markdown_theme(owner(theme, syntax)).styles for syntax in SyntaxTheme]
    assert all(styles == themes[0] for styles in themes)
    styles = themes[0]
    assert styles["markdown.h1"].color == styles["markdown.link"].color
    assert styles["markdown.h2"].color == styles["markdown.link"].color
    assert styles["markdown.h1"].bold
    assert all(not styles[f"markdown.h{level}"].bold for level in range(2, 7))
    assert styles["markdown.h3"].color == styles["markdown.code"].color
    assert styles["markdown.h4"].color == styles["markdown.strong"].color
    assert styles["markdown.h5"].color == styles["markdown.code"].color
    assert styles["markdown.h6"].color == styles["markdown.block_quote"].color
    assert all(styles[f"markdown.h{level}"].bgcolor is None for level in range(1, 7))
    assert not styles["markdown.h2"].underline
    if theme is not UiTheme.SYSTEM:
        color = styles["markdown.h2"].color
        assert color is not None
        assert contrast_ratio(color.name, textual_theme_for(theme).background) >= 4.5
    console = Console(theme=markdown_theme(owner(theme)), width=70)
    for level in range(1, 7):
        segments = list(console.render(AssistantMarkdown(f"{'#' * level} title")))
        title = next(segment for segment in segments if "title" in segment.text)
        assert title.style is not None
        assert title.style.color == styles[f"markdown.h{level}"].color
        assert bool(title.style.bold) == (level == 1)


@pytest.mark.parametrize("width", [114, 94, 74])
@pytest.mark.parametrize("name", ["chinese-paragraphs", "english-paragraphs", "mixed-paragraphs"])
def test_only_independent_top_level_paragraphs_gain_one_row(width: int, name: str) -> None:
    source = READING_FIXTURES[f"markdown-{name}"]
    console = Console(width=width, theme=markdown_theme(owner(UiTheme.GRAPHITE)))
    compact = rows(AssistantMarkdown(source, compact=lambda: True), console, width)
    regular = rows(AssistantMarkdown(source), console, width)
    assert len(regular) - len(compact) == source.count("\n\n")
    assert [line for line in compact if line] == [line for line in regular if line]
    assert max(cell_len(line) for line in regular) <= width


def test_exact_paragraph_spacing_and_softbreak_contract() -> None:
    console = Console(width=70)
    assert rows(AssistantMarkdown("First.\n\nSecond."), console, 70) == [
        "First.",
        "",
        "",
        "Second.",
    ]
    assert rows(AssistantMarkdown("First.\n\nSecond.", compact=lambda: True), console, 70) == [
        "First.",
        "",
        "Second.",
    ]
    for compact in [False, True]:
        assert rows(
            AssistantMarkdown("First\nsoft break.", compact=lambda compact=compact: compact),
            console,
            70,
        ) == ["First soft break."]


@pytest.mark.parametrize(
    "name", ["paragraph-list", "nested-list", "paragraph-code", "blockquote", "table", "headings"]
)
def test_other_block_transitions_have_identical_segments(name: str) -> None:
    source = READING_FIXTURES[f"markdown-{name}"]
    console = Console(width=74, theme=markdown_theme(owner(UiTheme.GRAPHITE)))
    compact = AssistantMarkdown(source, compact=lambda: True)
    regular = AssistantMarkdown(source)
    assert list(console.render(compact)) == list(console.render(regular))


@pytest.mark.parametrize("choice", list(SyntaxTheme))
def test_code_inline_emphasis_and_link_keep_native_rendering(choice: SyntaxTheme) -> None:
    host = owner(UiTheme.GRAPHITE, choice)
    theme = markdown_theme(host)
    code_theme = syntax_theme(host)
    source = READING_FIXTURES["markdown-paragraph-code"]
    source += "\n\n- **bold** *italic* [link](https://example.com) `AGENTS.md`"
    production = AssistantMarkdown(source, code_theme=code_theme)  # type: ignore[arg-type]
    native = Markdown(source, code_theme=code_theme)  # type: ignore[arg-type]
    console = Console(width=74, theme=theme, force_terminal=True, color_system="truecolor")
    assert list(console.render(production)) == list(console.render(native))
    assert theme.styles["markdown.code"].bgcolor is None
    assert production.markup == str(production) == source
    assert rows(production, console, 74) == rows(production, console, 74)


@pytest.mark.parametrize("compact", [False, True])
def test_streaming_token_append_introduces_gap_only_once(compact: bool) -> None:
    console = Console(width=74)
    prefix = "Completed paragraph."
    suffix = "Second paragraph arrives progressively."
    for stop in range(1, len(suffix) + 1):
        rendered = rows(
            AssistantMarkdown(
                prefix + "\n\n" + suffix[:stop], compact=lambda compact=compact: compact
            ),
            console,
            74,
        )
        gap = 1 if compact else 2
        assert rendered == [prefix, *[""] * gap, suffix[:stop].rstrip()]
    for trailing in ["", "\n", "\n\n", "\n\n\n"]:
        assert rows(AssistantMarkdown(prefix + trailing), console, 74) == [prefix]


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", [UiTheme.SYSTEM, UiTheme.GRAPHITE, UiTheme.PORCELAIN])
@pytest.mark.parametrize("name", ["chinese-paragraphs", "long-answer", "streaming-final-state"])
async def test_production_resize_restores_same_projection_without_replacing_widget(
    theme: UiTheme, name: str
) -> None:
    app = make_app(theme, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        source = READING_FIXTURES[f"markdown-{name}"]
        app._write_entry("assistant", source)
        await pilot.pause()
        message = app.query_one(AssistantMessage)
        markdown = message._content
        assert isinstance(markdown, AssistantMarkdown)
        initial: tuple[list[Segment], int, int] | None = None
        for viewport in [(120, 40), (80, 24), (120, 40), (120, 24), (120, 40)]:
            await pilot.resize_terminal(*viewport)
            await pilot.pause()
            width = message.content_size.width
            expected = AssistantMarkdown(
                source,
                code_theme=markdown.code_theme,
                style=markdown.style,
                hyperlinks=False,
                compact=lambda viewport=viewport: viewport[1] == 24,
            )
            assert rows(markdown, app.console, width) == rows(expected, app.console, width)
            assert message.region.height == len(rows(markdown, app.console, width))
            segments = list(app.console.render(markdown, app.console.options.update(width=width)))
            if viewport == (120, 40):
                initial = (segments, message.region.height, width) if initial is None else initial
                assert (segments, message.region.height, width) == initial
            assert message._content is markdown
            assert message.content == app._entries[-1].text == source


@pytest.mark.asyncio
async def test_production_pending_append_and_resize_have_no_retained_spacers() -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        source = READING_FIXTURES["markdown-chinese-paragraphs"]
        identity = None
        for stop in [40, 101, 166, 240, 360, len(source)]:
            content = source[:stop]
            app._update_pending_assistant(content)
            pending = app._pending_assistant
            assert isinstance(pending, AssistantMessage)
            identity = id(pending) if identity is None else identity
            assert id(pending) == identity
            for viewport in [(120, 40), (80, 24), (120, 40)]:
                await pilot.resize_terminal(*viewport)
                await pilot.pause()
                markdown = pending._content
                assert isinstance(markdown, AssistantMarkdown)
                expected = AssistantMarkdown(
                    content,
                    code_theme=markdown.code_theme,
                    style=markdown.style,
                    hyperlinks=False,
                    compact=lambda viewport=viewport: viewport[1] == 24,
                )
                assert rows(markdown, app.console, pending.content_size.width) == rows(
                    expected, app.console, pending.content_size.width
                )
                assert pending.region.height == len(
                    rows(markdown, app.console, pending.content_size.width)
                )
                assert pending.content == content
        app._finish_pending_assistant(source)
        assert app._entries[-1].text == source
        assert app._pending_assistant is None
        await pilot.pause()
        assert pending.region.height == len(
            rows(pending._content, app.console, pending.content_size.width)
        )


def test_native_rich_registry_and_original_source_are_not_modified() -> None:
    from rich.markdown import Paragraph

    assert Markdown.elements["paragraph_open"] is Paragraph
    source = READING_FIXTURES["markdown-long-answer"]
    rendered = AssistantMarkdown(source, hyperlinks=False)
    assert rendered.markup == str(rendered) == source
    assert rendered.hyperlinks is False


@pytest.mark.asyncio
async def test_markdown_policy_follows_main_shell_while_modal_is_open() -> None:
    from neuro_code.interfaces.tui.screens import SettingsScreen
    from neuro_code.shared.ui_language import UiLanguage

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=(120, 40)) as pilot:
        app._write_entry("assistant", "First.\n\nSecond.")
        await pilot.pause()
        markdown = app.query_one(AssistantMessage)._content
        assert isinstance(markdown, AssistantMarkdown)
        app.push_screen(
            SettingsScreen(
                UiLanguage.ENGLISH, language=UiLanguage.ENGLISH, provider_settings_available=False
            )
        )
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not app.screen.has_class("compact-chrome")
        assert rows(markdown, app.console, 70) == ["First.", "", "Second."]
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert rows(markdown, app.console, 70) == ["First.", "", "", "Second."]
