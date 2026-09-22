from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from neuro_code.application.memory.compaction import ProviderContextWindow
from neuro_code.application.memory.compaction_runtime import (
    ContextPreflightStatus,
    assess_context_preflight,
)
from neuro_code.application.permissions.policy import PermissionManager, PermissionMode
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.tools import (
    Tool,
    ToolContext,
    ToolOutputArtifact,
    ToolOutputArtifactStore,
)
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.supervision import (
    ExecutionControlMode,
    stable_observation_digest,
)
from neuro_code.application.runtime.tool_pipeline import ToolExecutor
from neuro_code.application.runtime.tool_result_guard import project_tool_result
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import Message, Role, ToolCall
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.tools import (
    ToolDefinition,
    ToolExecutionResult,
    ToolResult,
    ToolResultProjectionStrategy,
)
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.infrastructure.tools.bash import BashTool
from tests.fakes import EmptyWorkspaceChangeObserver


class _ResultTool:
    def __init__(
        self,
        name: str,
        result: ToolResult,
        *,
        side_effecting: bool = False,
    ) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Return the {name} fixture result.",
            input_schema={"type": "object", "additionalProperties": False},
        )
        self.side_effecting = side_effecting
        self._result = result
        self.calls = 0

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        self.calls += 1
        return self._result


class _ToolCollection:
    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def has_synthetic_intent(self, name: str) -> bool:
        """Mirror the registry rule for this built-in-only test collection."""

        tool = self._tools.get(name)
        if tool is None:
            return False
        properties = tool.definition.input_schema.get("properties") or {}
        return getattr(tool, "side_effecting", False) and "intent" not in properties


class _RecordingHook:
    def __init__(self) -> None:
        self.results: list[ToolExecutionResult] = []

    async def before_tool(
        self,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        side_effecting: bool,
    ) -> None:
        del call_id, tool_name, arguments, side_effecting

    async def after_tool(self, result: ToolExecutionResult) -> None:
        self.results.append(result)


class _NoDuplicateArtifactStore:
    def __init__(self) -> None:
        self.calls = 0

    async def save(
        self,
        *,
        tool_name: str,
        content: bytes,
        content_truncated: bool = False,
    ) -> ToolOutputArtifact:
        del tool_name, content, content_truncated
        self.calls += 1
        raise AssertionError("result guard must not create a duplicate artifact")


class _CountingArtifactStore:
    def __init__(self, delegate: ToolOutputArtifactStore) -> None:
        self._delegate = delegate
        self.calls = 0

    async def save(
        self,
        *,
        tool_name: str,
        content: bytes,
        content_truncated: bool = False,
    ) -> ToolOutputArtifact:
        self.calls += 1
        return await self._delegate.save(
            tool_name=tool_name,
            content=content,
            content_truncated=content_truncated,
        )


class _ScriptedProvider:
    provider_name = "fixture-provider"
    model_name = "fixture-model"
    context_affinity = "fixture-affinity"

    def __init__(self, scripts: Sequence[Sequence[ModelEvent]]) -> None:
        self._scripts = list(scripts)
        self.calls: list[ModelContext] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tools, tool_policy
        self.calls.append(context)
        for event in self._scripts.pop(0):
            yield event


class ToolResultGuardTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _executor(
        tool: Tool,
        context: ToolContext,
        *,
        hooks: Sequence[_RecordingHook] = (),
    ) -> ToolExecutor:
        return ToolExecutor(
            tools=_ToolCollection((tool,)),
            permissions=PermissionManager(mode=PermissionMode.BYPASS),
            approver=None,
            tool_context=context,
            session_store=None,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            context_builder=ContextBuilder(
                reasoning_effort=ReasoningEffort.HIGH,
                interaction_mode=InteractionMode.AUTO,
                plan=None,
                instruction_provider=None,
                skill_provider=None,
            ),
            hooks=hooks,
        )

    @staticmethod
    async def _execute(
        executor: ToolExecutor,
        call: ToolCall,
    ) -> tuple[list[Message], list[AgentEvent], object | None]:
        messages: list[Message] = []
        context_items: list[Message] = []
        events: list[AgentEvent] = []

        async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
            event = AgentEvent.create(len(events) + 1, kind, data)
            events.append(event)
            return event

        observation = await executor.execute(
            call,
            messages,
            context_items,
            emit,
            "fixture-session",
        )
        return messages, events, observation

    def test_small_result_passes_through_unchanged(self) -> None:
        result = project_tool_result(ToolResult("small result"), byte_limit=256)

        self.assertEqual(result.content, "small result")
        self.assertFalse(result.truncated)
        self.assertEqual(result.strategy, ToolResultProjectionStrategy.PASS_THROUGH)
        self.assertEqual(result.omitted_bytes, 0)
        self.assertEqual(result.to_metadata()["activated"], False)

    async def test_oversized_result_keeps_canonical_truth_and_bounds_model_message(self) -> None:
        full = "HEAD: command started\n" + ("middle evidence\n" * 300) + "TAIL: exit_code=17"
        hook = _RecordingHook()
        tool = _ResultTool("inspect", ToolResult(full))
        with tempfile.TemporaryDirectory() as directory:
            context = ToolContext(Path(directory), output_byte_limit=512)
            messages, events, observation = await self._execute(
                self._executor(tool, context, hooks=(hook,)),
                ToolCall("inspect-1", "inspect", {}),
            )

        model_message = messages[-1]
        self.assertLessEqual(len(model_message.content.encode("utf-8")), 512)
        self.assertNotEqual(model_message.content, full)
        self.assertIn("HEAD: command started", model_message.content)
        self.assertIn("TAIL: exit_code=17", model_message.content)
        self.assertIn("truncated", model_message.content)
        self.assertEqual(hook.results[0].content, full)
        completion = next(event for event in events if event.kind is AgentEventKind.TOOL_COMPLETED)
        self.assertEqual(completion.data["content"], full)
        self.assertEqual(completion.data["execution_result"]["content"], full)  # type: ignore[index]
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(
            observation.observation_digest,
            stable_observation_digest(is_error=False, content=full),
        )
        projection = completion.data["model_context_projection"]
        self.assertTrue(projection["activated"])  # type: ignore[index]
        self.assertNotIn(full, str(projection))

    async def test_redaction_precedes_canonical_and_model_result_projection(self) -> None:
        secret = "credential-without-a-recognizable-shape"
        full = f"HEAD {secret}\n" + ("diagnostic\n" * 300) + f"TAIL {secret}"
        hook = _RecordingHook()
        tool = _ResultTool("redacted", ToolResult(full))
        with tempfile.TemporaryDirectory() as directory:
            messages, events, _observation = await self._execute(
                self._executor(
                    tool,
                    ToolContext(
                        Path(directory),
                        output_byte_limit=512,
                        redaction_values=(secret,),
                    ),
                    hooks=(hook,),
                ),
                ToolCall("redacted-1", "redacted", {}),
            )

        completed = next(event for event in events if event.kind is AgentEventKind.TOOL_COMPLETED)
        self.assertNotIn(secret, hook.results[0].content)
        self.assertIn("[REDACTED]", hook.results[0].content)
        self.assertNotIn(secret, completed.data["content"])
        self.assertIn("[REDACTED]", completed.data["content"])
        self.assertNotIn(secret, messages[-1].content)
        self.assertIn("[REDACTED]", messages[-1].content)

    async def test_error_projection_preserves_actionable_tail(self) -> None:
        full = "diagnostic preamble\n" + ("noise\n" * 300) + "ERROR: exit status 23"
        tool = _ResultTool("failed", ToolResult(full, is_error=True))
        with tempfile.TemporaryDirectory() as directory:
            messages, events, _observation = await self._execute(
                self._executor(tool, ToolContext(Path(directory), output_byte_limit=512)),
                ToolCall("failed-1", "failed", {}),
            )

        self.assertTrue(messages[-1].content)
        self.assertIn("tool error output truncated", messages[-1].content)
        self.assertIn("ERROR: exit status 23", messages[-1].content)
        failed = next(event for event in events if event.kind is AgentEventKind.TOOL_FAILED)
        self.assertTrue(failed.data["is_error"])
        self.assertEqual(failed.data["content"], full)

    async def test_fake_artifact_metadata_does_not_claim_runtime_artifact(self) -> None:
        artifact_store = _NoDuplicateArtifactStore()
        full = "HEAD\n" + ("artifact output\n" * 300) + "TAIL"
        fake_artifact_id = "f" * 32
        tool = _ResultTool(
            "artifact_tool",
            ToolResult(
                full,
                metadata={
                    "output_artifact_id": fake_artifact_id,
                    "output_artifact_path": f"tool-output/{fake_artifact_id}.log",
                    "output_artifact_bytes": len(full.encode("utf-8")),
                    "output_artifact_truncated": True,
                },
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            messages, events, _observation = await self._execute(
                self._executor(
                    tool,
                    ToolContext(
                        Path(directory),
                        output_byte_limit=512,
                        output_artifact_store=artifact_store,
                    ),
                ),
                ToolCall("artifact-1", "artifact_tool", {}),
            )

        self.assertEqual(artifact_store.calls, 0)
        self.assertNotIn(fake_artifact_id, messages[-1].content)
        failed_or_completed = next(
            event
            for event in events
            if event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}
        )
        projection = failed_or_completed.data["model_context_projection"]
        self.assertFalse(projection["artifact_available"])  # type: ignore[index]
        self.assertIn("no fuller artifact is available", messages[-1].content)

    async def test_runtime_bash_artifact_is_reported_without_duplicate_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delegate = FileToolOutputArtifactStore(root / "tool-output", max_bytes=4096)
            artifact_store = _CountingArtifactStore(delegate)
            messages, events, _observation = await self._execute(
                self._executor(
                    BashTool(),
                    ToolContext(
                        root,
                        output_byte_limit=128,
                        output_artifact_store=artifact_store,
                    ),
                ),
                ToolCall("bash-1", "bash", {"command": "echo " + ("x" * 2048)}),
            )
            completed = next(
                event for event in events if event.kind is AgentEventKind.TOOL_COMPLETED
            )
            metadata = completed.data["metadata"]
            assert isinstance(metadata, Mapping)
            artifact_path = metadata["output_artifact_path"]
            assert isinstance(artifact_path, str)
            self.assertTrue((root / artifact_path).is_file())

        self.assertEqual(artifact_store.calls, 1)
        projection = completed.data["model_context_projection"]
        self.assertTrue(projection["artifact_available"])  # type: ignore[index]
        self.assertIn("a fuller artifact is available", messages[-1].content)

    def test_token_and_byte_bounds_are_used_without_provider_specific_accounting(self) -> None:
        full = "x" * 100_000
        projection = project_tool_result(ToolResult(full), byte_limit=200_000)
        self.assertLessEqual(projection.projected_bytes, 64 * 1024)
        self.assertLessEqual(projection.projected_estimated_tokens, 16 * 1024)
        self.assertEqual(projection.original_estimated_tokens, 30_000)

    def test_multibyte_head_tail_projection_is_valid_utf8(self) -> None:
        full = "开头\n" + ("中间🙂\n" * 300) + "结尾"

        projection = project_tool_result(ToolResult(full), byte_limit=512)

        self.assertLessEqual(projection.projected_bytes, 512)
        self.assertEqual(
            projection.content,
            projection.content.encode("utf-8").decode("utf-8"),
        )
        self.assertIn("开头", projection.content)
        self.assertIn("结尾", projection.content)

    async def test_rejected_tool_pairing_uses_the_same_bounded_projection(self) -> None:
        reason = "control batch rejected: " + ("diagnostic " * 300) + "final action denied"
        tool = _ResultTool("control", ToolResult("unused"))
        with tempfile.TemporaryDirectory() as directory:
            executor = self._executor(tool, ToolContext(Path(directory), output_byte_limit=512))
            messages: list[Message] = []
            context_items: list[Message] = []
            events: list[AgentEvent] = []

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                event = AgentEvent.create(len(events) + 1, kind, data)
                events.append(event)
                return event

            await executor.record_rejected_tool_calls(
                (ToolCall("control-1", "control", {}),),
                messages,
                context_items,
                emit,
                reason=reason,
            )

        self.assertLessEqual(len(messages[0].content.encode("utf-8")), 512)
        self.assertIn("tool error output truncated", messages[0].content)
        self.assertIn("final action denied", messages[0].content)
        self.assertEqual(events[0].data["content"], reason)
        self.assertTrue(events[0].data["model_context_projection"]["activated"])  # type: ignore[index]

    async def test_unstarted_tool_pairing_uses_the_same_projection_boundary(self) -> None:
        tool = _ResultTool("cancelled", ToolResult("unused"))
        with tempfile.TemporaryDirectory() as directory:
            executor = self._executor(
                tool,
                ToolContext(Path(directory), output_byte_limit=512),
            )
            messages: list[Message] = []
            context_items: list[Message] = []
            events: list[AgentEvent] = []

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                event = AgentEvent.create(len(events) + 1, kind, data)
                events.append(event)
                return event

            await executor.record_unstarted_tool_calls(
                (ToolCall("cancelled-1", "cancelled", {}),),
                messages,
                context_items,
                emit,
                cancelled=True,
            )

        self.assertEqual(messages[0].content, "tool call cancelled before execution")
        self.assertEqual(events[0].data["content"], messages[0].content)
        self.assertTrue(events[0].data["cancelled"])
        self.assertTrue(events[0].data["not_started"])
        self.assertFalse(events[0].data["model_context_projection"]["activated"])  # type: ignore[index]

    def test_projected_message_reduces_the_next_preflight_request(self) -> None:
        full = "x" * 20_000
        projection = project_tool_result(ToolResult(full), byte_limit=512)
        definition = ToolDefinition(
            "inspect",
            "inspect fixture",
            {"type": "object", "additionalProperties": False},
        )
        provider_window = ProviderContextWindow("fixture", "model", 4_096, "fixture-affinity")
        full_context = ModelContext((Message(Role.TOOL, full, name="inspect", tool_call_id="1"),))
        bounded_context = ModelContext(
            (Message(Role.TOOL, projection.content, name="inspect", tool_call_id="1"),)
        )
        full_preflight = assess_context_preflight(
            context=full_context,
            tools=(definition,),
            provider="fixture",
            model="model",
            context_affinity="fixture-affinity",
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=provider_window,
            max_output_tokens=128,
        )
        bounded_preflight = assess_context_preflight(
            context=bounded_context,
            tools=(definition,),
            provider="fixture",
            model="model",
            context_affinity="fixture-affinity",
            reasoning_effort=ReasoningEffort.HIGH,
            provider_window=provider_window,
            max_output_tokens=128,
        )

        self.assertGreater(
            full_preflight.estimated_input_tokens, bounded_preflight.estimated_input_tokens
        )
        self.assertNotEqual(full_preflight.status, ContextPreflightStatus.SAFE)
        self.assertEqual(bounded_preflight.status, ContextPreflightStatus.SAFE)

    async def test_runtime_preflight_uses_bounded_result_and_parallel_order_is_preserved(
        self,
    ) -> None:
        first = (
            ModelToolCall(ToolCall("a", "first", {})),
            ModelToolCall(ToolCall("b", "second", {})),
            ModelCompleted("tool_calls"),
        )
        provider = _ScriptedProvider((first, (ModelTextDelta("done"), ModelCompleted("stop"))))
        first_tool = _ResultTool("first", ToolResult("FIRST\n" + ("x" * 20_000) + "\nFIRST-END"))
        second_tool = _ResultTool(
            "second", ToolResult("SECOND\n" + ("y" * 20_000) + "\nSECOND-END")
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=_ToolCollection((first_tool, second_tool)),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(Path(directory), output_byte_limit=512),
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                final_output_gate_enabled=False,
                normal_requirements_enabled=False,
                provider_context_window=ProviderContextWindow(
                    "fixture-provider",
                    "fixture-model",
                    4_096,
                    "fixture-affinity",
                ),
                provider_max_output_tokens=128,
            )
            result = await runtime.run("inspect both outputs")

        self.assertEqual(result.response, "done")
        self.assertEqual(len(provider.calls), 2)
        tool_messages = [
            message for message in provider.calls[1].messages if message.role is Role.TOOL
        ]
        self.assertEqual([message.tool_call_id for message in tool_messages], ["a", "b"])
        self.assertLessEqual(
            max(len(message.content.encode("utf-8")) for message in tool_messages),
            512,
        )
        self.assertIn("FIRST-END", tool_messages[0].content)
        self.assertIn("SECOND-END", tool_messages[1].content)
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertTrue(preflights)
        self.assertEqual(preflights[-1].data["status"], ContextPreflightStatus.SAFE.value)

    async def test_parallel_results_use_an_aggregate_budget_before_next_preflight(self) -> None:
        names = tuple(f"inspect_{index}" for index in range(8))
        calls = tuple(ToolCall(f"call-{index}", name, {}) for index, name in enumerate(names))
        first = (*tuple(ModelToolCall(call) for call in calls), ModelCompleted("tool_calls"))
        provider = _ScriptedProvider((first, (ModelTextDelta("done"), ModelCompleted("stop"))))
        tools = tuple(
            _ResultTool(
                name,
                ToolResult(f"{name}-HEAD\n" + ("x" * 50_000) + f"\n{name}-TAIL"),
            )
            for name in names
        )
        provider_window = ProviderContextWindow(
            "fixture-provider",
            "fixture-model",
            16_384,
            "fixture-affinity",
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                provider=provider,
                tools=_ToolCollection(tools),
                workspace_change_observer=EmptyWorkspaceChangeObserver(),
                permissions=PermissionManager(mode=PermissionMode.BYPASS),
                tool_context=ToolContext(Path(directory), output_byte_limit=200_000),
                system_prompt="system",
                execution_control_mode=ExecutionControlMode.FINALIZE_TERMINAL,
                final_output_gate_enabled=False,
                normal_requirements_enabled=False,
                provider_context_window=provider_window,
                provider_max_output_tokens=128,
            )
            result = await runtime.run("inspect all outputs")

        for tool in tools:
            individual = project_tool_result(tool._result, byte_limit=200_000)
            self.assertFalse(individual.truncated)
        unguarded = ModelContext(
            (
                *provider.calls[0].items,
                Message(Role.ASSISTANT, tool_calls=calls),
                *(
                    Message(
                        Role.TOOL,
                        tool._result.content,
                        name=tool.definition.name,
                        tool_call_id=call.id,
                    )
                    for tool, call in zip(tools, calls, strict=True)
                ),
            ),
            provider.calls[0].source_provider,
            provider.calls[0].source_model,
            provider.calls[0].source_context_affinity,
            provider.calls[0].reasoning_effort,
        )
        unguarded_preflight = assess_context_preflight(
            context=unguarded,
            tools=_ToolCollection(tools).definitions(),
            provider="fixture-provider",
            model="fixture-model",
            context_affinity="fixture-affinity",
            reasoning_effort=unguarded.reasoning_effort,
            provider_window=provider_window,
            max_output_tokens=128,
        )
        self.assertNotEqual(unguarded_preflight.status, ContextPreflightStatus.SAFE)
        self.assertEqual(result.response, "done")
        self.assertEqual(len(provider.calls), 2)
        model_tool_messages = [
            message for message in provider.calls[1].messages if message.role is Role.TOOL
        ]
        self.assertEqual(
            [message.tool_call_id for message in model_tool_messages],
            [call.id for call in calls],
        )
        self.assertEqual(
            [message.name for message in model_tool_messages],
            list(names),
        )
        self.assertTrue(all(message.content for message in model_tool_messages))
        self.assertTrue(
            all(
                f"{name}-HEAD" in message.content
                for name, message in zip(names, model_tool_messages, strict=True)
            )
        )
        self.assertTrue(
            all(
                f"{name}-TAIL" in message.content
                for name, message in zip(names, model_tool_messages, strict=True)
            )
        )
        preflights = [
            event for event in result.events if event.kind is AgentEventKind.CONTEXT_PREFLIGHT
        ]
        self.assertEqual(preflights[-1].data["status"], ContextPreflightStatus.SAFE.value)
        for tool, call in zip(tools, calls, strict=True):
            self.assertEqual(tool.calls, 1)
            event = next(
                event
                for event in result.events
                if event.data.get("id") == call.id
                and event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}
            )
            self.assertEqual(event.data["content"], tool._result.content)


if __name__ == "__main__":
    unittest.main()
