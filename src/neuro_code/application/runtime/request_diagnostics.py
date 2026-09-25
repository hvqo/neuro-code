"""Opt-in request diagnostics metadata shared by application services.

应用服务共用的显式请求诊断元数据。
"""

from __future__ import annotations

import os
import uuid


def prompt_trajectory_opted_in() -> bool:
    """Return whether the user explicitly enabled digest-only wire diagnostics."""

    return os.environ.get("NEURO_PROMPT_TRAJECTORY", "").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def new_trajectory_id(*, enabled: bool | None = None) -> str | None:
    """Create an ephemeral request-group id only while diagnostics are enabled."""

    opted_in = prompt_trajectory_opted_in() if enabled is None else enabled
    if not isinstance(opted_in, bool):
        raise TypeError("enabled must be a bool or None")
    return uuid.uuid4().hex if opted_in else None


__all__ = ["new_trajectory_id", "prompt_trajectory_opted_in"]
