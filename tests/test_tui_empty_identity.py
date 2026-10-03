"""Static empty identity lifecycle, geometry and deterministic rendering."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from rich.cells import cell_len
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widgets import Static

from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.empty_state import EmptyStateIdentity, logo_layout
from neuro_code.interfaces.tui.empty_state_logo import LOGO_ROWS, SOURCE_SHA256
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.ui_theme import UiTheme
from tests.test_tui_visual_snapshots import canonicalize_svg
from tests.visual.empty_identity.exploration import ASSETS, VARIANTS, IdentityExplorationApp
from tests.visual.readiness import wait_for_shell_geometry_stability
from tests.visual.showcases import make_app

VIEWPORTS = ((120, 40), (100, 32), (80, 24))
THEMES = (UiTheme.SYSTEM, UiTheme.GRAPHITE, UiTheme.PORCELAIN)


def fixed_clock(app: NeuroCodeApp) -> None:
    app.query_one("#clock", Static).update("13:37")


async def settle(pilot: Pilot[NeuroCodeApp]) -> None:
    await wait_for_shell_geometry_stability(pilot.app)


@pytest.mark.parametrize("size", ["large", "medium", "small"])
@pytest.mark.parametrize("variant", VARIANTS)
def test_static_terminal_assets_are_bounded_and_single_cell(size: str, variant: str) -> None:
    rows = ASSETS["sizes"][size][variant]["core"]
    assert 8 <= len(rows) <= 16
    assert 16 <= len(rows[0]) <= 32
    assert all(len(row) == len(rows[0]) for row in rows)
    assert all(cell_len(character) == 1 for row in rows for character in row)
    assert sum(character != " " for row in rows for character in row) > 0
    assert all(
        character == " " or 0x2800 <= ord(character) <= 0x28FF for row in rows for character in row
    )


def test_insufficient_room_hides_instead_of_pressuring_composer() -> None:
    assert logo_layout(Region(3, 2, 54, 13), (60, 20)) is None
    assert logo_layout(Region(3, 2, 114, 8), (120, 40)) is None


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("viewport", VIEWPORTS)
async def test_identity_is_centered_without_changing_shell_geometry(
    theme: UiTheme, viewport: tuple[int, int]
) -> None:
    app = make_app(theme, fixture="empty-conversation")
    async with app.run_test(size=viewport) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        content = app.query_one("#transcript").content_region
        expected = {
            name: app.query_one(f"#{name}").region
            for name in ("header", "transcript", "prompt-surface", "runtime-bar")
        }
        assert symbol.display
        assert content.contains_region(symbol.region)
        assert abs(symbol.region.center[0] - content.center[0]) <= 1
        assert abs(symbol.region.center[1] - content.center[1]) <= 1
        assert app.query_one("#transcript").max_scroll_y == 0
        assert not app._entries
        assert not app._entry_widgets
        symbol.display = False
        await settle(pilot)
        assert all(app.query_one(f"#{name}").region == region for name, region in expected.items())


async def test_first_user_and_streaming_assistant_content_remove_identity_immediately() -> None:
    for category in ("user", "assistant-stream"):
        app = make_app(UiTheme.SYSTEM, fixture="empty-conversation")
        async with app.run_test(size=VIEWPORTS[0]) as pilot:
            await settle(pilot)
            symbol = app.query_one(EmptyStateIdentity)
            assert symbol.display
            if category == "user":
                app._write_entry("user", "First message")
            else:
                app._update_pending_assistant("Streaming evidence")
            assert not symbol.display
            await settle(pilot)
            await pilot.resize_terminal(*VIEWPORTS[2])
            await settle(pilot)
            assert not symbol.display


async def test_restored_history_does_not_show_identity_or_modify_restored_entries() -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    items = (Message(Role.USER, "Original question"), Message(Role.ASSISTANT, "Original answer"))
    async with app.run_test(size=VIEWPORTS[1]) as pilot:
        app._replace_transcript(items)
        await settle(pilot)
        assert not app.query_one(EmptyStateIdentity).display
        assert [(entry.category, entry.text) for entry in app._entries] == [
            ("user", "Original question"),
            ("assistant", "Original answer"),
        ]
        # Switching the presentation back to an empty conversation clears the gate.
        app._replace_transcript(())
        await settle(pilot)
        assert app.query_one(EmptyStateIdentity).display


@pytest.mark.parametrize("theme", THEMES)
async def test_resize_large_compact_large_and_static_screenshot_are_deterministic(
    theme: UiTheme,
) -> None:
    app = make_app(theme, fixture="empty-conversation")
    with patch.object(NeuroCodeApp, "_update_clock", fixed_clock):
        async with app.run_test(size=VIEWPORTS[0]) as pilot:
            await settle(pilot)
            original = app.query_one(EmptyStateIdentity).region
            before = canonicalize_svg(app.export_screenshot(title="Identity test"))
            await pilot.resize_terminal(*VIEWPORTS[2])
            await settle(pilot)
            assert app.query_one(EmptyStateIdentity).asset_size == "small"
            assert app.query_one("#transcript").max_scroll_y == 0
            await pilot.resize_terminal(*VIEWPORTS[0])
            await settle(pilot)
            assert app.query_one(EmptyStateIdentity).region == original
            assert canonicalize_svg(app.export_screenshot(title="Identity test")) == before
            await pilot.resize_terminal(60, 20)
            await settle(pilot)
            assert not app.query_one(EmptyStateIdentity).display


async def test_draft_growth_recenters_without_layout_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(UiTheme.SYSTEM, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        symbol = app.query_one(EmptyStateIdentity)
        before = symbol.region
        prompt = app.query_one("#prompt", PromptInput)
        drafts = {
            "\n".join(["中文 draft"] * 12): asyncio.Event(),
            "": asyncio.Event(),
        }
        sync_content_height = PromptInput.sync_content_height

        def sync_and_signal(text_area: PromptInput) -> None:
            sync_content_height(text_area)
            if text_area.text in drafts:
                drafts[text_area.text].set()

        monkeypatch.setattr(PromptInput, "sync_content_height", sync_and_signal)

        prompt.text = next(text for text in drafts if text)
        await asyncio.wait_for(drafts[prompt.text].wait(), timeout=1)
        await pilot.pause()
        await settle(pilot)
        assert prompt.region.height == 8
        content = app.query_one("#transcript").content_region
        assert symbol.region != before
        assert abs(symbol.region.center[1] - content.center[1]) <= 1
        assert content.contains_region(symbol.region)
        assert app.query_one("#transcript").max_scroll_y == 0
        prompt.text = ""
        await asyncio.wait_for(drafts[""].wait(), timeout=1)
        await pilot.pause()
        await settle(pilot)
        assert prompt.region.height == 1
        assert symbol.region == before


@pytest.mark.parametrize("category", ["tool", "error"])
async def test_activity_is_not_a_watermark(category: str) -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        app._write_entry(category, "Activity")
        assert not app.query_one(EmptyStateIdentity).display


async def test_startup_resume_is_hidden_before_first_frame() -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    app._initial_items = (
        Message(Role.USER, "Saved question"),
        Message(Role.ASSISTANT, "Saved answer"),
    )
    with patch.object(app._runner, "session_id", "saved-session"):
        async with app.run_test(size=VIEWPORTS[0]) as pilot:
            await settle(pilot)
            assert not app.query_one(EmptyStateIdentity).display
            assert app._entries[0].text == "Saved question"


async def test_status_notice_does_not_consume_empty_identity() -> None:
    app = make_app(UiTheme.SYSTEM, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        app._write_entry("system", "Session ready")
        await settle(pilot)
        assert app.query_one(EmptyStateIdentity).display
        app._replace_transcript((Message(Role.SYSTEM, "System instructions"),))
        await settle(pilot)
        assert app.query_one(EmptyStateIdentity).display


async def test_modal_does_not_reset_history_latch() -> None:
    from textual.screen import ModalScreen

    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        app.push_screen(ModalScreen())
        await settle(pilot)
        app._write_entry("user", "Message while screen is covered")
        await app.pop_screen()
        await settle(pilot)
        assert not app.query_one(EmptyStateIdentity).display


async def test_production_uses_corrected_core_without_badge() -> None:
    app = make_app(UiTheme.GRAPHITE, fixture="empty-conversation")
    async with app.run_test(size=VIEWPORTS[0]) as pilot:
        await settle(pilot)
        identity = app.query_one(EmptyStateIdentity)
        assert identity.rows is LOGO_ROWS
        assert identity.display
        assert ASSETS["source_sha256"] == SOURCE_SHA256
        assert {
            size: tuple(content["B"]["core"]) for size, content in ASSETS["sizes"].items()
        } == LOGO_ROWS
        assert all(
            LOGO_ROWS[size] != tuple(content["A"]["core"])
            for size, content in ASSETS["sizes"].items()
        )


def test_corrected_source_provenance_and_sparse_hybrid() -> None:
    assert (
        ASSETS["source_sha256"]
        == "8334fe13506ca923f09b16933c6c7e5713346d349ada6eb0675ef97021398a6e"
    )
    assert ASSETS["source_size"] == [212, 212]
    for content in ASSETS["sizes"].values():
        assert content["A"]["core"] != content["B"]["core"]
        assert content["C"]["core"] != content["B"]["core"]
        for variant in VARIANTS:
            for core, outline in zip(
                content[variant]["core"], content[variant]["outline"], strict=True
            ):
                assert len(core) == len(outline)
                assert all(cell_len(character) == 1 for character in outline)

        # Hybrid outline must remain much sparser than the full badge.
        def ink(rows: list[str]) -> int:
            return sum(
                (ord(character) - 0x2800).bit_count()
                for row in rows
                for character in row
                if character != " "
            )

        assert 0 < ink(content["C"]["outline"]) < ink(content["A"]["core"]) // 3


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("theme", THEMES)
async def test_resting_peak_return_is_static_and_byte_stable(variant: str, theme: UiTheme) -> None:
    app = IdentityExplorationApp(variant, theme)
    with patch.object(NeuroCodeApp, "_update_clock", fixed_clock):
        async with app.run_test(size=VIEWPORTS[0]) as pilot:
            await settle(pilot)
            symbol = app.query_one(EmptyStateIdentity)
            original_region = symbol.region
            resting = canonicalize_svg(app.export_screenshot(title="Identity state"))
            assert symbol.styles.text_style.dim
            app.set_peak_preview(True)
            await settle(pilot)
            peak = canonicalize_svg(app.export_screenshot(title="Identity state"))
            assert peak != resting
            assert symbol.region == original_region
            app.set_peak_preview(False)
            await settle(pilot)
            assert canonicalize_svg(app.export_screenshot(title="Identity state")) == resting
