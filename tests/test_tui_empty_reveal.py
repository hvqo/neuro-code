"""Bounded, deterministic reveal: presentation cannot own input or durable truth."""

from __future__ import annotations

from unittest.mock import Mock, PropertyMock, patch

import pytest
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events
from textual.color import Color
from textual.timer import Timer

from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.empty_state import LOGO_SIZES, EmptyStateIdentity
from neuro_code.interfaces.tui.empty_state_logo import LOGO_ROWS
from neuro_code.interfaces.tui.empty_state_reveal import TORSION_LOCK, TOTAL_DURATION_MS
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
        clock.seek(sum(frame.duration_ms for frame in TORSION_LOCK[:9]))
        peak = symbol.render()
        assert isinstance(peak, Text)
        assert isinstance(peak.style, Style)
        assert peak.style.dim is False
        assert (
            peak.style.color
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
def test_selected_e_assets_are_fixed_geometry_and_exact_resting(size: str) -> None:
    assert len(TORSION_LOCK) == 12
    assert TOTAL_DURATION_MS == 6000
    assert [f.duration_ms for f in TORSION_LOCK] == [
        400,
        350,
        350,
        450,
        600,
        500,
        400,
        350,
        400,
        800,
        600,
        800,
    ]
    width, height = LOGO_SIZES[size]
    for frame in TORSION_LOCK:
        assert len(frame.rows[size]) == height
        assert all(cell_len(row) == width for row in frame.rows[size])
        assert all(
            ch == " " or 0x2800 <= ord(ch) <= 0x28FF for row in frame.rows[size] for ch in row
        )
    assert TORSION_LOCK[0].rows[size] == TORSION_LOCK[-1].rows[size] == LOGO_ROWS[size]
    assert len({frame.rows[size] for frame in TORSION_LOCK}) == 9
    with pytest.raises(TypeError):
        TORSION_LOCK[1].rows[size] = ()  # type: ignore[index]


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
            for index, frame in enumerate(TORSION_LOCK):
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
        with patch.object(
            NeuroCodeApp, "is_headless", new_callable=PropertyMock, return_value=False
        ):
            allowed = not no_color and level in {
                TerminalColorLevel.TRUECOLOR,
                TerminalColorLevel.ANSI256,
            }
            assert symbol.activate() == allowed
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
