"""Composer and shell geometry across the approved visual viewports."""

from __future__ import annotations

import pytest

from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.readiness import wait_for_screenshot_readiness
from tests.visual.showcases import make_app, populate_fixture, show_fixture_screen


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
@pytest.mark.parametrize("viewport", [(120, 40), (100, 32), (80, 24)])
async def test_empty_session_preserves_reading_space_and_a_locatable_composer(
    theme: UiTheme, viewport: tuple[int, int]
) -> None:
    width, height = viewport
    app = make_app(theme, fixture="empty-conversation")

    async with app.run_test(size=viewport) as pilot:
        await pilot.pause()
        header = app.query_one("#header")
        transcript = app.query_one("#transcript")
        composer = app.query_one("#composer")
        surface = app.query_one("#prompt-surface")
        prompt = app.query_one("#prompt", PromptInput)
        send = app.query_one("#prompt-send")
        status = app.query_one("#runtime-bar")
        brand = app.query_one("#brand")
        runtime_primary = app.query_one("#runtime-primary")

        assert app.entries == ()
        assert header.region.height == 1
        assert header.region.bottom == transcript.region.y
        assert transcript.region.bottom == composer.region.y
        assert composer.region.bottom == status.region.bottom == height
        assert composer.region.height == (2 if width <= 80 else 4)
        assert transcript.region.height >= height * 4 // 5
        assert prompt.region.height == send.region.height == status.region.height == 1
        assert surface.region.height == (1 if width <= 80 else 3)
        assert send.region.y == prompt.region.y
        assert not app.query("#prompt-caption-hint")
        assert not app.query("#prompt-newline")
        assert surface.styles.background == prompt.styles.background
        assert prompt.region.x == runtime_primary.region.x + 1
        assert abs(prompt.region.x - brand.region.x) <= 2
        assert prompt.region.x - transcript.styles.padding.left == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("viewport", "expected_outer_x", "expected_outer_width", "expected_height", "expected_text_x"),
    [
        ((120, 40), 3, 114, 3, 5),
        ((100, 32), 3, 94, 3, 5),
        ((80, 24), 1, 78, 1, 3),
    ],
)
@pytest.mark.parametrize("theme", [UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM])
async def test_composer_matches_user_message_surface_and_reading_axis(
    viewport: tuple[int, int],
    expected_outer_x: int,
    expected_outer_width: int,
    expected_height: int,
    expected_text_x: int,
    theme: UiTheme,
) -> None:
    app = make_app(theme, fixture="user-assistant")

    async with app.run_test(size=viewport) as pilot:
        populate_fixture(app, "user-assistant")
        await pilot.pause()

        user_message = app.query_one(".message-user")
        surface = app.query_one("#prompt-surface")
        prompt = app.query_one("#prompt", PromptInput)
        send = app.query_one("#prompt-send")
        status = app.query_one("#runtime-bar")

        assert (user_message.region.x, user_message.region.width) == (
            expected_outer_x,
            expected_outer_width,
        )
        assert (surface.region.x, surface.region.width) == (
            expected_outer_x,
            expected_outer_width,
        )
        assert user_message.region.height == surface.region.height == expected_height
        assert user_message.content_region.x == prompt.region.x == expected_text_x
        assert prompt.region.y == send.region.y
        assert surface.region.bottom == status.region.y


@pytest.mark.asyncio
async def test_long_draft_grows_then_scrolls_with_viewport_budget_and_shrinks() -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    draft = "Review this source line.\n" * 20

    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptInput)
        transcript = app.query_one("#transcript")
        composer = app.query_one("#composer")
        initial_height = composer.region.height

        prompt.value = draft
        await pilot.pause()
        assert prompt.region.height == 8
        assert composer.region.height > initial_height
        assert transcript.region.height >= 20

        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert prompt.value == draft
        assert prompt.region.height == 6
        assert transcript.region.height >= 12
        assert composer.region.bottom == 24

        prompt.value = ""
        await pilot.pause()
        assert prompt.region.height == 1
        assert composer.region.height == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [(120, 40), (100, 32), (80, 24)])
@pytest.mark.parametrize("fixture", ["long-markdown", "tool-activity"])
async def test_long_content_keeps_shell_height_and_transcript_scrollable(
    fixture: str, viewport: tuple[int, int]
) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture=fixture)
    async with app.run_test(size=viewport) as pilot:
        populate_fixture(app, fixture)
        await pilot.pause()
        transcript = app.query_one("#transcript")
        composer = app.query_one("#composer")

        assert transcript.region.height >= viewport[1] * 4 // 5
        assert composer.region.height == (2 if viewport[0] <= 80 else 4)
        assert composer.region.bottom == viewport[1]
        if fixture == "long-markdown":
            # The compact shell may fit this sample at 120x40. Longer history
            # must still scroll without consuming Composer rows.
            populate_fixture(app, fixture)
            await pilot.pause()
            assert transcript.max_scroll_y > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [(120, 40), (100, 32), (80, 24)])
async def test_permission_modal_fits_without_resizing_the_shell(viewport: tuple[int, int]) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="permission")
    async with app.run_test(size=viewport):
        shell = app.screen_stack[0]
        composer = shell.query_one("#composer")
        shell_height = composer.region.height
        await show_fixture_screen(app, "permission", UiTheme.GRAPHITE)
        await wait_for_screenshot_readiness(app)
        dialog = app.screen.query_one("#approval-dialog")

        assert dialog.region.x >= 0
        assert dialog.region.y >= 0
        assert dialog.region.right <= viewport[0]
        assert dialog.region.bottom <= viewport[1]
        assert composer.region.height == shell_height


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [(120, 40), (80, 24)])
async def test_theme_switch_preserves_shell_geometry(viewport: tuple[int, int]) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="single-line-composer")
    async with app.run_test(size=viewport) as pilot:
        populate_fixture(app, "single-line-composer")
        await pilot.pause()
        selectors = ("#header", "#transcript", "#composer", "#prompt", "#runtime-bar")

        def regions() -> tuple[tuple[int, int, int, int], ...]:
            return tuple(tuple(app.query_one(selector).region) for selector in selectors)

        baseline = regions()
        for theme in (UiTheme.PORCELAIN, UiTheme.SYSTEM):
            app.theme = theme.textual_name
            await pilot.pause()
            assert regions() == baseline
