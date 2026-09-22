#!/usr/bin/env python3
"""Generate or verify the volatile facts block in ``docs/agent_memory/README.md``.

``docs/agent_memory`` 是本地的、被 Git 忽略的交接记忆。它过去腐烂的原因是:
**易变事实(分支、HEAD、schema 版本、ADR 范围)被手写**,于是没人更新也无人察觉。

因此这些事实改由本脚本从 Git 与源码生成,写在两个标记之间;叙述部分仍然手写,但不再
包含会过期的数字。旧记忆的时间线归档不再保留——现状只在 README.md,历史看 git log 与 ADR。

Usage::

    python scripts/agent_memory_status.py --write   # 刷新事实块
    python scripts/agent_memory_status.py --check   # 校验事实块是否与仓库一致
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MEMORY_README = _REPOSITORY_ROOT / "docs" / "agent_memory" / "README.md"

_BEGIN = "<!-- BEGIN GENERATED FACTS -->"
_END = "<!-- END GENERATED FACTS -->"

_SCHEMA_CONSTANTS = (
    _REPOSITORY_ROOT
    / "src"
    / "neuro_code"
    / "infrastructure"
    / "persistence"
    / "sqlite_session_constants.py"
)
_VERSION_MODULE = _REPOSITORY_ROOT / "src" / "neuro_code" / "_version.py"
_ADR_DIRECTORY = _REPOSITORY_ROOT / "docs" / "en" / "adr"


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _facts() -> dict[str, str]:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("log", "-1", "--format=%h %s")
    counts = _git("rev-list", "--left-right", "--count", "origin/main...HEAD").split()
    behind, ahead = [*counts, "?", "?"][:2]

    version_match = re.search(
        r'__version__ = "([^"]+)"', _VERSION_MODULE.read_text(encoding="utf-8")
    )
    schema_match = re.search(
        r"^SCHEMA_VERSION = (\d+)\s*$",
        _SCHEMA_CONSTANTS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if version_match is None or schema_match is None:
        raise SystemExit("cannot read the package version or SCHEMA_VERSION")

    numbers = sorted(path.name[:4] for path in _ADR_DIRECTORY.glob("*.md"))
    worktrees = len([line for line in _git("worktree", "list").splitlines() if line.strip()])
    generated = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return {
        "生成时间": generated,
        "当前分支": f"`{branch}`",
        "HEAD": f"`{head}`",
        "相对 origin/main": f"ahead {ahead} / behind {behind}",
        "包版本": f"`{version_match.group(1)}`",
        "Session schema": f"`{schema_match.group(1)}`",
        "ADR": f"{len(numbers)} 篇, 编号 {numbers[0]}-{numbers[-1]}",
        "本地工作树": str(worktrees),
    }


def _render(facts: dict[str, str]) -> str:
    lines = [_BEGIN, "", "| 事实 | 值 |", "|---|---|"]
    lines.extend(f"| {name} | {value} |" for name, value in facts.items())
    lines.extend(
        [
            "",
            "> 本块由 `python scripts/agent_memory_status.py --write` 生成; 请勿手写。",
            _END,
        ]
    )
    return "\n".join(lines)


def _block(text: str) -> str | None:
    start = text.find(_BEGIN)
    end = text.find(_END)
    if start == -1 or end == -1:
        return None
    return text[start : end + len(_END)]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    write = "--write" in arguments
    check = "--check" in arguments
    if write == check:
        print(__doc__, file=sys.stderr)
        return 2
    if not _MEMORY_README.is_file():
        print(f"{_MEMORY_README} does not exist", file=sys.stderr)
        return 1

    text = _MEMORY_README.read_text(encoding="utf-8")
    current = _block(text)
    if current is None:
        print(f"{_MEMORY_README}: generated facts markers are missing", file=sys.stderr)
        return 1

    rendered = _render(_facts())
    if write:
        _MEMORY_README.write_text(text.replace(current, rendered), encoding="utf-8")
        print(f"agent memory facts refreshed: {_MEMORY_README}")
        return 0

    # The generation timestamp always differs, so only the substantive rows are
    # compared; a stale timestamp alone must not fail the gate.
    def substantive(block: str) -> list[str]:
        return [line for line in block.splitlines() if not line.startswith("| 生成时间 |")]

    if substantive(current) != substantive(rendered):
        print(
            f"{_MEMORY_README}: generated facts are stale; "
            "run 'python scripts/agent_memory_status.py --write'",
            file=sys.stderr,
        )
        for line in substantive(rendered):
            if line not in current:
                print(f"  expected {line}", file=sys.stderr)
        return 1
    print("agent memory facts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
