"""Nonblocking local SQLite/invocation execution arbitration for VERIFY."""

from __future__ import annotations

import errno
import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from neuro_code.application.ports.workflow_state import WorkflowStateError


@contextmanager
def verification_execution_lock(database: Path, invocation_id: str) -> Iterator[None]:
    database = database.resolve(strict=True)
    directory = database.parent / (database.name + ".workflow-verification-locks")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (hashlib.sha256(invocation_id.encode()).hexdigest() + ".lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError as error:
        if not acquired and error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise WorkflowStateError(
                "VERIFY execution is active", kind="concurrent_execution"
            ) from error
        raise
    finally:
        os.close(descriptor)  # Never unlink: all processes must keep the same inode.
