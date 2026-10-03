"""Platform-aware comparisons for terminal attributes returned by ``tcgetattr``."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from types import ModuleType


def terminal_mode_signature(
    attributes: Sequence[object], termios: ModuleType, *, platform: str = sys.platform
) -> tuple[object, ...]:
    """Keep every configurable termios field exact, excluding Darwin state bits.

    XNU declares ``PENDIN`` in ``c_lflag`` as kernel state rather than persistent
    user configuration. The macOS runner sets it after tcsetattr even though the
    saved mode was restored. No other field or flag is masked.
    """

    comparable = list(attributes)
    if platform == "darwin":
        transient = int(getattr(termios, "PENDIN", 0))
        comparable[3] = int(comparable[3]) & ~transient
    return tuple(comparable)
