#!/usr/bin/env python3
"""Run every completion check required of an implementation change.

实现改动必须通过的完成检查集中在这里。新增一项门禁只需要修改本文件,而不必同步
六份手写清单;文档只需指向这条命令。

Usage::

    uv run python scripts/check_all.py            # every check
    uv run python scripts/check_all.py --quick    # skip the slow test run and build
    uv run python scripts/check_all.py --no-uv    # skip uv-only steps (sandboxes)

Steps run in the documented order and stop at the first failure unless
``--keep-going`` is supplied. ``uv``-only steps are reported as skipped, with a
reason, when ``uv`` is unavailable or ``--no-uv`` is given; CI still runs them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_PASS = "pass"
_FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Step:
    """One completion check."""

    name: str
    command: tuple[str, ...]
    slow: bool = False
    requires: str | None = None
    # A local-only input: the step is skipped when the path is absent, so the
    # same command works in CI, which never checks out local handoff memory.
    needs_path: str | None = None


def _steps(python: str) -> tuple[Step, ...]:
    """Return the completion checks in their documented order."""

    return (
        Step("lock", ("uv", "lock", "--check"), requires="uv"),
        Step("docs-parity", (python, "scripts/check_docs_parity.py")),
        Step("docs-consistency", (python, "scripts/check_docs_consistency.py")),
        Step(
            "agent-memory",
            (python, "scripts/agent_memory_status.py", "--check"),
            needs_path="docs/agent_memory/README.md",
        ),
        Step("ruff-check", (python, "-m", "ruff", "check", ".")),
        Step("ruff-format", (python, "-m", "ruff", "format", "--check", ".")),
        Step("mypy", (python, "-m", "mypy")),
        Step(
            "pytest",
            (python, "-m", "pytest", "--cov=neuro_code", "--cov-report=term-missing"),
            slow=True,
        ),
        Step("build", ("uv", "build"), slow=True, requires="uv"),
    )


def _run(step: Step) -> str:
    print(f"\n=== {step.name}: {' '.join(step.command)}", flush=True)
    completed = subprocess.run(
        step.command,
        cwd=_REPOSITORY_ROOT,
        check=False,
    )
    return _PASS if completed.returncode == 0 else _FAIL


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    quick = "--quick" in arguments
    keep_going = "--keep-going" in arguments
    no_uv = "--no-uv" in arguments
    unknown = [
        argument for argument in arguments if argument not in {"--quick", "--keep-going", "--no-uv"}
    ]
    if unknown:
        print(f"unknown option(s): {' '.join(unknown)}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2

    steps = _steps(sys.executable)
    results: list[tuple[str, str]] = []
    for index, step in enumerate(steps):
        if quick and step.slow:
            results.append((step.name, "skipped (--quick)"))
            continue
        if step.requires is not None and (no_uv or shutil.which(step.requires) is None):
            reason = "--no-uv" if no_uv else f"{step.requires} is not installed"
            results.append((step.name, f"skipped ({reason})"))
            continue
        if step.needs_path is not None and not (_REPOSITORY_ROOT / step.needs_path).exists():
            results.append((step.name, f"skipped ({step.needs_path} is absent)"))
            continue
        outcome = _run(step)
        results.append((step.name, outcome))
        if outcome == _FAIL and not keep_going:
            results.extend((later.name, "not run") for later in steps[index + 1 :])
            break

    print("\n=== completion checks")
    for name, outcome in results:
        print(f"  {name:<18} {outcome}")
    failed = [name for name, outcome in results if outcome == _FAIL]
    if failed:
        print(f"\ncompletion checks failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("\ncompletion checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
