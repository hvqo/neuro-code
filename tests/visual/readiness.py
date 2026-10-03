"""Deterministic screenshot readiness for Textual visual fixtures."""

from __future__ import annotations

import asyncio

from neuro_code.interfaces.tui.app import NeuroCodeApp


async def wait_for_screenshot_readiness(app: NeuroCodeApp) -> None:
    """Await layout, overlay synchronization, and the resulting refresh."""

    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def synchronize_overlay() -> None:
        app._sync_empty_identity()

        def mark_ready() -> None:
            if not ready.done():
                ready.set_result(None)

        app.call_after_refresh(mark_ready)

    app.call_after_refresh(synchronize_overlay)
    await ready
