"""Deterministic screenshot readiness for Textual visual fixtures."""

from __future__ import annotations

import asyncio

from textual.widget import Widget

from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.empty_state import EmptyStateIdentity, logo_layout
from neuro_code.interfaces.tui.widgets import PromptInput, TranscriptScroll


async def wait_for_shell_geometry_stability(app: NeuroCodeApp) -> None:
    """Wait for a stable shell layout without mutating overlay state."""
    loop = asyncio.get_running_loop()
    previous_geometry: tuple[object, ...] | None = None
    for _ in range(12):
        ready: asyncio.Future[None] = loop.create_future()
        app.call_after_refresh(ready.set_result, None)
        await ready

        transcript = app._main_screen_query_one("#transcript", TranscriptScroll)
        prompt = app._main_screen_query_one("#prompt", PromptInput)
        composer = app._main_screen_query_one("#composer", Widget)
        identity = app._main_screen_query_optional("#empty-state-identity", EmptyStateIdentity)
        placement = logo_layout(transcript.content_region, (app.size.width, app.size.height))
        identity_aligned = (
            identity is None
            or not identity.display
            or (
                placement is not None
                and identity.asset_size == placement[0]
                and transcript.content_region.contains_region(identity.region)
                and abs(identity.region.center[0] - transcript.content_region.center[0]) <= 1
                and abs(identity.region.center[1] - transcript.content_region.center[1]) <= 1
            )
        )
        geometry = (
            app.size,
            transcript.region,
            transcript.content_region,
            transcript.scroll_y,
            prompt.region,
            composer.region,
            None if identity is None or not identity.display else identity.region,
        )
        if identity_aligned and geometry == previous_geometry:
            return
        previous_geometry = geometry

    raise AssertionError(
        "TUI shell geometry did not settle across refreshes: "
        f"transcript={transcript.region}, content={transcript.content_region}, "
        f"prompt={prompt.region}, composer={composer.region}, "
        f"identity={None if identity is None else identity.region}, "
        f"aligned={identity_aligned}"
    )


async def wait_for_screenshot_readiness(app: NeuroCodeApp) -> None:
    """Await stable shell geometry and its synchronized empty-state overlay."""
    loop = asyncio.get_running_loop()
    previous_geometry: tuple[object, ...] | None = None
    for _ in range(12):
        ready: asyncio.Future[None] = loop.create_future()

        def synchronize_overlay(ready: asyncio.Future[None] = ready) -> None:
            app._sync_empty_identity()

            def mark_ready(ready: asyncio.Future[None] = ready) -> None:
                if not ready.done():
                    ready.set_result(None)

            app.call_after_refresh(mark_ready)

        app.call_after_refresh(synchronize_overlay)
        await ready

        transcript = app._main_screen_query_one("#transcript", TranscriptScroll)
        prompt = app._main_screen_query_one("#prompt", PromptInput)
        composer = app._main_screen_query_one("#composer", Widget)
        identity = app._main_screen_query_optional("#empty-state-identity", EmptyStateIdentity)
        placement = logo_layout(transcript.content_region, (app.size.width, app.size.height))
        identity_aligned = (
            identity is None
            or not identity.display
            or (placement is not None and identity.region == placement[1])
        )
        geometry = (
            app.size,
            transcript.region,
            transcript.content_region,
            transcript.scroll_y,
            prompt.region,
            composer.region,
            None if identity is None or not identity.display else identity.region,
        )
        if identity_aligned and geometry == previous_geometry:
            return
        previous_geometry = geometry

    raise AssertionError(
        "TUI screenshot geometry did not settle across refreshes: "
        f"transcript={transcript.region}, content={transcript.content_region}, "
        f"prompt={prompt.region}, composer={composer.region}, "
        f"identity={None if identity is None else identity.region}"
    )
