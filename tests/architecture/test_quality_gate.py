from __future__ import annotations

import tomllib
from pathlib import Path

from coverage.results import should_fail_under

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_quality_workflow_enforces_two_decimal_coverage_boundary() -> None:
    pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    report = pyproject["tool"]["coverage"]["report"]
    fail_under = report["fail_under"]
    precision = report["precision"]
    workflow = (_PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert fail_under == 85
    assert precision >= 2
    assert "--cov-fail-under=85" in workflow
    assert should_fail_under(84.99, fail_under, precision)
    assert not should_fail_under(85.00, fail_under, precision)


def test_quality_workflow_keeps_documentation_gates() -> None:
    """The documentation gates must stay part of the required CI quality job.

    文档门禁必须保留在 CI 的 quality job 中:否则它们会在无人察觉时消失,
    复述的易变事实又会与源码漂移。
    """

    workflow = (_PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python scripts/check_docs_parity.py" in workflow
    assert "python scripts/check_docs_consistency.py" in workflow
    assert (_PROJECT_ROOT / "scripts" / "check_docs_consistency.py").is_file()
