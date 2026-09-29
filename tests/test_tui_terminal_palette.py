from __future__ import annotations

import os
import select
import threading
import time
from io import StringIO
from types import SimpleNamespace

import pytest

from neuro_code.interfaces.tui.terminal_palette import (
    TerminalColorLevel,
    TerminalPalette,
    _probe_default_colors,
    parse_osc_default_colors,
    probe_terminal_palette,
    resolve_system_palette,
)
from neuro_code.interfaces.tui.theme import markdown_theme, textual_theme_for
from neuro_code.shared.ui_theme import UiTheme
from tests.visual.showcases import make_app, populate_fixture


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _luminance(color: str) -> float:
    channels = (_rgb(color)[index] / 255 for index in range(3))
    linear = (
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    )
    red, green, blue = linear
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_osc_default_color_parser_accepts_ordered_and_reversed_replies() -> None:
    expected = ((238, 238, 238), (26, 26, 26))
    first = b"\x1b]10;rgb:eeee/eeee/eeee\x1b\\\x1b]11;rgb:1a1a/1a1a/1a1a\x07"
    reversed_replies = b"\x1b]11;rgb:1a/1a/1a\x07\x1b]10;rgb:ee/ee/ee\x1b\\"

    assert parse_osc_default_colors(first) == expected
    assert parse_osc_default_colors(reversed_replies) == expected
    assert parse_osc_default_colors(b"\x1b]10;rgb:eeee/eeee/eeee\x07") is None


def test_palette_probe_fails_soft_without_interactive_terminal() -> None:
    palette = probe_terminal_palette(stdin=StringIO(), stdout=StringIO())

    assert palette.foreground is None
    assert palette.background is None
    assert palette.color_level is TerminalColorLevel.UNKNOWN


@pytest.mark.skipif(os.name != "posix", reason="PTY palette probing requires POSIX")
def test_terminal_probe_reads_osc_pair_and_restores_terminal_mode() -> None:
    import pty
    import termios

    master_fd, slave_fd = pty.openpty()
    original_mode = termios.tcgetattr(slave_fd)
    requests = bytearray()

    def emulate_terminal() -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.05)
            if readable:
                requests.extend(os.read(master_fd, 512))
                if b"\x1b]11;?\x1b\\" in requests:
                    os.write(
                        master_fd,
                        b"\x1b]10;rgb:eeee/eeee/eeee\x1b\\\x1b]11;rgb:1a1a/1a1a/1a1a\x1b\\",
                    )
                    return

    responder = threading.Thread(target=emulate_terminal, daemon=True)
    responder.start()
    try:
        colors = _probe_default_colors(slave_fd, slave_fd, timeout_seconds=0.5)
        responder.join(timeout=1.0)
        assert colors == ((238, 238, 238), (26, 26, 26))
        assert b"\x1b]10;?\x1b\\" in requests
        assert b"\x1b]11;?\x1b\\" in requests
        assert termios.tcgetattr(slave_fd) == original_mode
    finally:
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.parametrize(
    ("palette", "mode", "accent"),
    [
        (
            TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30)),
            "dark",
            "#69AEE5",
        ),
        (
            TerminalPalette(TerminalColorLevel.ANSI256, (24, 24, 24), (246, 246, 246)),
            "light",
            "#005F87",
        ),
    ],
)
def test_detected_dark_and_light_palettes_derive_separate_surfaces(
    palette: TerminalPalette, mode: str, accent: str
) -> None:
    resolved = resolve_system_palette(palette)

    assert resolved.adaptive
    assert resolved.mode == mode
    assert resolved.canvas != resolved.surface
    assert resolved.surface != resolved.surface_subtle
    assert resolved.surface_subtle != resolved.composer_surface
    assert resolved.composer_surface != resolved.user_message_surface
    assert resolved.user_message_surface != resolved.surface_selected
    assert resolved.border_normal != resolved.canvas
    assert resolved.border_focus == accent
    assert resolved.text_primary != resolved.text_secondary != resolved.text_muted
    for surface in (
        resolved.canvas,
        resolved.surface,
        resolved.surface_subtle,
        resolved.surface_selected,
        resolved.composer_surface,
        resolved.user_message_surface,
    ):
        assert _contrast(resolved.text_secondary, surface) >= 6.0
        assert _contrast(resolved.text_muted, surface) >= 4.5


@pytest.mark.parametrize(
    "palette",
    [
        TerminalPalette(),
        TerminalPalette(TerminalColorLevel.ANSI16, (238, 238, 238), (30, 30, 30)),
        TerminalPalette(TerminalColorLevel.TRUECOLOR, (40, 40, 40), (30, 30, 30)),
    ],
)
def test_unknown_or_low_capability_palette_falls_back_to_visible_terminal_roles(
    palette: TerminalPalette,
) -> None:
    resolved = resolve_system_palette(palette)

    assert not resolved.adaptive
    assert resolved.mode == "unknown"
    assert resolved.canvas == "ansi_default"
    assert resolved.composer_surface == "ansi_default"
    assert resolved.composer_surface == resolved.canvas
    assert resolved.border_normal == "ansi_white"
    assert resolved.border_focus == "ansi_bright_blue"
    assert resolved.text_secondary == resolved.text_muted == "ansi_default"


def test_system_textual_theme_projects_adaptive_roles_without_bright_black() -> None:
    palette = TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30))
    theme = textual_theme_for(UiTheme.SYSTEM, palette)
    variables = theme.variables

    assert theme.dark
    assert theme.background == "#1E1E1E"
    assert theme.surface != theme.background
    assert variables["composer-surface"] != theme.background
    assert variables["user-message-surface"] != variables["composer-surface"]
    assert variables["border-focus"] != variables["border"]
    assert all("bright_black" not in value for value in variables.values())


def test_unknown_system_theme_keeps_inline_code_foreground_only() -> None:
    owner = SimpleNamespace(
        app=SimpleNamespace(theme=UiTheme.SYSTEM.textual_name, terminal_palette=TerminalPalette())
    )

    inline_code = markdown_theme(owner).styles["markdown.code"]

    assert inline_code.color is not None
    assert inline_code.bgcolor is None


def test_adaptive_system_inline_code_keeps_its_background_free_contract() -> None:
    palette = TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30))
    owner = SimpleNamespace(
        app=SimpleNamespace(theme=UiTheme.SYSTEM.textual_name, terminal_palette=palette)
    )
    inline_code = markdown_theme(owner).styles["markdown.code"]

    assert inline_code.color is not None
    assert inline_code.bgcolor is None


@pytest.mark.parametrize(
    "palette",
    [
        TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30)),
        TerminalPalette(),
    ],
)
@pytest.mark.asyncio
async def test_system_composer_has_no_top_rule_and_keeps_geometry_when_focused(
    palette: TerminalPalette,
) -> None:
    app = make_app(UiTheme.SYSTEM, fixture="user-assistant", terminal_palette=palette)
    async with app.run_test(size=(100, 32)) as pilot:
        prompt = app.query_one("#prompt")
        surface = app.query_one("#prompt-surface")
        focused_surface_region = tuple(surface.region)
        focused_prompt_region = tuple(prompt.region)
        assert surface.styles.padding.top == 1
        assert not surface.styles.border_top[0]

        prompt.blur()
        await pilot.pause()
        idle_surface_region = tuple(surface.region)
        idle_prompt_region = tuple(prompt.region)
        assert surface.styles.padding.top == 1
        assert not surface.styles.border_top[0]

        prompt.focus()
        await pilot.pause()
        assert tuple(surface.region) == idle_surface_region == focused_surface_region
        assert tuple(prompt.region) == idle_prompt_region == focused_prompt_region
        assert not surface.styles.border_top[0]


@pytest.mark.asyncio
async def test_adaptive_theme_switch_changes_only_palette_geometry() -> None:
    palette = TerminalPalette(TerminalColorLevel.TRUECOLOR, (232, 232, 232), (30, 30, 30))
    app = make_app(UiTheme.GRAPHITE, fixture="user-assistant", terminal_palette=palette)
    async with app.run_test(size=(100, 32)) as pilot:
        populate_fixture(app, "user-assistant")
        await pilot.pause()
        selectors = (
            "#header",
            "#transcript",
            ".message-user",
            ".message-assistant",
            "#composer",
            "#prompt-surface",
            "#prompt",
        )

        def regions() -> tuple[tuple[int, int, int, int], ...]:
            return tuple(tuple(app.screen.query_one(selector).region) for selector in selectors)

        original = regions()
        app.theme = UiTheme.SYSTEM.textual_name
        await pilot.pause()
        assert regions() == original
