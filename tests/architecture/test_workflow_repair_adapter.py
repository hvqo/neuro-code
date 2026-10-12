"""Real SQLite/Git/AgentRuntime/filesystem, with a controlled offline provider.

Provider scripts and fault injection test boundaries; no paid model calls and
no claim of filesystem/provider exactly-once is made.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing

import pytest

from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
    PermissionMode,
    PermissionRule,
)
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workflow_repair import WorkflowRepairLimits
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.sessions.conversation import AgentConversation
from neuro_code.application.workflows.workflow_repair import (
    BoundWorkflowRepairTurn,
    WorkflowRepairActivityAdapter,
)
from neuro_code.domain.conversation.events import ModelCompleted, ModelTextDelta, ModelToolCall
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.interpreter import invocation_id
from neuro_code.domain.workflows.state import StepIdentity, WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.tools.filesystem_mutation import SearchReplaceTool
from neuro_code.infrastructure.tools.registry import ToolRegistry
from neuro_code.infrastructure.workspace.repair import LocalWorkflowRepairWorkspace
from tests.architecture.test_workflow_interpreter import activity, source
from tests.architecture.test_workflow_verify_adapter import adapter as verify_adapter
from tests.architecture.test_workflow_verify_adapter import ready as verify_ready
from tests.architecture.test_workflow_verify_adapter import tick, workspace
from tests.fakes import EmptyWorkspaceChangeObserver
from tests.test_agent_runtime import ScriptedProvider


async def ready(tmp_path, *, passed=False, inputs=None, injected_verify=False, branch=False):
    data = source()
    data["steps"] = [
        activity("verify"),
        activity(
            "repair",
            "parent.repair",
            inputs=inputs
            or {"failure": {"kind": "result", "step_id": "verify", "field_path": ["status"]}},
        ),
    ]
    if branch:
        decision = source("branch")["steps"][1]
        decision["paths"]["fail"] = [data["steps"][1]]
        data["steps"] = [data["steps"][0], decision]
    store, root, session, key = await verify_ready(
        tmp_path, "def test_fail():\n    assert " + str(passed) + "\n", data=data
    )
    from tests.architecture.workflow_verify_fixtures import FixtureCommand

    verifier = verify_adapter(
        store, root, session, command=FixtureCommand(root, passed) if injected_verify else None
    )
    await verifier.run_once(key)
    await tick(store, verifier)
    await tick(store, verifier)
    await tick(store, verifier)
    if branch:
        await tick(store, verifier)
        await tick(store, verifier)
    run = await store.get_workflow_run("run")
    step = next(s for s in run.steps if s.identity == StepIdentity("repair"))
    return store, root, session, invocation_id("run", step.identity, step.input_fingerprint)


def build(
    store,
    root,
    session,
    *,
    change=True,
    known=True,
    deny=False,
    scripts=None,
    limits=None,
    tools=None,
    context=None,
    profile=None,
):
    def completion():
        return ModelCompleted(
            "stop", input_tokens=10 if known else None, output_tokens=5 if known else None
        )

    if scripts is None:
        scripts = (
            [
                (
                    ModelToolCall(
                        ToolCall(
                            "edit-1",
                            "search_replace",
                            {"path": "test_sample.py", "old": "assert False", "new": "assert True"},
                        )
                    ),
                    completion(),
                ),
                (
                    ModelTextDelta("Changed the assertion; this is not verification PASS."),
                    completion(),
                ),
            ]
            if change
            else [(ModelTextDelta("No change necessary; not verified."), completion())]
        )
    provider = ScriptedProvider(scripts)
    registry = tools or ToolRegistry([SearchReplaceTool()])
    effective = None
    if profile is not None:
        from neuro_code.application.agents.composition import bind_agent_profile
        from neuro_code.application.execution_policy import ExecutionBudgetPolicy
        from neuro_code.application.ports.model import ModelCapability, ModelCapabilitySet
        from neuro_code.domain.conversation.reasoning import ReasoningEffort
        from neuro_code.domain.sandbox import SandboxProfile

        provider.capabilities = ModelCapabilitySet.from_supported(ModelCapability.FUNCTION_TOOLS)
        effective = bind_agent_profile(
            profile=profile,
            provider=provider,
            tools=registry,
            sandbox_profile=SandboxProfile.WORKSPACE,
            execution_budget=ExecutionBudgetPolicy.from_max_steps(4),
            reasoning_effort=ReasoningEffort.HIGH,
        )
    runtime = AgentRuntime(
        provider=provider,
        tools=registry,
        effective_agent_binding=effective,
        workspace_change_observer=EmptyWorkspaceChangeObserver(),
        permissions=PermissionManager(
            mode=PermissionMode.DONT_ASK if deny else PermissionMode.BYPASS,
            rules=(PermissionRule(PermissionEffect.DENY, "search_replace"),) if deny else (),
        ),
        tool_context=context or ToolContext(root),
        session_store=store,
        final_output_gate_enabled=False,
    )
    conversation = AgentConversation(runtime=runtime, store=store, session_id=session)
    binding = ConversationBinding(
        conversation, provider, workspace_root=root, effective_agent_binding=effective
    )
    repair_workspace = LocalWorkflowRepairWorkspace(workspace(root).reader)
    adapter = WorkflowRepairActivityAdapter(
        store=store,
        activities=store,
        turn=BoundWorkflowRepairTurn(binding),
        workspace=repair_workspace,
        limits=limits,
    )
    return adapter, provider, binding


@pytest.mark.parametrize("change", [True, False])
async def test_real_runtime_completion_and_interpreter_consumption(tmp_path, change):
    store, root, session, key = await ready(tmp_path)
    adapter, provider, binding = build(store, root, session, change=change)
    result = (await adapter.run_once(key)).attempt
    assert result.state is State.COMPLETED
    assert result.result.usage.model_calls == (2 if change else 1)
    assert result.result.usage.tool_calls == int(change)
    assert result.result.usage.input_tokens == (20 if change else 10)
    assert result.result.usage.output_tokens == (10 if change else 5)
    assert result.result.usage.wall_milliseconds > 0
    assert result.result.usage.generated_tasks == 0
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.WAITING
    assert ("assert True" in (root / "test_sample.py").read_text()) is change
    assert all(
        item.role.value == "system" for item in binding.runner.items
    )  # No internal user/assistant history.
    assert json.loads(result.result.output_json)["status"] == "COMPLETED"
    with closing(store._connect()) as connection:
        started = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_repair_executions WHERE invocation_id=?",
                (key,),
            ).fetchone()[0]
        )
        proof = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_repair_evidence WHERE execution_id=?",
                (started["execution_id"],),
            ).fetchone()[0]
        )["turn_terminal_proof"]
        event = connection.execute(
            "SELECT kind,data_json FROM events WHERE session_id=? AND sequence=?",
            (session, proof["event_sequence"]),
        ).fetchone()
    assert event is not None
    assert event[0] == "turn_completed"
    assert json.loads(event[1])["execution_status"] == "completed"
    assert proof["turn_id"] == started["turn_id"]
    assert proof["source"] == "workflow_repair"
    assert proof["input_fingerprint"] == started["turn_input_fingerprint"]
    assert (await tick(store)).action == "consume_activity"
    assert (await adapter.run_once(key)).attempt == result
    assert len(provider.calls) == (2 if change else 1)


async def test_runtime_outcome_none_uses_exact_durable_blocked_turn(tmp_path):
    from dataclasses import replace

    from neuro_code.domain.execution import (
        AgentExecutionOutcome,
        AgentExecutionStatus,
    )

    store, _root, session, key = await ready(tmp_path)
    adapter, provider, _ = build(store, _root, session)
    original = store.finalize_turn

    async def persist_blocked(*args, **kwargs):
        args = list(args)
        record = args[3]
        args[3] = replace(
            record,
            outcome=AgentExecutionOutcome(
                AgentExecutionStatus.BLOCKED,
                None,
                finalized=False,
                recoverable=False,
            ),
        )
        return await original(*args, **kwargs)

    store.finalize_turn = persist_blocked
    terminal = (await adapter.run_once(key)).attempt

    assert terminal.state is State.BLOCKED
    assert terminal.result.output_json is None
    assert (
        await store.load_execution_record(session)
    ).outcome.status is AgentExecutionStatus.BLOCKED
    assert (await tick(store)).action != "consume_activity"
    assert len(provider.calls) == 2


async def test_missing_runtime_finalization_never_settles_repair_completed(tmp_path):
    store, _root, session, key = await ready(tmp_path)
    adapter, provider, _ = build(store, _root, session)
    original = store.finalize_turn

    async def acknowledge_without_commit(*args, **kwargs):
        if len(args) > 4 and str(args[4]).startswith("repair-turn-"):
            return None
        return await original(*args, **kwargs)

    store.finalize_turn = acknowledge_without_commit
    terminal = (await adapter.run_once(key)).attempt

    assert terminal.state is State.INDETERMINATE
    assert terminal.result.output_json is None
    assert terminal.result.usage.model_calls == 2
    assert terminal.result.usage.tool_calls == 1
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION
    assert (await tick(store)).action != "consume_activity"
    assert len(provider.calls) == 2
    assert (await adapter.run_once(key)).attempt == terminal
    assert len(provider.calls) == 2


async def test_repair_proof_isolated_from_later_session_execution_record(tmp_path):
    from datetime import UTC, datetime

    from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
    from neuro_code.domain.execution import (
        AgentExecutionOutcome,
        AgentExecutionStatus,
        SessionExecutionRecord,
        TurnInput,
        TurnRecoveryAttempt,
    )

    store, root, session, key = await ready(tmp_path)
    adapter, _provider, _binding = build(store, root, session, change=False)
    terminal = (await adapter.run_once(key)).attempt
    later_turn_id = "later-unrelated-turn"
    now = datetime.now(UTC)
    await store.start_turn_attempt(
        TurnRecoveryAttempt.create(
            turn_id=later_turn_id,
            session_id=session,
            input=TurnInput("A later unrelated turn"),
            accepted_at=now,
        )
    )
    with closing(store._connect()) as connection:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE session_id=?", (session,)
        ).fetchone()[0]
    outcome = AgentExecutionOutcome(
        AgentExecutionStatus.COMPLETED, None, finalized=False, recoverable=False
    )
    await store.finalize_turn(
        session,
        AgentEvent.create(sequence, AgentEventKind.TURN_COMPLETED, {"turn_id": later_turn_id}),
        await store.load_session_items(session),
        SessionExecutionRecord(outcome, sequence, now),
        later_turn_id,
    )
    latest = await store.load_execution_record(session)
    assert latest is not None
    with closing(store._connect()) as connection:
        started = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_repair_executions WHERE invocation_id=?",
                (key,),
            ).fetchone()[0]
        )
        exact_sequence = json.loads(
            connection.execute(
                "SELECT payload_json FROM workflow_repair_evidence WHERE execution_id=?",
                (started["execution_id"],),
            ).fetchone()[0]
        )["turn_terminal_proof"]["event_sequence"]
    assert latest.event_sequence > exact_sequence
    replay = await store.reconcile_workflow_repair(
        key,
        parent_session_id=session,
        parent_workspace_root=root,
        updated_at=terminal.updated_at,
    )
    assert replay == terminal
    assert latest.event_sequence > exact_sequence
    assert await store.load_execution_record(session) == latest


@pytest.mark.parametrize("passed", [True, False])
async def test_pass_and_fake_fail_not_authority(tmp_path, passed):
    inputs = None if passed else {"failure": {"kind": "literal", "value": "FAIL"}}
    store, root, session, key = await ready(tmp_path, passed=passed, inputs=inputs)
    adapter, provider, _ = build(store, root, session)
    before = await store.get_workflow_run("run")
    with pytest.raises(WorkflowStateError):
        await adapter.run_once(key)
    assert await store.get_workflow_run("run") == before
    assert not provider.calls


async def test_stale_fail_refuses_before_claim(tmp_path):
    store, root, session, key = await ready(tmp_path)
    (root / "test_sample.py").write_text("different\n")
    adapter, provider, _ = build(store, root, session)
    with pytest.raises(WorkflowStateError, match="stale"):
        await adapter.run_once(key)
    assert (await store.get_workflow_activity(key)).state is State.READY
    assert not provider.calls


async def test_unknown_provider_tokens_are_never_zero(tmp_path):
    store, root, session, key = await ready(tmp_path)
    adapter, _, _ = build(store, root, session, change=False, known=False)
    result = (await adapter.run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.usage.input_tokens is None
    assert result.result.usage.output_tokens is None
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_permission_denial_no_tool_entry(tmp_path):
    store, root, session, key = await ready(tmp_path)
    adapter, _, _ = build(store, root, session, deny=True)
    result = (await adapter.run_once(key)).attempt
    assert result.state is State.BLOCKED
    assert result.result.usage.tool_calls == 0
    assert "assert False" in (root / "test_sample.py").read_text()


async def test_terminal_reopen_replay_does_not_execute(tmp_path):
    store, root, session, key = await ready(tmp_path)
    adapter, provider, _ = build(store, root, session)
    terminal = (await adapter.run_once(key)).attempt
    restarted = SqliteSessionStore(store.database_path)
    await restarted.initialize()
    result = await restarted.reconcile_workflow_repair(
        key, parent_session_id=session, parent_workspace_root=root, updated_at=terminal.updated_at
    )
    assert result == terminal
    assert len(provider.calls) == 2


async def test_provider_failure_is_counted_with_unknown_tokens(tmp_path):
    store, root, session, key = await ready(tmp_path)
    adapter, provider, _ = build(
        store, root, session, scripts=[(RuntimeError("provider disconnected"),)]
    )
    result = (await adapter.run_once(key)).attempt
    assert len(provider.calls) == 1
    assert result.result.usage.model_calls == 1
    assert result.result.usage.input_tokens is None
    assert result.state is State.INDETERMINATE


async def test_pre_dispatch_model_ceiling_and_actual_token_overrun(tmp_path):
    store, root, session, key = await ready(tmp_path)
    adapter, provider, _ = build(
        store, root, session, limits=WorkflowRepairLimits(model_calls=1, input_tokens=1)
    )
    result = (await adapter.run_once(key)).attempt
    assert len(provider.calls) == 1
    assert result.result.usage.model_calls == 1
    assert result.result.usage.input_tokens == 10
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_generic_terminal_refused_without_execution(tmp_path):
    from datetime import UTC, datetime

    from neuro_code.domain.workflows.activity import WorkflowActivityResult
    from neuro_code.domain.workflows.publication import canonical, digest
    from neuro_code.domain.workflows.state import BudgetAmounts

    store, _root, _session, key = await ready(tmp_path)
    current = await store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="attacker",
        reserved=BudgetAmounts(),
        updated_at=datetime.now(UTC),
    )
    current = await store.start_workflow_activity(
        key,
        expected_revision=current.revision,
        owner_id=current.owner_id,
        owner_fence=current.owner_fence,
        updated_at=datetime.now(UTC),
    )
    fake = WorkflowActivityResult(
        key,
        current.invocation.request_fingerprint,
        current.invocation.activity,
        State.COMPLETED,
        "fake",
        digest("fake"),
        BudgetAmounts(),
        datetime.now(UTC),
        canonical({"status": "COMPLETED", "response": "fake"}),
    )
    before = await store.get_workflow_run("run")
    with pytest.raises(WorkflowStateError):
        await store.finish_workflow_activity(
            fake,
            expected_revision=current.revision,
            owner_id=current.owner_id,
            owner_fence=current.owner_fence,
        )
    assert await store.get_workflow_run("run") == before


async def test_active_recovery_busy_and_cancellation_unknown(tmp_path):
    store, root, session, key = await ready(tmp_path)
    adapter, provider, _ = build(store, root, session, change=False)
    entered, release = asyncio.Event(), asyncio.Event()
    stream = provider.stream

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        async for event in stream(*args, **kwargs):
            yield event

    provider.stream = blocked
    task = asyncio.create_task(adapter.run_once(key))
    await asyncio.wait_for(entered.wait(), 5)
    second = SqliteSessionStore(store.database_path)
    second_adapter, second_provider, _ = build(second, root, session)
    assert (await second_adapter.run_once(key)).disposition == "busy"
    assert not second_provider.calls
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = (await second_adapter.run_once(key)).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.usage.model_calls is None
    assert result.result.usage.tool_calls is None
    assert not second_provider.calls
