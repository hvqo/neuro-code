"""Nonblocking, OS-owned arbitration for one database/invocation execution.

The lock is liveness evidence, not permission or dispatch authority. Keep its
inode after release: unlinking it could let competing processes lock different
files for the same invocation. Process exit releases the OS lock automatically.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from neuro_code.application.ports.workflow_state import WorkflowStateError

_executing: ContextVar[tuple[Path, str, object] | None] = ContextVar(
    "workflow_adoption_execution_lock", default=None
)


def owns_execution_lock(database: Path, invocation_id: str) -> bool:
    """Only the task holding first execution may mark its own uncertain ceiling.

    This avoids reacquiring its own OS lock. It is not used to authorize public
    reconciliation, and inherited context in a child task does not confer ownership.
    """
    return _executing.get() == (
        database.resolve(strict=True),
        invocation_id,
        asyncio.current_task(),
    )


@contextmanager
def adoption_execution_lock(
    database: Path, invocation_id: str, *, execution: bool = False
) -> Iterator[None]:
    """Protect first dispatch or recovery, without waiting or SQLite lock time."""
    database = database.resolve(strict=True)
    directory = database.parent / (database.name + ".workflow-adoption-locks")
    directory.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest() + ".lock"
    descriptor = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    token = None
    try:
        if sys.platform == "win32":
            import msvcrt

            # Windows byte-range locks need one stable byte and offset zero.
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        if execution:
            token = _executing.set((database, invocation_id, asyncio.current_task()))
        yield
    except OSError as error:
        if not acquired and error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise WorkflowStateError(
                "ADOPT execution is still active", kind="concurrent_execution"
            ) from error
        raise
    finally:
        if token is not None:
            _executing.reset(token)
        # Closing releases flock/Windows byte-range ownership, including on
        # cancellation and failure. Never remove the persistent lock file.
        os.close(descriptor)
