"""Real SQLite/Git/Bash VERIFY; injected boundaries are explicitly identified."""

from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.ports.workflow_verification import ApprovedWorkflowVerification
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.application.workflows.workflow_verify import WorkflowVerifyActivityAdapter
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.tools import ToolExecutionResult
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.interpreter import invocation_id
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import BudgetAmounts, StepIdentity, WorkflowStatus
from neuro_code.infrastructure.git.worktree import LocalGitWorktreeAdapter
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.tools.bash import BashTool
from neuro_code.infrastructure.tools.registry import ToolRegistry
from neuro_code.infrastructure.workspace.checkpoints import LocalWorkspaceStateAdapter
from neuro_code.infrastructure.workspace.projection import LocalParentWorkspaceProjectionReader
from neuro_code.infrastructure.workspace.verification import LocalWorkflowVerificationWorkspace
from tests.architecture.test_workflow_interpreter import activity, engine, setup, source
from tests.fakes import EmptyWorkspaceChangeObserver


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


async def ready(tmp_path, text="def test_pass():\n    assert True\n", *, data=None):
    root = tmp_path / "parent"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "Tests")
    (root / "test_sample.py").write_text(text, encoding="utf-8")
    (root / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.ruff_cache/\n", encoding="utf-8"
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    data = data or dict(source(), steps=[activity("verify")])
    store = await setup(tmp_path, data)
    run = await store.get_workflow_run("run")
    with closing(store._connect()) as conn, conn:
        conn.execute("UPDATE sessions SET cwd=? WHERE id=?", (str(root), run.parent_session_id))
    await tick(store)
    await tick(store)
    run = await store.get_workflow_run("run")
    step = next(s for s in run.steps if s.identity == StepIdentity("verify"))
    key = invocation_id(run.run_id, step.identity, step.input_fingerprint)
    return store, root, run.parent_session_id, key


async def tick(store, verifier=None):
    run = await store.get_workflow_run("run")
    return await engine(store, verification=verifier).advance_once(
        "run",
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=datetime.now(UTC),
    )


def workspace(root):
    git_adapter = LocalGitWorktreeAdapter()
    state = LocalWorkspaceStateAdapter(git=git_adapter, workspace_git=git_adapter)
    return LocalWorkflowVerificationWorkspace(
        LocalParentWorkspaceProjectionReader(git=git_adapter, state=state)
    )


def executor(root, *, deny=False, context=None, tools=None):
    return ToolExecutor(
        tools=tools or ToolRegistry([BashTool()]),
        permissions=PermissionManager(
            mode=PermissionMode.DONT_ASK if deny else PermissionMode.BYPASS
        ),
        approver=None,
        tool_context=context or ToolContext(root),
        session_store=None,
        workspace_change_observer=EmptyWorkspaceChangeObserver(),
        context_builder=ContextBuilder(
            reasoning_effort=ReasoningEffort.HIGH,
            interaction_mode=InteractionMode.AUTO,
            plan=None,
            instruction_provider=None,
            skill_provider=None,
        ),
    )


def pytest_command():
    return "python -m pytest -q -p no:cacheprovider"


def adapter(store, root, session, *, configuration="default", command=None, observer=None):
    config = (
        ApprovedWorkflowVerification(pytest_command())
        if configuration == "default"
        else configuration
    )
    return WorkflowVerifyActivityAdapter(
        store=store,
        activities=store,
        command=command or executor(root),
        workspace=observer or workspace(root),
        parent_session_id=session,
        parent_workspace_root=root,
        configuration=config,
    )


@pytest.mark.parametrize("passed", [True, False])
async def test_real_pytest_and_consumption(tmp_path, passed):
    store, root, session, key = await ready(tmp_path, f"def test_pass():\n    assert {passed}\n")
    verifier = adapter(store, root, session)
    result = (await verifier.run_once(key)).attempt
    assert result.state is State.COMPLETED
    assert json.loads(result.result.output_json)["status"] == ("PASS" if passed else "FAIL")
    assert result.result.usage.known
    assert result.result.usage.tool_calls == 1
    assert result.result.usage.wall_milliseconds > 0
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.WAITING
    assert (await tick(store, verifier)).action == "consume_activity"
    assert (await verifier.run_once(key)).attempt == result


async def test_real_static_check(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = "ruff check test_sample.py"
    verifier = adapter(store, root, session, configuration=ApprovedWorkflowVerification(command))
    assert (await verifier.run_once(key)).attempt.state is State.COMPLETED
    assert (await tick(store, verifier)).action == "consume_activity"


@pytest.mark.parametrize("missing", [True, False])
async def test_missing_configuration_and_permission_denial(tmp_path, missing):
    store, root, session, key = await ready(tmp_path)
    result = (
        await adapter(
            store,
            root,
            session,
            configuration=None if missing else "default",
            command=executor(root, deny=True),
        ).run_once(key)
    ).attempt
    assert result.state is State.BLOCKED
    assert result.result.usage.tool_calls == 0
    assert result.revision == 2 if missing else result.revision == 3
    assert await store.get_workflow_activity(key) == result


@pytest.mark.parametrize(
    "command", ["echo PASS", "pytest; echo forged", 'python -c "print(0)"', "pytest && touch x"]
)
def test_unapproved_command_not_authority(command):
    with pytest.raises(ValueError, match="verification command"):
        ApprovedWorkflowVerification(command)


async def test_stale_pass_before_consume(tmp_path):
    store, root, session, key = await ready(tmp_path)
    verifier = adapter(store, root, session)
    await verifier.run_once(key)
    (root / "test_sample.py").write_text("def test_fail():\n    assert False\n")
    before = await store.get_workflow_run("run")
    with pytest.raises(WorkflowStateError, match="stale"):
        await tick(store, verifier)
    assert await store.get_workflow_run("run") == before


class ControlledCommand:
    """Injected execution port for precise crash/ordering tests, not real Bash."""

    def __init__(self, root, *, result=None, error=None):
        self.workspace_root = root
        self.result = result or ToolExecutionResult(
            "call", "bash", "test output", metadata={"exit_code": 0}
        )
        self.error = error
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def verify_command(self, configuration, *, session_id):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.error:
            raise self.error
        return self.result


async def test_during_execution_change_then_restore_is_not_pass(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    verifier = adapter(store, root, session, command=command)
    task = asyncio.create_task(verifier.run_once(key))
    await asyncio.wait_for(command.entered.wait(), 10)
    path = root / "test_sample.py"
    original = path.read_bytes()
    path.write_bytes(b"changed\n")
    await asyncio.sleep(0.12)
    path.write_bytes(original)
    command.release.set()
    result = (await task).attempt
    assert result.state is State.INDETERMINATE
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_other_store_adapters_busy_and_known_settles(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    verifier = adapter(store, root, session, command=command)
    task = asyncio.create_task(verifier.run_once(key))
    await asyncio.wait_for(command.entered.wait(), 10)
    run = await store.get_workflow_run("run")
    competitors = [SqliteSessionStore(store.database_path) for _ in range(4)]
    outcomes = await asyncio.gather(
        *(adapter(s, root, session, command=command).run_once(key) for s in competitors)
    )
    assert all(o.disposition == "busy" for o in outcomes)
    assert await store.get_workflow_run("run") == run
    command.release.set()
    result = (await task).attempt
    assert result.result.usage.known
    assert command.calls == 1
    assert (await verifier.run_once(key)).attempt == result
    assert command.calls == 1


async def test_task_cancel_and_reopen_never_redispatch(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    task = asyncio.create_task(adapter(store, root, session, command=command).run_once(key))
    await asyncio.wait_for(command.entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    result = (await adapter(reopened, root, session, command=command).run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.usage.tool_calls is None
    assert result.result.usage.wall_milliseconds is None
    assert command.calls == 1
    assert (await adapter(reopened, root, session, command=command).run_once(key)).attempt == result


@pytest.mark.parametrize(
    ("code", "state"), [(2, State.FAILED), (-9, State.INDETERMINATE), (None, State.INDETERMINATE)]
)
async def test_executor_fault_is_not_test_fail(tmp_path, code, state):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(
        root,
        result=ToolExecutionResult(
            "call", "bash", "fault", is_error=True, metadata={"exit_code": code}
        ),
    )
    command.release.set()
    result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.state is state
    assert result.result.output_json is None


async def test_evidence_rehash_tamper_is_rejected(tmp_path):
    store, root, session, key = await ready(tmp_path)
    await adapter(store, root, session).run_once(key)
    with closing(store._connect()) as conn, conn:
        for (trigger,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='workflow_verification_evidence'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {trigger}")
        exec_id, payload, _ = conn.execute(
            "SELECT * FROM workflow_verification_evidence"
        ).fetchone()
        value = json.loads(payload)
        value["exit_code"] = 1
        conn.execute(
            "UPDATE workflow_verification_evidence SET payload_json=?,payload_fingerprint=? WHERE execution_id=?",
            (canonical(value), digest(value), exec_id),
        )
    with pytest.raises(WorkflowStateError, match="proof"):
        await store.get_workflow_activity(key)


async def test_atomic_settlement_rollback_and_unknown_recovery(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    command.release.set()
    with (
        patch(
            "neuro_code.infrastructure.persistence.sqlite_session_workflow_verification._settle_terminal",
            side_effect=RuntimeError("crash during settlement"),
        ),
        pytest.raises(RuntimeError, match="crash"),
    ):
        await adapter(store, root, session, command=command).run_once(key)
    assert (await store.get_workflow_activity(key)).state is State.RUNNING
    with closing(store._connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_verification_evidence").fetchone() == (
            0,
        )
    result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.result.usage.tool_calls is None
    assert command.calls == 1


@pytest.mark.parametrize("running", [False, True])
async def test_legacy_claim_or_running_no_execution_fact(tmp_path, running):
    store, root, session, key = await ready(tmp_path)
    current = await store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="old-owner",
        reserved=BudgetAmounts(tool_calls=1, wall_milliseconds=35000),
        updated_at=datetime.now(UTC),
    )
    if running:
        await store.start_workflow_activity(
            key,
            expected_revision=current.revision,
            owner_id=current.owner_id,
            owner_fence=current.owner_fence,
            updated_at=datetime.now(UTC),
        )
    command = ControlledCommand(root)
    result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.usage.tool_calls == (None if running else 0)
    assert command.calls == 0


async def test_parent_and_wrong_request_rejected(tmp_path):
    store, root, session, key = await ready(tmp_path)
    with pytest.raises(WorkflowStateError):
        await store.execute_workflow_verification(
            key,
            parent_session_id=session,
            parent_workspace_root=root.parent,
            configuration=ApprovedWorkflowVerification(pytest_command()),
            command=executor(root),
            workspace=workspace(root),
            updated_at=datetime.now(UTC),
        )
    assert (await store.get_workflow_activity(key)).state is State.READY
    with pytest.raises(WorkflowStateError):
        await adapter(store, root, "other-session").run_once(key)
    assert (await store.get_workflow_activity(key)).state is State.READY


@pytest.mark.parametrize("passed", [True, False])
async def test_real_fail_output_routes_branch_without_repair_execution(tmp_path, passed):
    data = source("branch")
    store, root, session, key = await ready(
        tmp_path, f"def test_result():\n    assert {passed}\n", data=data
    )
    verifier = adapter(store, root, session)
    await verifier.run_once(key)
    await tick(store, verifier)
    outcome = await tick(store, verifier)
    assert outcome.action == "record_branch"
    from neuro_code.domain.workflows.state import WorkflowEventKind

    events = await store.get_workflow_journal("run")
    event = next(event for event in events if event.kind is WorkflowEventKind.BRANCH)
    assert json.loads(event.payload_json)["change"]["selected_path"] == (
        "pass" if passed else "fail"
    )
    assert not any(
        step.identity.step_id == "repair" and step.status is WorkflowStatus.COMPLETED
        for step in outcome.run.steps
    )


@pytest.mark.parametrize("passed", [True, False])
@pytest.mark.parametrize("no_changes", [False, True])
async def test_real_adopt_then_real_pytest(tmp_path, passed, no_changes):
    from tests.architecture.test_workflow_adopt_adapter import adapter as adopt_adapter
    from tests.architecture.test_workflow_adopt_adapter import ready as adopt_ready
    from tests.architecture.test_workflow_adopt_execution_race import DiskMutation

    ref = {"kind": "result", "step_id": "adopt", "field_path": ["parent_workspace_changed"]}
    f, adopt_key = await adopt_ready(
        tmp_path,
        no_changes=no_changes,
        following_steps=[activity("verify", inputs={"changed": ref})],
    )
    port = DiskMutation(f.parent, f.binding.workspace_root)
    adopted = (await adopt_adapter(f, mutation=port).run_once(adopt_key)).attempt
    assert adopted.state is State.COMPLETED
    root, store, session = f.binding.workspace_root, f.store, f.binding.runner.session_id
    # Real fixture command files/caches are in the parent workspace before VERIFY.
    (root / "test_sample.py").write_text(f"def test_result():\n    assert {passed}\n")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")

    async def advance(verifier=None):
        run = await store.get_workflow_run("workflow-run")
        return await engine(store, verification=verifier).advance_once(
            run.run_id,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=datetime.now(UTC),
        )

    for _ in range(4):
        await advance()
        run = await store.get_workflow_run("workflow-run")
        candidate = next(
            (step for step in run.steps if step.identity == StepIdentity("verify")), None
        )
        if candidate:
            key = invocation_id(run.run_id, candidate.identity, candidate.input_fingerprint)
            if await store.get_workflow_activity(key):
                break
    verifier = adapter(store, root, session)
    result = (await verifier.run_once(key)).attempt
    assert result.state is State.COMPLETED
    assert json.loads(result.result.output_json)["status"] == ("PASS" if passed else "FAIL")
    assert (await advance(verifier)).action == "consume_activity"
    assert len(port.calls) == (0 if no_changes else 2)


async def test_ack_loss_exact_replay_and_no_usage_injection(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    command.release.set()
    original = store._finish_verification

    async def lose_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("outer ACK loss")

    with (
        patch.object(store, "_finish_verification", side_effect=lose_ack),
        pytest.raises(RuntimeError, match="ACK"),
    ):
        await adapter(store, root, session, command=command).run_once(key)
    completed = await store.get_workflow_activity(key)
    assert completed.result.usage.tool_calls == 1
    assert (
        await adapter(
            SqliteSessionStore(store.database_path), root, session, command=command
        ).run_once(key)
    ).attempt == completed
    assert command.calls == 1
    with pytest.raises(TypeError, match="usage"):
        await store.reconcile_workflow_verification(
            key,
            parent_session_id=session,
            parent_workspace_root=root,
            updated_at=datetime.now(UTC),
            usage=BudgetAmounts(),
        )


async def test_generic_finish_cannot_bypass_verify_proof(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    task = asyncio.create_task(adapter(store, root, session, command=command).run_once(key))
    await asyncio.wait_for(command.entered.wait(), 10)
    attempt = await store.get_workflow_activity(key)
    from neuro_code.domain.workflows.activity import WorkflowActivityResult

    forged = WorkflowActivityResult(
        key,
        attempt.invocation.request_fingerprint,
        attempt.invocation.activity,
        State.COMPLETED,
        "fake",
        digest("fake"),
        BudgetAmounts(),
        datetime.now(UTC),
        canonical({"status": "PASS", "workspace_generation": 1}),
    )
    with pytest.raises(WorkflowStateError, match="VERIFY"):
        await store.finish_workflow_activity(
            forged,
            expected_revision=attempt.revision,
            owner_id=attempt.owner_id,
            owner_fence=attempt.owner_fence,
        )
    command.release.set()
    assert (await task).attempt.result.usage.tool_calls == 1


async def test_real_timeout_kills_process_and_no_pass(tmp_path):
    text = "import time\ndef test_slow():\n    time.sleep(20)\n"
    store, root, session, key = await ready(tmp_path, text)
    verifier = adapter(
        store,
        root,
        session,
        configuration=ApprovedWorkflowVerification(pytest_command(), timeout_milliseconds=800),
    )
    result = (await verifier.run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.output_json is None
    assert result.result.usage.tool_calls == 1


async def test_missing_explicit_sandbox_backend_never_pass(tmp_path):
    from neuro_code.domain.sandbox.models import SandboxProfile

    store, root, session, key = await ready(tmp_path)
    result = (
        await adapter(
            store,
            root,
            session,
            command=executor(
                root, context=ToolContext(root, sandbox_profile=SandboxProfile.WORKSPACE)
            ),
        ).run_once(key)
    ).attempt
    assert result.state is State.BLOCKED
    assert result.result.output_json is None
    assert result.result.usage.tool_calls == 0


async def test_bounded_redacted_summary(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(
        root,
        result=ToolExecutionResult(
            "call", "bash", "api_key=sk-supersecret\n" + "X" * 20000, metadata={"exit_code": 0}
        ),
    )
    command.release.set()
    await adapter(store, root, session, command=command).run_once(key)
    with closing(store._connect()) as conn:
        value = json.loads(
            conn.execute("SELECT payload_json FROM workflow_verification_evidence").fetchone()[0]
        )
    assert len(value["summary"].encode()) <= 4096
    assert "sk-supersecret" not in value["summary"]


async def test_wall_overrun_records_actual_not_clamped(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    command.release.set()
    # Inject monotonic clock to exercise accounting, not command timing.
    with patch(
        "neuro_code.infrastructure.persistence.sqlite_session_workflow_verification.time.perf_counter_ns",
        side_effect=[0, 40_000_000_000],
    ):
        result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.result.usage.wall_milliseconds == 40000
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_freshness_rechecked_after_waiting_for_writer_lock(tmp_path):
    store, root, session, key = await ready(tmp_path)
    reader = workspace(root)
    observed = asyncio.Event()

    class Observe:
        async def evidence(self, path):
            value = await reader.evidence(path)
            observed.set()
            return value

        async def watch(self, path):
            return await reader.watch(path)

    verifier = adapter(store, root, session, observer=Observe())
    await verifier.run_once(key)
    observed.clear()
    await store._write_lock.acquire()
    try:
        consume = asyncio.create_task(tick(store, verifier))
        await asyncio.wait_for(observed.wait(), 10)
        (root / "test_sample.py").write_text("def test_new():\n    assert False\n")
    finally:
        store._write_lock.release()
    with pytest.raises(WorkflowStateError, match="stale"):
        await consume
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.WAITING


async def test_result_error_cannot_be_pass_even_with_zero_exit(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(
        root,
        result=ToolExecutionResult(
            "call", "bash", "guard failed", is_error=True, metadata={"exit_code": 0}
        ),
    )
    command.release.set()
    result = (await adapter(store, root, session, command=command).run_once(key)).attempt
    assert result.state is State.INDETERMINATE


async def test_direct_output_cannot_bypass_freshness_port(tmp_path):
    from neuro_code.domain.workflows.interpreter import OutputKind, WorkflowStepOutput

    store, root, session, key = await ready(tmp_path)
    result = (await adapter(store, root, session).run_once(key)).attempt.result
    run = await store.get_workflow_run("run")
    attempt = await store.get_workflow_activity(key)
    output = WorkflowStepOutput(
        run.run_id,
        attempt.invocation.step,
        attempt.invocation.input_fingerprint,
        OutputKind.ACTIVITY,
        key,
        result.fingerprint,
        result.output_json,
    )
    with pytest.raises(WorkflowStateError, match="workspace evidence"):
        await store.commit_workflow_step_output(
            output,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=datetime.now(UTC),
        )


async def test_real_timeout_cleans_descendant_process_tree(tmp_path):
    started, survived = tmp_path / "child-started", tmp_path / "child-survived"
    child = f"from pathlib import Path; import time; Path({str(started)!r}).write_text('started'); time.sleep(4); Path({str(survived)!r}).write_text('leaked')"
    text = f"import subprocess, sys, time\ndef test_tree():\n    subprocess.Popen([sys.executable, '-c', {child!r}])\n    time.sleep(20)\n"
    store, root, session, key = await ready(tmp_path, text)
    verifier = adapter(
        store,
        root,
        session,
        configuration=ApprovedWorkflowVerification(pytest_command(), timeout_milliseconds=2000),
    )
    result = (await verifier.run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert started.exists()
    await asyncio.sleep(4.2)
    assert not survived.exists()


async def test_success_has_real_terminal_time(tmp_path):
    store, root, session, key = await ready(tmp_path)
    command = ControlledCommand(root)
    task = asyncio.create_task(adapter(store, root, session, command=command).run_once(key))
    await asyncio.wait_for(command.entered.wait(), 10)
    await asyncio.sleep(0.05)
    release_time = datetime.now(UTC)
    command.release.set()
    result = (await task).attempt.result
    assert result.terminal_at >= release_time
    with closing(store._connect()) as connection:
        execution = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_verification_executions"
            ).fetchone()[0]
        )
        evidence = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_verification_evidence"
            ).fetchone()[0]
        )
    assert datetime.fromisoformat(execution["started_at"]) <= release_time
    assert evidence["finished_at"] == result.terminal_at.isoformat()


@pytest.mark.parametrize("bash_allowed", [True, False])
async def test_real_composition_builds_parent_bound_verify(tmp_path, monkeypatch, bash_allowed):
    from neuro_code.domain.sandbox.models import SandboxProfile
    from tests.architecture.test_workflow_adopt_adapter import adapter as adopt_adapter
    from tests.architecture.test_workflow_adopt_adapter import ready as adopt_ready
    from tests.architecture.test_workflow_adopt_execution_race import DiskMutation
    from tests.test_result_adoption import (
        ApplicationComposition,
        ApplicationSettings,
        _capability,
        _CompositionProvider,
        _write_composition_config,
    )

    f, adopt_key = await adopt_ready(tmp_path, following_steps=[activity("verify")])
    root, session = f.binding.workspace_root, f.binding.runner.session_id
    await adopt_adapter(f, mutation=DiskMutation(f.parent, root)).run_once(adopt_key)
    (root / "test_sample.py").write_text("def test_result():\n    assert True\n")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    for _ in range(4):
        run = await f.store.get_workflow_run("workflow-run")
        await engine(f.store).advance_once(
            run.run_id,
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            updated_at=datetime.now(UTC),
        )
        run = await f.store.get_workflow_run("workflow-run")
        candidate = next((s for s in run.steps if s.identity.step_id == "verify"), None)
        if candidate:
            key = invocation_id(run.run_id, candidate.identity, candidate.input_fingerprint)
            if await f.store.get_workflow_activity(key):
                break
    _write_composition_config(f.store.database_path.parent)
    monkeypatch.setenv("NEURO_CODE_HOME", str(f.store.database_path.parent))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    app = await ApplicationComposition.open(
        ApplicationSettings(
            cwd=root,
            provider="fixture",
            sandbox="off",
            permission_mode=PermissionMode.BYPASS,
            verification_command=pytest_command(),
        ),
        provider_factory=lambda _config, _failover: _CompositionProvider(),
    )
    binding = None
    try:
        from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet

        original = _capability(root, sandbox=SandboxProfile.OFF)
        capabilities = SubagentCapabilitySet.from_runtime(
            tool_names=tuple(original.allowed_tool_names) + (("bash",) if bash_allowed else ()),
            cwd=root,
            sandbox_profile=SandboxProfile.OFF,
            enable_background_tasks=False,
            max_steps=8,
        )
        binding = await app.create_binding(resume_id=session, capabilities=capabilities)
        verifier = app.create_workflow_verify_service(parent_binding=binding)
        assert verifier.command is binding.verification_executor
        result = (await verifier.run_once(key)).attempt
        if bash_allowed:
            assert result.state is State.COMPLETED
            assert json.loads(result.result.output_json)["status"] == "PASS"
            assert result.result.usage.tool_calls == 1
        else:
            assert result.state is State.BLOCKED
            assert result.result.usage.tool_calls == 0
            assert result.result.output_json is None
    finally:
        if binding:
            await binding.close()
        await app.close()
