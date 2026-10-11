"""Real Runtime/security boundaries with offline provider and injected VERIFY exit.

No commercial provider or production sandbox is bypassed by these fixtures.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from neuro_code.application.ports.tools import ToolContext
from neuro_code.domain.conversation.events import ModelCompleted, ModelTextDelta, ModelToolCall
from neuro_code.domain.conversation.messages import ToolCall
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.state import WorkflowChange, WorkflowEventKind, WorkflowStatus
from neuro_code.infrastructure.tools.bash import BashTool
from neuro_code.infrastructure.tools.registry import ToolRegistry
from tests.architecture.test_workflow_repair_adapter import build, ready


def done():
    return ModelCompleted("stop", input_tokens=10, output_tokens=5)


async def test_exact_readonly_parent_profile_is_not_augmented(tmp_path):
    from neuro_code.application.agents.profiles import EXPLORER_AGENT_PROFILE

    store, root, session, key = await ready(tmp_path, injected_verify=True)
    adapter, _, binding = build(store, root, session, profile=EXPLORER_AGENT_PROFILE)
    terminal = (await adapter.run_once(key)).attempt
    assert binding.effective_agent_binding.profile is EXPLORER_AGENT_PROFILE
    assert "search_replace" not in binding.effective_agent_binding.bound_tool_names
    assert terminal.state is not State.COMPLETED
    assert terminal.result.usage.tool_calls == 0
    assert "assert False" in (root / "test_sample.py").read_text()


async def test_required_sandbox_unavailable_never_spawns_shell(tmp_path):
    store, root, session, key = await ready(tmp_path, injected_verify=True)
    marker = root / "unauthorized-shell"
    adapter, _, _ = build(
        store,
        root,
        session,
        tools=ToolRegistry([BashTool()]),
        context=ToolContext(root, sandbox_profile=SandboxProfile.WORKSPACE),
        scripts=[
            (
                ModelToolCall(
                    ToolCall("shell", "bash", {"command": "echo unsafe > unauthorized-shell"})
                ),
                done(),
            ),
            (ModelTextDelta("No authorized shell was run."), done()),
        ],
    )
    terminal = (await adapter.run_once(key)).attempt
    assert terminal.state is not State.COMPLETED
    assert terminal.result.usage.tool_calls == 1  # Entered real tool; launcher failed closed.
    assert not marker.exists()


async def test_actual_file_tool_error_is_counted(tmp_path):
    store, root, session, key = await ready(tmp_path, injected_verify=True)
    adapter, _, _ = build(
        store,
        root,
        session,
        scripts=[
            (
                ModelToolCall(
                    ToolCall(
                        "bad-edit",
                        "search_replace",
                        {"path": "test_sample.py", "old": "not present", "new": "replacement"},
                    )
                ),
                done(),
            ),
            (ModelTextDelta("Unable to edit."), done()),
        ],
    )
    result = (await adapter.run_once(key)).attempt
    assert result.state is not State.COMPLETED
    assert result.result.usage.tool_calls == 1
    assert result.result.usage.model_calls == 2
    assert "assert False" in (root / "test_sample.py").read_text()


async def test_external_workspace_change_during_model_is_not_owned_change(tmp_path):
    store, root, session, key = await ready(tmp_path, injected_verify=True)
    adapter, provider, _ = build(store, root, session, change=False)
    original = provider.stream
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(*args, **kwargs):
        entered.set()
        await release.wait()
        async for value in original(*args, **kwargs):
            yield value

    provider.stream = stream
    task = asyncio.create_task(adapter.run_once(key))
    await asyncio.wait_for(entered.wait(), 15)
    (root / "test_sample.py").write_text("external change\n")
    release.set()
    result = (await task).attempt
    assert result.state is State.INDETERMINATE
    assert result.result.usage.model_calls is None
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.NEEDS_ATTENTION
    assert (root / "test_sample.py").read_text() == "external change\n"


async def test_cancelled_workflow_cannot_dispatch_subsequent_tool(tmp_path):
    from datetime import UTC, datetime

    store, root, session, key = await ready(tmp_path, injected_verify=True)
    adapter, provider, _ = build(store, root, session)
    original = provider.stream
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(*args, **kwargs):
        entered.set()
        await release.wait()
        async for value in original(*args, **kwargs):
            yield value

    provider.stream = stream
    task = asyncio.create_task(adapter.run_once(key))
    await asyncio.wait_for(entered.wait(), 15)
    run = await store.get_workflow_run("run")
    await store.transition_workflow_run(
        run.run_id,
        WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        request_id="cancel-repair",
        updated_at=datetime.now(UTC),
    )
    release.set()
    with suppress(asyncio.CancelledError):
        await task
    result = (await adapter.run_once(key)).attempt
    assert result.state is not State.COMPLETED
    assert "assert False" in (root / "test_sample.py").read_text()
    assert (await store.get_workflow_run("run")).status is WorkflowStatus.CANCELLED
