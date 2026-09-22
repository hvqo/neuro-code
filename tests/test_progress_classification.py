"""Trusted progress classification tests.

可信进展分类测试.

Read-only shell inspection may legitimately make progress even though Bash is
side-effecting-capable, but non-empty output alone must never prove read-only
inspection.  These tests pin both halves of that contract.
"""

from __future__ import annotations

import unittest

from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeReport,
    WorkspaceFileChange,
)
from neuro_code.application.runtime.supervision import AgentExecutionSupervisor, SupervisionMode
from neuro_code.application.runtime.tool_pipeline import ToolObservationBuilder
from neuro_code.domain.execution import ProgressKind, SupervisorDecisionKind
from neuro_code.domain.tools import ToolResult
from tests.test_execution_supervision import Clock, execute_tool, execution_budget, observation


class _BashTool:
    side_effecting = True


class _ReadOnlyTool:
    side_effecting = False


def classify_bash(command: str, content: str = "useful output") -> ProgressKind:
    return (
        ToolObservationBuilder(())
        .build(
            tool_name="bash",
            arguments={"command": command},
            result=ToolResult(content),
            tool=_BashTool(),
            change_report=None,
            plan_fingerprint_before=None,
            current_plan_fingerprint=None,
            tool_call_id="call-1",
            verification_eligible=False,
        )
        .progress_kind
    )


class TrustedProgressClassificationTests(unittest.TestCase):
    def test_structured_read_only_tool_still_produces_evidence(self) -> None:
        value = ToolObservationBuilder(()).build(
            tool_name="read_file",
            arguments={"path": "src/app.py"},
            result=ToolResult("def main():\n    return 1"),
            tool=_ReadOnlyTool(),
            change_report=None,
            plan_fingerprint_before=None,
            current_plan_fingerprint=None,
            tool_call_id="call-1",
            verification_eligible=False,
        )

        self.assertIs(value.progress_kind, ProgressKind.EVIDENCE)

    def test_trusted_read_only_bash_inspection_produces_evidence(self) -> None:
        for command in (
            "grep -n textual pyproject.toml",
            "rg -n pattern src",
            "cat README.md",
            "head -20 notes.txt",
            "tail -5 log.txt",
            "wc -l src/app.py",
            "git status",
            "git diff",
            "git log --oneline",
            "git show HEAD",
            "git rev-parse HEAD",
        ):
            with self.subTest(command=command):
                self.assertIs(classify_bash(command), ProgressKind.EVIDENCE)

    def test_pipeline_of_trusted_read_only_segments_produces_evidence(self) -> None:
        self.assertIs(classify_bash("grep -n a file | wc -l"), ProgressKind.EVIDENCE)

    def test_unknown_or_ambiguous_bash_does_not_become_evidence(self) -> None:
        for command in (
            'python -c "print(1)"',
            "curl https://example.com",
            "mystery-command --flag",
            "cat notes.txt > copy.txt",
            "grep -n a file && grep -n b file",
            "grep -n a file; wc -l file",
        ):
            with self.subTest(command=command):
                self.assertIs(classify_bash(command), ProgressKind.NONE)

    def test_mutating_shell_output_alone_is_not_progress(self) -> None:
        for command in ("mv a b", "rm -rf build", "sed -i s/a/b/ note.txt"):
            with self.subTest(command=command):
                self.assertIs(classify_bash(command, content="done"), ProgressKind.NONE)

    def test_actual_workspace_mutation_remains_mutation_progress(self) -> None:
        value = ToolObservationBuilder(()).build(
            tool_name="apply_patch",
            arguments={"path": "note.txt"},
            result=ToolResult("patched note.txt"),
            tool=_BashTool(),
            change_report=WorkspaceChangeReport(
                files=(
                    WorkspaceFileChange(
                        path="note.txt",
                        status="modified",
                        additions=1,
                        deletions=0,
                    ),
                ),
                omitted_files=0,
                scan_limited=False,
            ),
            plan_fingerprint_before=None,
            current_plan_fingerprint=None,
            tool_call_id="call-1",
            verification_eligible=False,
        )

        self.assertIs(value.progress_kind, ProgressKind.WORKSPACE)

    def test_verification_remains_verification(self) -> None:
        value = ToolObservationBuilder(()).build(
            tool_name="bash",
            arguments={"command": "pytest -q tests/unit"},
            result=ToolResult("2 passed", metadata={"exit_code": 0}),
            tool=_BashTool(),
            change_report=None,
            plan_fingerprint_before=None,
            current_plan_fingerprint=None,
            tool_call_id="call-1",
        )

        self.assertIs(value.progress_kind, ProgressKind.VERIFICATION)

    def test_repeated_identical_trusted_inspection_still_marks_stuck(self) -> None:
        supervisor = AgentExecutionSupervisor(
            execution_budget(),
            clock=Clock(),
            mode=SupervisionMode.ENFORCE,
        )
        supervisor.start_turn()
        item = observation(
            "bash",
            {"command": "grep -n needle file"},
            "same output",
            progress_kind=ProgressKind.EVIDENCE,
        )

        self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.CONTINUE)
        self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.REPLAN)
        self.assertIs(execute_tool(supervisor, item), SupervisorDecisionKind.MARK_STUCK)


if __name__ == "__main__":
    unittest.main()
