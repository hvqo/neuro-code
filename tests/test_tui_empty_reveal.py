"""Bounded, deterministic reveal: presentation cannot own input or durable truth."""

from __future__ import annotations

from unittest.mock import Mock, PropertyMock, patch

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.color import Color
from textual.timer import Timer

from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.empty_state import LOGO_SIZES, EmptyStateIdentity
from neuro_code.interfaces.tui.empty_state_logo import LOGO_ROWS
from neuro_code.interfaces.tui.empty_state_reveal import TOTAL_DURATION_MS, VORTEX_FRAMES
from neuro_code.interfaces.tui.terminal_palette import TerminalColorLevel, TerminalPalette
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui_empty_identity import fixed_clock, settle
from tests.test_tui_visual_snapshots import canonicalize_svg
from tests.visual.showcases import make_app

THEMES = (UiTheme.GRAPHITE, UiTheme.PORCELAIN, UiTheme.SYSTEM)
VIEWPORTS = ((120, 40), (100, 32), (80, 24))


@pytest.mark.parametrize(
    ("theme", "palette"),
    [
        (UiTheme.GRAPHITE, None),
        (UiTheme.PORCELAIN, None),
        (UiTheme.SYSTEM, TerminalPalette()),
        (
            UiTheme.SYSTEM,
            TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30)),
        ),
        (
            UiTheme.SYSTEM,
            TerminalPalette(TerminalColorLevel.TRUECOLOR, (30, 30, 30), (246, 246, 246)),
        ),
    ],
)
async def test_quiet_resting_is_semantic_blend_and_only_click_brightens(
    theme: UiTheme, palette: TerminalPalette | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(theme, fixture="empty-conversation", terminal_palette=palette)
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        resting = symbol.visual_style.rich_style
        semantic = Color.parse(app.get_css_variables()["text-dim"])
        assert resting.dim
        assert symbol._timer is None
        assert symbol._frame_index is None
        if semantic.ansi is None:
            assert symbol.styles.color == semantic.with_alpha(0.28)
            background = symbol.visual_style.background
            assert resting.color == (background + semantic.with_alpha(0.28)).rich_color
            assert resting.color != semantic.rich_color
        else:
            # Unknown ANSI defaults cannot be blended without inventing a palette.
            assert resting.color is not None
            assert resting.color.is_default
        clock = RevealClock(symbol, monkeypatch)
        await pilot.click(symbol)
        clock.seek(TOTAL_DURATION_MS // 2)
        peak = symbol.render()
        assert isinstance(peak, Text)
        assert symbol._frame_styles is not None
        assert symbol._frame_styles[-1].dim is False
        assert (
            symbol._frame_styles[-1].color
            == symbol.get_component_rich_style("empty-state--secondary", partial=True).color
        )
        clock.seek(TOTAL_DURATION_MS)
        assert symbol.visual_style.rich_style == resting


class RevealClock:
    """Drive real frame selection without waiting on the operating-system scheduler."""

    def __init__(self, symbol: EmptyStateIdentity, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 100.0
        self.symbol = symbol
        self.timers: list[Mock] = []
        monkeypatch.setattr("neuro_code.interfaces.tui.empty_state.monotonic", lambda: self.now)
        monkeypatch.setattr(symbol, "_motion_allowed", lambda: True)
        monkeypatch.setattr(symbol, "set_timer", self.schedule)

    def schedule(self, delay: float, callback: object, *, name: str) -> Mock:
        timer = Mock(spec=Timer)
        timer.delay = delay
        timer.callback = callback
        self.timers.append(timer)
        return timer

    def seek(self, milliseconds: int) -> None:
        self.now = 100.0 + (milliseconds + 0.01) / 1000
        self.symbol._advance_animation(self.symbol._animation_generation)


@pytest.mark.parametrize("size", LOGO_SIZES)
def test_vortex_assets_are_dense_samples_with_exact_resting(size: str) -> None:
    assert len(VORTEX_FRAMES) == 145
    assert TOTAL_DURATION_MS == 6000
    assert sum(frame.duration_ms for frame in VORTEX_FRAMES) == TOTAL_DURATION_MS
    assert {f.duration_ms for f in VORTEX_FRAMES[:-1]} == {41, 42}
    assert VORTEX_FRAMES[-1].duration_ms == 0
    width, height = LOGO_SIZES[size]
    for frame in VORTEX_FRAMES:
        assert len(frame.rows[size]) == height
        assert all(cell_len(row) == width for row in frame.rows[size])
        assert all(
            ch in " 01" or 0x2800 <= ord(ch) <= 0x28FF for row in frame.rows[size] for ch in row
        )
    assert VORTEX_FRAMES[0].rows[size] == VORTEX_FRAMES[-1].rows[size] == LOGO_ROWS[size]
    assert len({frame.rows[size] for frame in VORTEX_FRAMES}) > 100
    # Cell brightness is theme-neutral metadata, with no hardcoded RGB.
    for frame in VORTEX_FRAMES:
        assert all(len(row) == width for row in frame.levels[size])
        assert len(frame.levels[size]) == height
        assert set("".join(frame.levels[size])) <= set("0123456789abcdef")
    with pytest.raises(TypeError):
        VORTEX_FRAMES[1].rows[size] = ()  # type: ignore[index]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("viewport", VIEWPORTS)
async def test_full_sequence_restores_exact_static_render_and_shell(
    theme: UiTheme, viewport: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(theme, fixture="empty-conversation")
    with patch.object(NeuroCodeApp, "_update_clock", fixed_clock):
        async with app.run_test(size=viewport) as pilot:
            await settle(pilot)
            symbol = app.query_one(EmptyStateIdentity)
            assert symbol._timer is None
            assert not symbol.animation_running
            before = canonicalize_svg(app.export_screenshot())
            geometry = {
                key: app.query_one(f"#{key}").region
                for key in ("transcript", "prompt-surface", "runtime-bar")
            }
            clock = RevealClock(symbol, monkeypatch)
            assert symbol.activate()
            elapsed = 0
            for index, frame in enumerate(VORTEX_FRAMES[:-1]):
                clock.seek(elapsed)
                assert symbol._frame_index == index
                rendered = symbol.render()
                if isinstance(rendered, Text):
                    assert rendered.plain == "\n".join(frame.rows[symbol.asset_size])
                elapsed += frame.duration_ms
            clock.seek(elapsed)
            await settle(pilot)
            assert not symbol.animation_running
            assert symbol._timer is None
            assert all(timer.stop.called for timer in clock.timers)
            assert not tuple(symbol.workers)
            assert app.query_one("#transcript").max_scroll_y == 0
            assert all(
                app.query_one(f"#{key}").region == region for key, region in geometry.items()
            )
            assert canonicalize_svg(app.export_screenshot()) == before


async def test_repeat_click_ignored_then_reactivation_and_stale_callback_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        clock = RevealClock(symbol, monkeypatch)
        assert symbol.activate()
        generation = symbol._animation_generation
        first_timer = clock.timers[-1]
        assert not symbol.activate()
        assert symbol._animation_generation == generation
        assert len(clock.timers) == 1
        clock.seek(TOTAL_DURATION_MS)
        clock.now = 100.0
        assert symbol.activate()
        first_timer.callback()
        assert symbol._frame_index == 0
        assert symbol.animation_running


@pytest.mark.parametrize("boundary", ["user", "stream", "restore", "reset", "hide", "unmount"])
async def test_lifecycle_boundary_cancels_and_never_retains_intermediate_frame(
    boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(UiTheme.SYSTEM, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        clock = RevealClock(symbol, monkeypatch)
        assert symbol.activate()
        clock.seek(1600)
        if boundary == "user":
            app._write_entry("user", "First content")
        elif boundary == "stream":
            app._update_pending_assistant("Real streamed content")
        elif boundary == "restore":
            app._replace_transcript([Message(role=Role.USER, content="Restored history")])
        elif boundary == "reset":
            symbol.reset()
        elif boundary == "hide":
            symbol.display = False
        else:
            await symbol.remove()
        await settle(pilot)
        assert not symbol.animation_running
        assert symbol._frame_index is None
        assert symbol._timer is None
        assert not symbol.display or boundary == "unmount"
        assert all(timer.stop.called for timer in clock.timers)


async def test_resize_size_change_and_insufficient_space_cancel_without_residue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        clock = RevealClock(symbol, monkeypatch)
        for viewport, size in [
            ((100, 32), "medium"),
            ((80, 24), "small"),
            ((120, 40), "large"),
            ((60, 20), None),
        ]:
            clock.now = 100.0
            assert symbol.activate()
            clock.seek(1600)
            await pilot.resize_terminal(*viewport)
            await settle(pilot)
            assert not symbol.animation_running
            assert symbol._timer is None
            assert symbol.asset_size == size
            assert symbol.display == (size is not None)
        await pilot.resize_terminal(120, 40)
        await settle(pilot)
        assert symbol.display
        assert symbol.asset_size == "large"
        assert not symbol.animation_running


async def test_same_size_arrange_preserves_progress_and_theme_switch_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        clock = RevealClock(symbol, monkeypatch)
        assert symbol.activate()
        clock.seek(1600)
        index = symbol._frame_index
        app._sync_empty_identity()
        assert symbol._frame_index == index
        for theme in (UiTheme.PORCELAIN, UiTheme.SYSTEM, UiTheme.GRAPHITE):
            app.theme = theme.textual_name
            await settle(pilot)
            assert not symbol.animation_running
            assert symbol._timer is None
            semantic = Color.parse(app.get_css_variables()["text-dim"])
            expected = semantic if semantic.ansi is not None else semantic.with_alpha(0.28)
            assert symbol.styles.color == expected
            clock.now = 100.0
            assert symbol.activate()
            clock.seek(1600)


async def test_click_preserves_composer_input_paste_and_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        prompt = app.query_one(PromptInput)
        clock = RevealClock(symbol, monkeypatch)
        assert not symbol.can_focus
        prompt.blur()
        await pilot.click(symbol)
        assert symbol.animation_running
        assert app.focused is prompt
        await pilot.press("中", "文", "shift+enter", "a")
        prompt.post_message(events.Paste("\n多行\n粘贴"))
        await settle(pilot)
        assert prompt.value == "中文\na\n多行\n粘贴"
        assert app.focused is prompt
        clock.seek(1600)
        await pilot.press("enter")
        await settle(pilot)
        assert prompt.value == ""
        assert not symbol.display
        assert symbol._timer is None


@pytest.mark.parametrize(
    "level",
    [
        TerminalColorLevel.ANSI16,
        TerminalColorLevel.UNKNOWN,
        TerminalColorLevel.TRUECOLOR,
        TerminalColorLevel.ANSI256,
    ],
)
@pytest.mark.parametrize("no_color", [False, True])
async def test_capability_policy_static_fallback(level: TerminalColorLevel, no_color: bool) -> None:
    app = make_app(
        UiTheme.GRAPHITE, fixture="empty-conversation", terminal_palette=TerminalPalette(level)
    )
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        assert not symbol.activate()  # deterministic headless policy
        app.no_color = no_color
        with (
            patch.object(
                NeuroCodeApp, "is_headless", new_callable=PropertyMock, return_value=False
            ),
            patch.object(
                type(app.console), "color_system", new_callable=PropertyMock, return_value=None
            ),
        ):
            allowed = not no_color and level in {
                TerminalColorLevel.TRUECOLOR,
                TerminalColorLevel.ANSI256,
            }
            assert symbol.activate() == allowed
            symbol.cancel_animation()


@pytest.mark.parametrize("color_system", ["truecolor", "256", "standard", None])
async def test_native_output_capability_is_independent_of_palette_detection(
    color_system: str | None,
) -> None:
    app = make_app(UiTheme.SYSTEM, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        app.no_color = False
        symbol = app.query_one(EmptyStateIdentity)
        with (
            patch.object(
                NeuroCodeApp, "is_headless", new_callable=PropertyMock, return_value=False
            ),
            patch.object(
                type(app.console),
                "color_system",
                new_callable=PropertyMock,
                return_value=color_system,
            ),
        ):
            assert symbol.activate() == (color_system in {"truecolor", "256"})
            symbol.cancel_animation()


async def test_real_one_shot_timer_completes_and_shutdown_releases_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        monkeypatch.setattr(symbol, "_motion_allowed", lambda: True)
        assert symbol.activate()
        await pilot.pause(TOTAL_DURATION_MS / 1000 + 0.2)
        assert not symbol.animation_running
        assert symbol._timer is None
        assert symbol.activate()
        timer = symbol._timer
    assert not symbol.animation_running
    assert symbol._timer is None
    assert timer is not None
    assert timer._task is None


async def test_frame_updates_are_widget_only_repaints(monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        clock = RevealClock(symbol, monkeypatch)
        with patch.object(symbol, "update") as update, patch.object(symbol, "refresh") as refresh:
            symbol.activate()
            clock.seek(1600)
            symbol.cancel_animation()
            update.assert_not_called()
            assert refresh.call_count == 2
            assert all(call.kwargs == {"layout": False} for call in refresh.call_args_list)


async def test_delayed_callback_skips_samples_without_queueing_catch_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        clock = RevealClock(symbol, monkeypatch)
        assert symbol.activate()
        with patch.object(symbol, "refresh") as refresh:
            clock.seek(3000)
            assert symbol._frame_index == 72
            assert len(clock.timers) == 2
            assert 0 < clock.timers[-1].delay <= 0.042
            refresh.assert_called_once_with(layout=False)
            rendered = symbol.render()
            assert isinstance(rendered, Text)
            cached_styles = symbol._frame_styles
            clock.seek(3042)
            symbol.render()
            assert symbol._frame_styles is cached_styles
            assert clock.timers[-2].stop.called
        symbol.cancel_animation()
        assert symbol._frame_styles is None


def test_offline_vortex_projection_is_structural_and_returns_exactly() -> None:
    from scripts.generate_empty_vortex import particles, project, sample

    points = particles(LOGO_ROWS["large"])
    assert {point[4] for point in points} == {0, 1, 2}
    for point in points:
        assert project(point, 0) == (point[0], point[1], 0)
        assert project(point, 1) == (point[0], point[1], 0)
    for size, rows in LOGO_ROWS.items():
        for index in (0, 32, 72, 111, 136, 144):
            generated, levels = sample(rows, index / 144)
            assert generated == VORTEX_FRAMES[index].rows[size]
            assert levels == VORTEX_FRAMES[index].levels[size]
        assert sample(rows, -1)[0] == sample(rows, 2)[0] == rows
        # Geometry genuinely changes independently of brightness metadata.
        assert sample(rows, 0.5)[0] != rows


async def test_player_captures_real_shell_geometry_and_semantic_palette() -> None:
    from tests.visual.empty_reveal.preview import capture_player

    player = await capture_player(UiTheme.GRAPHITE, (120, 40))
    assert player["size"] == "large"
    assert len(player["palette"]) == 16
    assert len(set(player["palette"])) > 10
    assert 'id="terminal-grid"' in player["shell"]
    assert not any(0x2800 <= ord(ch) <= 0x28FF for ch in player["shell"])
    assert player["origin"] == pytest.approx({"x": 536.8, "y": 264.0, "dx": 12.2, "dy": 24.4})
