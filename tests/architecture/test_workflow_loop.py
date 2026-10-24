"""DW5b-5 bounded loop tests using real SQLite, Git, pytest and AgentRuntime."""

from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import closing

import pytest

from neuro_code.application.workflows.workflow_loop import (
    UnsupportedWorkflowLoop,
    WorkflowLoopCoordinator,
    WorkflowLoopStatus,
    bounded_verification_loop_plan,
)
from neuro_code.bootstrap.composition_services import CompositionServicesMixin
from neuro_code.domain.conversation.events import ModelCompleted, ModelTextDelta, ModelToolCall
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.workflows.activity import WorkflowActivityState
from neuro_code.domain.workflows.definition import Branch, compile_workflow
from neuro_code.domain.workflows.interpreter import invocation_id
from neuro_code.domain.workflows.publication import canonical
from neuro_code.domain.workflows.state import (
    BudgetAmounts,
    StepIdentity,
    WorkflowChange,
    WorkflowEventKind,
    WorkflowStatus,
)
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from tests.architecture.test_workflow_interpreter import engine, setup
from tests.architecture.test_workflow_repair_adapter import build as build_repair
from tests.architecture.test_workflow_verify_adapter import adapter as build_verifier
from tests.architecture.test_workflow_verify_adapter import executor
from tests.architecture.workflow_loop_fixtures import bounded_loop_source, compile_bounded_loop


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def repair_edit_script():
    return [
        [
            ModelToolCall(
                ToolCall(
                    "edit-1",
                    "search_replace",
                    {"path": "test_sample.py", "old": "assert False", "new": "assert True"},
                )
            ),
            ModelCompleted("stop", input_tokens=10, output_tokens=5),
        ],
        [
            ModelTextDelta("Updated the assertion; only the fresh VERIFY can establish PASS."),
            ModelCompleted("stop", input_tokens=10, output_tokens=5),
        ],
    ]


def repair_noop_script():
    return [
        ModelTextDelta("No change was necessary; this is not verification evidence."),
        ModelCompleted("stop", input_tokens=10, output_tokens=5),
    ]


async def make_loop(
    tmp_path,
    *,
    passed: bool,
    verify_count: int = 3,
    repair_scripts=None,
    ceiling=None,
):
    root = tmp_path / "parent"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "Tests")
    (root / "test_sample.py").write_text(
        f"def test_sample():\n    assert {passed}\n", encoding="utf-8"
    )
    (root / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.ruff_cache/\n", encoding="utf-8"
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "workflow loop fixture")
    store = await setup(tmp_path, bounded_loop_source(verify_count), ceiling=ceiling)
    run = await store.get_workflow_run("run")
    assert run is not None
    with closing(store._connect()) as connection, connection:
        connection.execute(
            "UPDATE sessions SET cwd=? WHERE id=?", (str(root), run.parent_session_id)
        )
    verifier = build_verifier(store, root, run.parent_session_id)
    scripts = repair_scripts if repair_scripts is not None else repair_edit_script()
    repair, provider, _binding = build_repair(
        store,
        root,
        run.parent_session_id,
        scripts=scripts,
    )
    interpreter = engine(store, verification=verifier)
    coordinator = WorkflowLoopCoordinator(
        state=store,
        facts=store,
        activities=store,
        interpreter=interpreter,
        verification=verifier,
        repair=repair,
    )
    return store, root, run, verifier, repair, provider, coordinator


def test_canonical_branch_chain_compiles_and_proves_final_fail_has_no_repair():
    one = compile_bounded_loop(1)
    two = compile_bounded_loop(2)
    three = compile_bounded_loop(3)
    assert bounded_verification_loop_plan(one).repair_step_ids == ()
    assert bounded_verification_loop_plan(two).repair_step_ids == ("repair_1",)
    plan = bounded_verification_loop_plan(three)
    assert plan.verification_step_ids == ("verify_1", "verify_2", "verify_3")
    assert plan.repair_step_ids == ("repair_1", "repair_2")
    terminal_fail_branches = []

    def walk(steps):
        for step in steps:
            if isinstance(step, Branch):
                paths = dict(step.paths)
                if not paths["fail"]:
                    terminal_fail_branches.append(step)
                for path in paths.values():
                    walk(path)

    walk(three.steps)
    assert len(terminal_fail_branches) == 1


def test_loop_coordinator_rejects_unbounded_or_skippable_shapes():
    too_many = bounded_loop_source(3)
    # Add a fourth statically declared VERIFY beyond the bounded suffix.
    too_many["steps"].insert(
        0,
        {
            "kind": "Activity",
            "step_id": "extra_verify",
            "activity": "parent.verify",
            "inputs": {},
        },
    )
    definition = compile_workflow(canonical(too_many))
    with pytest.raises(UnsupportedWorkflowLoop):
        bounded_verification_loop_plan(definition)

    skipped = bounded_loop_source(2)
    route = skipped["steps"][-1]
    assert isinstance(route, dict)
    route["paths"]["pass"] = [
        {
            "kind": "Activity",
            "step_id": "pass_repair",
            "activity": "parent.repair",
            "inputs": {},
        }
    ]
    definition = compile_workflow(canonical(skipped))
    with pytest.raises(UnsupportedWorkflowLoop):
        bounded_verification_loop_plan(definition)


async def test_trusted_composition_uses_existing_adapters_and_one_store(tmp_path):
    store, _root, _run, verifier, repair, _provider, _coordinator = await make_loop(
        tmp_path, passed=True, verify_count=1, repair_scripts=[]
    )
    parent_binding = repair.turn.binding

    class CompositionFixture:
        def __init__(self):
            self.store = store

        def create_workflow_verify_service(self, *, parent_binding):
            assert parent_binding is repair.turn.binding
            return verifier

        def create_workflow_repair_service(self, *, parent_binding):
            assert parent_binding is repair.turn.binding
            return repair

    composed = CompositionServicesMixin.create_workflow_loop_coordinator(
        CompositionFixture(), parent_binding=parent_binding
    )
    assert composed.state is store
    assert composed.facts is store
    assert composed.activities is store
    assert composed.verification is verifier
    assert composed.repair is repair


@pytest.mark.parametrize("passed", [True, False])
async def test_one_verify_pass_or_fail_never_dispatches_unverified_repair(tmp_path, passed):
    store, root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=passed, verify_count=1, repair_scripts=[]
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is (
        WorkflowLoopStatus.VERIFIED if passed else WorkflowLoopStatus.EXHAUSTED
    )
    assert result.terminal_verifications == 1
    assert result.terminal_repairs == 0
    assert provider.calls == []
    current = await store.get_workflow_run("run")
    assert current.status is WorkflowStatus.WAITING
    expected = "assert True" if passed else "assert False"
    assert expected in (root / "test_sample.py").read_text(encoding="utf-8")


async def test_real_verify_fail_repair_and_fresh_verify_pass(tmp_path):
    store, root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=3
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.VERIFIED
    assert (result.terminal_verifications, result.terminal_repairs) == (2, 1)
    assert (root / "test_sample.py").read_text(encoding="utf-8").endswith("assert True\n")
    assert len(provider.calls) == 2
    current = await store.get_workflow_run("run")
    assert current.status is WorkflowStatus.WAITING
    assert current.waiting_reason == "control_flow_exhausted: completion requirements pending"
    assert current.status is not WorkflowStatus.COMPLETED
    invocations = []
    for step_id in ("verify_1", "verify_2"):
        step = next(item for item in current.steps if item.identity == StepIdentity(step_id))
        key = invocation_id("run", step.identity, step.input_fingerprint)
        invocations.append(key)
        attempt = await store.get_workflow_activity(key)
        assert attempt.state is WorkflowActivityState.COMPLETED
        assert attempt.result.usage.known
    assert invocations[0] != invocations[1]
    assert (
        json.loads(
            (await store.get_workflow_step_output("run", StepIdentity("verify_1"))).output_json
        )["status"]
        == "FAIL"
    )
    assert (
        json.loads(
            (await store.get_workflow_step_output("run", StepIdentity("verify_2"))).output_json
        )["status"]
        == "PASS"
    )


async def test_two_repairs_require_three_distinct_real_verifications(tmp_path):
    scripts = [repair_noop_script(), *repair_edit_script()]
    store, root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=3, repair_scripts=scripts
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.VERIFIED
    assert (result.terminal_verifications, result.terminal_repairs) == (3, 2)
    assert len(provider.calls) == 3
    assert "assert True" in (root / "test_sample.py").read_text(encoding="utf-8")
    run = await store.get_workflow_run("run")
    verify_keys = []
    for step_id in ("verify_1", "verify_2", "verify_3"):
        step = next(item for item in run.steps if item.identity == StepIdentity(step_id))
        verify_keys.append(invocation_id("run", step.identity, step.input_fingerprint))
    assert len(set(verify_keys)) == 3
    for index, expected in enumerate(("FAIL", "FAIL", "PASS"), 1):
        output = await store.get_workflow_step_output("run", StepIdentity(f"verify_{index}"))
        assert json.loads(output.output_json)["status"] == expected
    for index in (1, 2):
        output = await store.get_workflow_step_output("run", StepIdentity(f"repair_{index}"))
        assert json.loads(output.output_json)["status"] == "COMPLETED"


async def test_third_fail_exhausts_without_third_repair(tmp_path):
    scripts = [repair_noop_script(), repair_noop_script()]
    store, root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=3, repair_scripts=scripts
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.EXHAUSTED
    assert (result.terminal_verifications, result.terminal_repairs) == (3, 2)
    assert len(provider.calls) == 2
    assert "assert False" in (root / "test_sample.py").read_text(encoding="utf-8")
    run = await store.get_workflow_run("run")
    assert StepIdentity("repair_3") not in {step.identity for step in run.steps}
    assert run.status is WorkflowStatus.WAITING
    assert run.status is not WorkflowStatus.COMPLETED


async def test_repair_permission_block_stops_before_following_verify(tmp_path):
    store, root, run, _verify, _repair, _provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=3, repair_scripts=repair_edit_script()
    )
    blocked_repair, provider, _binding = build_repair(
        store,
        root,
        run.parent_session_id,
        deny=True,
        scripts=repair_edit_script(),
    )
    coordinator.repair = blocked_repair
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.BLOCKED
    assert result.terminal_verifications == 1
    assert result.terminal_repairs == 1
    assert "assert False" in (root / "test_sample.py").read_text(encoding="utf-8")
    run = await store.get_workflow_run("run")
    assert StepIdentity("verify_2") not in {step.identity for step in run.steps}
    assert len(provider.calls) == 2  # One bounded REPAIR turn, with its normal follow-up call.


async def test_unknown_repair_usage_stops_before_following_verify(tmp_path):
    store, root, run, _verify, _repair, _provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=3
    )
    unknown_repair, provider, _binding = build_repair(
        store, root, run.parent_session_id, known=False, change=True
    )
    coordinator.repair = unknown_repair
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.INDETERMINATE
    assert result.terminal_verifications == 1
    assert result.terminal_repairs == 1
    run = await store.get_workflow_run("run")
    assert StepIdentity("verify_2") not in {step.identity for step in run.steps}
    assert run.status is WorkflowStatus.NEEDS_ATTENTION
    repair_step = next(step for step in run.steps if step.identity == StepIdentity("repair_1"))
    repair_key = invocation_id("run", repair_step.identity, repair_step.input_fingerprint)
    repair_attempt = await store.get_workflow_activity(repair_key)
    assert repair_attempt.state is WorkflowActivityState.INDETERMINATE
    assert not repair_attempt.result.usage.known
    assert len(provider.calls) >= 1


async def test_external_change_after_repair_is_tested_by_fresh_verify(tmp_path):
    store, root, run, _verify, repair, provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=2
    )

    class ExternalChangeAfterRepair:
        async def run_once(self, invocation):
            outcome = await repair.run_once(invocation)
            (root / "test_sample.py").write_text(
                "def test_sample():\n    assert False\n", encoding="utf-8"
            )
            return outcome

    coordinator.repair = ExternalChangeAfterRepair()
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.EXHAUSTED
    assert (result.terminal_verifications, result.terminal_repairs) == (2, 1)
    for step_id in ("verify_1", "verify_2"):
        output = await store.get_workflow_step_output("run", StepIdentity(step_id))
        assert json.loads(output.output_json)["status"] == "FAIL"
    assert "assert False" in (root / "test_sample.py").read_text(encoding="utf-8")
    assert len(provider.calls) == 2


async def test_global_budget_exhaustion_stops_before_repair_dispatch(tmp_path):
    _store, root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path,
        passed=False,
        verify_count=3,
        repair_scripts=repair_edit_script(),
        ceiling=BudgetAmounts(
            model_calls=0,
            tool_calls=1,
            input_tokens=0,
            output_tokens=0,
            wall_milliseconds=120_000,
        ),
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status in {
        WorkflowLoopStatus.BLOCKED,
        WorkflowLoopStatus.INDETERMINATE,
    }
    assert result.terminal_verifications == 1
    assert result.terminal_repairs == 0
    assert provider.calls == []
    assert "assert False" in (root / "test_sample.py").read_text(encoding="utf-8")


async def test_cancelled_run_stops_before_activity_dispatch(tmp_path):
    store, _root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=3
    )
    assert run.owner_id is not None
    await store.transition_workflow_run(
        "run",
        WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="cancel-before-loop-drive",
        updated_at=run.updated_at,
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.CANCELLED
    assert result.terminal_verifications == result.terminal_repairs == 0
    assert provider.calls == []


async def test_permission_denial_stops_before_repair(tmp_path):
    store, root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=3
    )
    denied = build_verifier(store, root, run.parent_session_id, command=executor(root, deny=True))
    coordinator.verification = denied
    coordinator.interpreter.verification = denied
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.BLOCKED
    assert result.terminal_verifications == 1
    assert result.terminal_repairs == 0
    assert provider.calls == []


async def test_coordinator_cannot_dispatch_after_owner_fence_replacement(tmp_path):
    _store, _root, run, _verify, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=True, verify_count=1, repair_scripts=[]
    )
    result = await coordinator.drive_once(
        "run", owner_id=run.owner_id, owner_fence=run.owner_fence + 1
    )
    assert result.status is WorkflowLoopStatus.BLOCKED
    assert provider.calls == []


async def test_external_change_before_verify_consumption_fails_closed(tmp_path):
    store, root, run, verifier, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=True, verify_count=1, repair_scripts=[]
    )

    class ChangeAfterVerify:
        async def run_once(self, invocation):
            outcome = await verifier.run_once(invocation)
            with (root / "test_sample.py").open("a", encoding="utf-8") as file:
                file.write("# external change\n")
            return outcome

        async def consume(self, output, **kwargs):
            return await verifier.consume(output, **kwargs)

    changed = ChangeAfterVerify()
    coordinator.verification = changed
    coordinator.interpreter.verification = changed
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.INDETERMINATE
    current = await store.get_workflow_run("run")
    assert not any(
        step.identity == StepIdentity("verify_1") and step.status is WorkflowStatus.COMPLETED
        for step in current.steps
    )
    assert await store.get_workflow_step_output("run", StepIdentity("verify_1")) is None
    assert provider.calls == []


async def test_ack_loss_after_verify_terminal_resumes_without_second_command(tmp_path):
    store, root, run, verifier, _repair, provider, coordinator = await make_loop(
        tmp_path, passed=True, verify_count=1, repair_scripts=[]
    )

    class LoseAckOnce:
        lost = False

        async def run_once(self, invocation):
            result = await verifier.run_once(invocation)
            if not self.lost:
                self.lost = True
                raise RuntimeError("simulated client ACK loss after durable VERIFY")
            return result

        async def consume(self, output, **kwargs):
            return await verifier.consume(output, **kwargs)

    wrapper = LoseAckOnce()
    coordinator.verification = wrapper
    coordinator.interpreter.verification = wrapper
    with pytest.raises(RuntimeError, match="ACK loss"):
        await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    new_verifier = build_verifier(reopened, root, run.parent_session_id)
    new_repair, new_provider, _binding = build_repair(
        reopened, root, run.parent_session_id, scripts=[]
    )
    coordinator = WorkflowLoopCoordinator(
        state=reopened,
        facts=reopened,
        activities=reopened,
        interpreter=engine(reopened, verification=new_verifier),
        verification=new_verifier,
        repair=new_repair,
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.VERIFIED
    assert result.terminal_verifications == 1
    assert new_provider.calls == []
    assert provider.calls == []


async def test_ack_loss_after_repair_terminal_replays_without_second_model_turn(tmp_path):
    store, root, run, _verifier, repair, provider, coordinator = await make_loop(
        tmp_path, passed=False, verify_count=2
    )

    class LoseAckOnce:
        lost = False

        async def run_once(self, invocation):
            result = await repair.run_once(invocation)
            if not self.lost:
                self.lost = True
                raise RuntimeError("simulated client ACK loss after durable REPAIR")
            return result

    wrapper = LoseAckOnce()
    coordinator.repair = wrapper
    with pytest.raises(RuntimeError, match="ACK loss"):
        await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert provider.calls
    calls_after_repair = len(provider.calls)

    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    new_verifier = build_verifier(reopened, root, run.parent_session_id)
    new_repair, replay_provider, _binding = build_repair(
        reopened, root, run.parent_session_id, scripts=[]
    )
    coordinator = WorkflowLoopCoordinator(
        state=reopened,
        facts=reopened,
        activities=reopened,
        interpreter=engine(reopened, verification=new_verifier),
        verification=new_verifier,
        repair=new_repair,
    )
    result = await coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
    assert result.status is WorkflowLoopStatus.VERIFIED
    assert (result.terminal_verifications, result.terminal_repairs) == (2, 1)
    assert len(provider.calls) == calls_after_repair
    assert replay_provider.calls == []
    assert (root / "test_sample.py").read_text(encoding="utf-8").endswith("assert True\n")


async def test_concurrent_coordinators_share_one_verify_dispatch(tmp_path):
    store, root, run, _verifier, _repair, _provider, _coordinator = await make_loop(
        tmp_path, passed=True, verify_count=1, repair_scripts=[]
    )

    class CountingCommand:
        def __init__(self):
            self.delegate = executor(root)
            self.workspace_root = root
            self.calls = 0

        async def verify_command(self, configuration, *, session_id, pre_entry_guard):
            self.calls += 1
            return await self.delegate.verify_command(
                configuration,
                session_id=session_id,
                pre_entry_guard=pre_entry_guard,
            )

    command = CountingCommand()
    coordinators = []
    for _ in range(2):
        verifier = build_verifier(store, root, run.parent_session_id, command=command)
        repair, _provider, _binding = build_repair(store, root, run.parent_session_id, scripts=[])
        coordinators.append(
            WorkflowLoopCoordinator(
                state=store,
                facts=store,
                activities=store,
                interpreter=engine(store, verification=verifier),
                verification=verifier,
                repair=repair,
            )
        )

    results = await asyncio.gather(
        *(
            coordinator.drive_once("run", owner_id=run.owner_id, owner_fence=run.owner_fence)
            for coordinator in coordinators
        )
    )
    assert WorkflowLoopStatus.VERIFIED in {result.status for result in results}
    assert command.calls == 1
    current = await store.get_workflow_run("run")
    assert current.status is WorkflowStatus.WAITING
    verify_step = next(step for step in current.steps if step.identity == StepIdentity("verify_1"))
    attempt = await store.get_workflow_activity(
        invocation_id("run", verify_step.identity, verify_step.input_fingerprint)
    )
    assert attempt.state is WorkflowActivityState.COMPLETED
