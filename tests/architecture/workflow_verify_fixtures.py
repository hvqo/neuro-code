"""Explicit injected VERIFY ports for control-only tests, never production wiring.

These exercise the real evidence protocol. They do not claim to run pytest or
observe a real filesystem; shell/freshness integration lives in VERIFY tests.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from neuro_code.application.ports.workflow_verification import (
    ApprovedWorkflowVerification,
    VerificationWorkspaceEvidence,
)
from neuro_code.domain.tools import ToolExecutionResult
from tests.architecture.test_workflow_projection import END


class FixedWorkspace:
    async def evidence(self, root):
        return VerificationWorkspaceEvidence("fixture", str(root), "fixture-head", "a" * 64)

    async def watch(self, root):
        return self

    async def unchanged(self):
        return True


@dataclass
class FixtureCommand:
    workspace_root: Path
    passed: bool = True

    async def verify_command(self, configuration, *, session_id, pre_entry_guard):
        assert await pre_entry_guard()
        return ToolExecutionResult(
            "fixture",
            "bash",
            "injected result",
            is_error=not self.passed,
            metadata={"exit_code": 0 if self.passed else 1},
        )


class FixtureVerificationConsumer:
    def __init__(self, store):
        self.store = store

    async def consume(self, output, **kwargs):
        attempt = await self.store.get_workflow_activity(output.source_id)
        session = await self.store.get_session(attempt.invocation.parent_session_id)
        return await self.store.consume_workflow_verification(
            output,
            workspace=FixedWorkspace(),
            parent_session_id=attempt.invocation.parent_session_id,
            parent_workspace_root=Path(session.cwd),
            **kwargs,
        )


async def finish_fixture_verification(store, attempt, *, passed=True):
    session = await store.get_session(attempt.invocation.parent_session_id)
    root = Path(session.cwd)

    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return END

    with patch(
        "neuro_code.infrastructure.persistence.sqlite_session_workflow_verification.datetime",
        FixtureClock,
    ):
        return await store.execute_workflow_verification(
            attempt.invocation.invocation_id,
            parent_session_id=attempt.invocation.parent_session_id,
            parent_workspace_root=root,
            configuration=ApprovedWorkflowVerification("pytest"),
            command=FixtureCommand(root, passed),
            workspace=FixedWorkspace(),
            updated_at=END,
        )


def insert_legacy_fake_output(store, run, output):
    """Construct a pre-DW5b fixture by SQL, without a production fake-write API."""
    from contextlib import closing
    from dataclasses import replace

    from neuro_code.domain.workflows.publication import canonical
    from neuro_code.domain.workflows.state import WorkflowEventKind, WorkflowStatus
    from neuro_code.infrastructure.persistence.sqlite_session_workflows import (
        _append_event,
        _save_run,
    )

    proposed = replace(
        run,
        generation=run.generation + 1,
        updated_at=END,
        status=WorkflowStatus.RUNNING,
        waiting_reason=None,
        position=output.step,
        steps=tuple(
            replace(s, status=WorkflowStatus.COMPLETED, waiting_reason=None)
            if s.identity == output.step
            else s
            for s in run.steps
        ),
    )
    with closing(store._connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _save_run(connection, proposed, expected=run)
        _append_event(
            connection,
            proposed,
            "output:" + output.step.key,
            WorkflowEventKind.STEP,
            canonical(
                {
                    "operation": "consume_typed_output",
                    "step_key": output.step.key,
                    "output_fingerprint": output.fingerprint,
                }
            ),
        )
        connection.execute(
            "INSERT INTO workflow_step_outputs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                output.step.key,
                output.kind.value,
                output.source_id,
                output.source_fingerprint,
                output.input_fingerprint,
                output.output_json,
                output.fingerprint,
                proposed.generation,
            ),
        )
