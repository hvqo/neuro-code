"""Task-local freshness permit; not a permission grant or OS security boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.interpreter import WorkflowStepOutput

_consuming: ContextVar[tuple[str, str, object] | None] = ContextVar(
    "verified_output_consumption", default=None
)


def owns_fresh_consumption(database: Path, output: WorkflowStepOutput) -> bool:
    return _consuming.get() == (str(database.resolve()), output.fingerprint, asyncio.current_task())


_revalidate: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "verify_consumption_revalidate", default=None
)


async def revalidate_consumption(database: Path, output: WorkflowStepOutput) -> None:
    validate = _revalidate.get()
    if not owns_fresh_consumption(database, output) or validate is None:
        raise WorkflowStateError(
            "VERIFY consumption has no live freshness scope", kind="stale_verification"
        )
    await validate()
