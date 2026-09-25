from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from itertools import pairwise
from typing import Any

import httpx
import pytest

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.runtime.cache_continuity import CacheContinuityState
from neuro_code.application.runtime.context_builder import ContextBuilder
from neuro_code.application.runtime.model_step import ModelStepProcessor
from neuro_code.application.runtime.projection_journal import ProviderProjectionJournal
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelCompleted,
    ModelInputTokenSemantics,
    ModelRequestTrajectoryObserved,
    ModelUsage,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason, ToolCall
from neuro_code.domain.conversation.prompt_continuity import (
    CacheBoundaryReason,
    ModelRequestSource,
)
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.domain.workspace.instructions import (
    InstructionDiscoveryResult,
    InstructionFile,
)
from neuro_code.domain.workspace.skills import (
    SkillDiscoveryResult,
    SkillInfo,
    SkillScope,
)
from neuro_code.infrastructure.providers.anthropic import AnthropicProvider
from neuro_code.infrastructure.providers.gemini import GeminiProvider
from neuro_code.infrastructure.providers.gemini_interactions import GeminiInteractionsProvider
from neuro_code.infrastructure.providers.openai_compatible import OpenAICompatibleProvider
from neuro_code.infrastructure.providers.openai_responses import OpenAIResponsesProvider
from neuro_code.infrastructure.providers.request_trajectory import (
    MAX_TRAJECTORY_FINGERPRINTS,
    ProviderRequestTrajectoryRecorder,
)
from neuro_code.infrastructure.tools.registry import ToolRegistry


def _wire_message(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def _serialized_context(context: tuple[object, ...]) -> tuple[tuple[str, str, str | None], ...]:
    return tuple(
        (
            item.role.value,
            item.content,
            item.synthetic_reason.value if item.synthetic_reason is not None else None,
        )
        for item in context
        if isinstance(item, Message)
    )


def test_wire_trajectory_is_body_free_and_reports_append_only_prefix() -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"test-key-material-0123456789")
    context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="binding-fixture",
        prompt_trajectory_enabled=True,
    )
    first_body = {
        "model": "fixture-model",
        "messages": [
            _wire_message("system", "private system"),
            _wire_message("user", "secret prompt"),
        ],
        "tools": [{"type": "function", "name": "read_file"}],
    }
    second_body = {
        **first_body,
        "messages": [
            *first_body["messages"],
            _wire_message("assistant", "new suffix"),
        ],
    }

    first = recorder.observe(
        first_body,
        context=context,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    second = recorder.observe(
        second_body,
        context=context,
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert first is not None
    assert second is not None
    assert second.sequence == 2
    assert second.common_prefix_messages == 2
    assert second.previous_message_count == 2
    assert second.first_divergence_index is None
    assert second.append_only is True
    serialized = repr(second.to_event_data())
    assert "secret prompt" not in serialized
    assert "private system" not in serialized
    assert "new suffix" not in serialized
    assert second.request_fingerprint != first.request_fingerprint
    same_input_recorder = ProviderRequestTrajectoryRecorder(
        fingerprint_key=b"test-key-material-0123456789"
    )
    same_input = same_input_recorder.observe(
        first_body,
        context=context,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    assert same_input is not None
    assert same_input.request_fingerprint == first.request_fingerprint


def test_wire_trajectory_keeps_independent_baselines_for_interleaved_sources() -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"test-key-material-0123456789")
    main_context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="interleaved-source-fixture",
        prompt_trajectory_enabled=True,
    )
    auxiliary_context = ModelContext(
        (),
        request_source=ModelRequestSource.FINALIZER,
        trajectory_id="interleaved-source-fixture",
        prompt_trajectory_enabled=True,
    )
    prefix = [_wire_message("system", "stable"), _wire_message("user", "question")]

    first_main = recorder.observe(
        {"messages": prefix, "tools": []},
        context=main_context,
        provider="p",
        model="m",
    )
    finalizer = recorder.observe(
        {"messages": [_wire_message("system", "finalize"), _wire_message("user", "answer")]},
        context=auxiliary_context,
        provider="p",
        model="m",
    )
    second_main = recorder.observe(
        {"messages": [*prefix, _wire_message("assistant", "new suffix")], "tools": []},
        context=main_context,
        provider="p",
        model="m",
    )

    assert first_main is not None
    assert first_main.sequence == 1
    assert finalizer is not None
    assert finalizer.sequence == 1
    assert second_main is not None
    assert second_main.sequence == 2
    assert second_main.common_prefix_messages == 2
    assert second_main.append_only is True


def test_wire_trajectory_detects_retroactive_insert_and_epoch_boundary() -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"test-key-material-0123456789")
    context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="binding-fixture",
        prompt_trajectory_enabled=True,
    )
    prefix = [_wire_message("system", "stable"), _wire_message("user", "question")]
    recorder.observe(
        {"messages": prefix, "tools": []},
        context=context,
        provider="p",
        model="m",
    )
    inserted = recorder.observe(
        {"messages": [prefix[0], _wire_message("user", "inserted"), prefix[1]], "tools": []},
        context=context,
        provider="p",
        model="m",
    )
    boundary_context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="binding-fixture",
        cache_epoch=1,
        cache_boundary_reason=CacheBoundaryReason.FULL_COMPACTION,
        prompt_trajectory_enabled=True,
    )
    after_boundary = recorder.observe(
        {"messages": [_wire_message("system", "new boundary")], "tools": []},
        context=boundary_context,
        provider="p",
        model="m",
    )

    assert inserted is not None
    assert inserted.append_only is False
    assert inserted.first_divergence_index == 1
    assert after_boundary is not None
    assert after_boundary.append_only is None
    assert after_boundary.previous_message_count is None
    assert after_boundary.boundary_reason is CacheBoundaryReason.FULL_COMPACTION


def test_wire_trajectory_disables_comparison_above_bounded_fingerprint_limit() -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"test-key-material-0123456789")
    context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="binding-fixture",
        prompt_trajectory_enabled=True,
    )
    messages = [
        _wire_message("user", str(index)) for index in range(MAX_TRAJECTORY_FINGERPRINTS + 1)
    ]
    event = recorder.observe(
        {"messages": messages, "tools": []},
        context=context,
        provider="p",
        model="m",
    )

    assert event is not None
    assert event.fingerprints_truncated is True
    assert event.message_fingerprints == ()
    assert event.append_only is None
    assert event.request_fingerprint is None
    next_event = recorder.observe(
        {"messages": [_wire_message("user", "next")], "tools": []},
        context=context,
        provider="p",
        model="m",
    )
    assert next_event is not None
    assert next_event.sequence == 2
    assert next_event.previous_message_count == MAX_TRAJECTORY_FINGERPRINTS + 1
    assert next_event.append_only is None


def test_cache_continuity_state_advances_only_for_typed_boundaries() -> None:
    state = CacheContinuityState()
    assert state.bind("session-a", "project-a", 0) is CacheBoundaryReason.NEW_BINDING
    trajectory_id = state.trajectory_id
    assert state.cache_epoch == 0
    assert state.bind("session-a", "project-a", 0) is None
    assert state.observe_provider("deepseek", "deepseek-v4-flash") is None
    assert state.observe_provider("openai", "gpt-fixture") is CacheBoundaryReason.PROVIDER_SWITCH
    assert state.cache_epoch == 1
    state.consume_boundary()
    assert state.pending_boundary is None
    assert state.bind("session-a", "project-b", 0) is CacheBoundaryReason.PROJECT_SCOPE_CHANGE
    assert state.cache_epoch == 2
    assert state.trajectory_id == trajectory_id
    state.bind("session-b", "project-b", 0)
    assert state.trajectory_id != trajectory_id
    assert state.cache_epoch == 0


def test_model_usage_cache_reuse_ratio_uses_only_exact_reported_denominator() -> None:
    assert ModelUsage(input_tokens=1_000, cache_read_tokens=700).cache_reuse_ratio == 0.7
    assert ModelUsage(input_tokens=0, cache_read_tokens=0).cache_reuse_ratio is None
    assert ModelUsage(input_tokens=10, cache_read_tokens=20).cache_reuse_ratio is None
    assert ModelUsage(input_tokens=30, cache_read_tokens=70).cache_reuse_ratio is None
    anthropic = ModelUsage(
        input_tokens=30,
        cache_read_tokens=60,
        cache_write_tokens=10,
        input_token_semantics=ModelInputTokenSemantics.UNCACHED_TAIL,
    )
    assert anthropic.cache_reuse_ratio == 0.6
    assert (
        ModelUsage(
            input_tokens=30,
            cache_read_tokens=60,
            input_token_semantics=ModelInputTokenSemantics.UNCACHED_TAIL,
        ).cache_reuse_ratio
        is None
    )


def test_projection_journal_preserves_working_set_and_runtime_revision_order() -> None:
    builder = ContextBuilder(
        reasoning_effort=ReasoningEffort.HIGH,
        interaction_mode=InteractionMode.NORMAL,
        plan=None,
        instruction_provider=None,
        skill_provider=None,
    )
    canonical_1 = (Message(Role.SYSTEM, "system"), Message(Role.USER, "question"))
    working_set_1 = Message(
        Role.USER,
        "Working set revision 1",
        synthetic_reason=SyntheticReason.WORKING_SET,
    )
    request_1 = builder.build(canonical_1, working_set_message=working_set_1)
    canonical_2 = (*canonical_1, Message(Role.ASSISTANT, "tool call"), Message(Role.TOOL, "result"))
    request_2 = builder.build(canonical_2, working_set_message=working_set_1)
    working_set_2 = Message(
        Role.USER,
        "Working set revision 2 supersedes revision 1",
        synthetic_reason=SyntheticReason.WORKING_SET,
    )
    request_3 = builder.build(
        (*canonical_2, Message(Role.ASSISTANT, "continuing")),
        working_set_message=working_set_2,
    )

    first = _serialized_context(request_1)
    second = _serialized_context(request_2)
    third = _serialized_context(request_3)
    assert second[: len(first)] == first
    assert third[: len(second)] == second
    assert [text for _, text, reason in third if reason == SyntheticReason.WORKING_SET.value] == [
        "Working set revision 1",
        "Working set revision 2 supersedes revision 1",
    ]
    assert canonical_1 == (Message(Role.SYSTEM, "system"), Message(Role.USER, "question"))


def test_instruction_and_skill_scope_changes_append_revisions() -> None:
    instruction_values = [
        InstructionDiscoveryResult((InstructionFile("AGENTS.md", "root rules", 0),), (), "a"),
        InstructionDiscoveryResult(
            (
                InstructionFile("AGENTS.md", "root rules", 0),
                InstructionFile("backend/AGENTS.md", "backend rules", 1),
            ),
            (),
            "b",
        ),
    ]
    skill_values = [
        SkillDiscoveryResult(
            (
                SkillInfo(
                    "review",
                    "Review changes",
                    None,
                    ".neuro/skills/review/SKILL.md",
                    SkillScope.LOCAL,
                    0,
                ),
            ),
            (),
            "a",
        ),
        SkillDiscoveryResult(
            (
                SkillInfo(
                    "test", "Run tests", None, ".neuro/skills/test/SKILL.md", SkillScope.LOCAL, 0
                ),
            ),
            (),
            "b",
        ),
    ]

    def instruction_provider() -> InstructionDiscoveryResult:
        return instruction_values[0]

    def skill_provider() -> SkillDiscoveryResult:
        return skill_values[0]

    builder = ContextBuilder(
        reasoning_effort=ReasoningEffort.HIGH,
        interaction_mode=InteractionMode.NORMAL,
        plan=None,
        instruction_provider=instruction_provider,
        skill_provider=skill_provider,
    )
    original = builder.build((Message(Role.SYSTEM, "system"), Message(Role.USER, "question")))
    instruction_values[0] = instruction_values[1]
    skill_values[0] = skill_values[1]
    updated = builder.build(
        (
            Message(Role.SYSTEM, "system"),
            Message(Role.USER, "question"),
            Message(Role.ASSISTANT, "inspected backend"),
        )
    )
    old = _serialized_context(original)
    new = _serialized_context(updated)

    assert new[: len(old)] == old
    reasons_and_content = [(reason, content) for _, content, reason in new]
    assert any(
        reason == SyntheticReason.INSTRUCTION_SCOPE_REVISION.value and "backend rules" in content
        for reason, content in reasons_and_content
    )
    assert any(
        reason == SyntheticReason.SKILL_SCOPE_REVISION.value and "test" in content
        for reason, content in reasons_and_content
    )
    assert all(
        "review" not in content
        for reason, content in reasons_and_content
        if reason == SyntheticReason.SKILL_SCOPE_REVISION.value
    )


def test_journal_is_bounded_and_only_accepts_owned_synthetic_messages() -> None:
    journal = ProviderProjectionJournal()
    assert journal.append(
        0,
        Message(Role.USER, "runtime", synthetic_reason=SyntheticReason.RUNTIME_PLAN),
    )
    assert not journal.append(
        0,
        Message(Role.USER, "runtime", synthetic_reason=SyntheticReason.RUNTIME_PLAN),
    )
    with pytest.raises(TypeError):
        journal.append(0, Message(Role.USER, "canonical"))


@pytest.mark.parametrize(
    "reason",
    [
        SyntheticReason.RUNTIME_PLAN,
        SyntheticReason.RUNTIME_BUDGET,
        SyntheticReason.RUNTIME_CHECKPOINT,
        SyntheticReason.RUNTIME_SUPERVISION,
        SyntheticReason.RUNTIME_BACKGROUND_TASK,
    ],
)
def test_runtime_control_revisions_remain_append_only_without_entering_canonical_items(
    reason: SyntheticReason,
) -> None:
    builder = ContextBuilder(
        reasoning_effort=ReasoningEffort.HIGH,
        interaction_mode=InteractionMode.NORMAL,
        plan=None,
        instruction_provider=None,
        skill_provider=None,
    )
    durable = (Message(Role.SYSTEM, "system"), Message(Role.USER, "start"))
    first_notice = Message(Role.USER, "revision one", synthetic_reason=reason)
    first = builder.build((*durable, first_notice))
    durable_next = (*durable, Message(Role.ASSISTANT, "step completed"))
    second_notice = Message(Role.USER, "revision two", synthetic_reason=reason)
    second = builder.build((*durable_next, second_notice))
    third = builder.build((*durable_next, Message(Role.TOOL, "new result"), second_notice))

    first_wire = _serialized_context(first)
    second_wire = _serialized_context(second)
    third_wire = _serialized_context(third)
    assert second_wire[: len(first_wire)] == first_wire
    assert third_wire[: len(second_wire)] == second_wire
    assert [text for _, text, tagged in third_wire if tagged == reason.value] == [
        "revision one",
        "revision two",
    ]
    assert all(item.synthetic_reason is None for item in durable)


def test_tool_schema_order_is_stable_and_schema_change_is_a_boundary() -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"test-key-material-0123456789")
    context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="schema-fixture",
        prompt_trajectory_enabled=True,
    )
    messages = [_wire_message("system", "stable"), _wire_message("user", "question")]
    first = recorder.observe(
        {
            "messages": messages,
            "tools": [{"name": "read_file", "schema": {"a": 1, "b": 2}}],
        },
        context=context,
        provider="p",
        model="m",
    )
    reordered_schema = recorder.observe(
        {
            "messages": messages,
            "tools": [{"schema": {"b": 2, "a": 1}, "name": "read_file"}],
        },
        context=context,
        provider="p",
        model="m",
    )
    changed_schema = recorder.observe(
        {
            "messages": [*messages, _wire_message("assistant", "next")],
            "tools": [{"name": "read_file", "schema": {"a": 1, "b": 3}}],
        },
        context=context,
        provider="p",
        model="m",
    )

    assert first is not None
    assert reordered_schema is not None
    assert changed_schema is not None
    assert reordered_schema.append_only is True
    assert reordered_schema.tools_fingerprint == first.tools_fingerprint
    assert changed_schema.append_only is False
    assert changed_schema.boundary_reason is CacheBoundaryReason.TOOL_SCHEMA_CHANGE
    assert changed_schema.first_divergence_index is None

    class FixtureTool:
        def __init__(self, definition: ToolDefinition) -> None:
            self.definition = definition

        @property
        def side_effecting(self) -> bool:
            return False

        async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
            del arguments, context
            return ToolResult("fixture")

    registry = ToolRegistry(
        (
            FixtureTool(ToolDefinition("read_file", "Read", {"type": "object"})),
            FixtureTool(ToolDefinition("list_dir", "List", {"type": "object"})),
        )
    )
    ordered = [definition.to_dict() for definition in registry.definitions()]
    assert [definition["name"] for definition in ordered] == ["read_file", "list_dir"]
    schema_state = CacheContinuityState()
    assert schema_state.observe_tool_schema(ordered) is None
    assert schema_state.observe_tool_schema([dict(item) for item in ordered]) is None
    assert (
        schema_state.observe_tool_schema(tuple(reversed(ordered)))
        is CacheBoundaryReason.TOOL_SCHEMA_CHANGE
    )


def test_wire_append_only_requires_stable_prefix_and_request_options() -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"request-shape-key-0123456789")
    context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="request-shape-fixture",
        prompt_trajectory_enabled=True,
    )
    first = recorder.observe(
        {
            "instructions": "original system policy",
            "input": [_wire_message("user", "question")],
            "tools": [],
            "temperature": 0.2,
        },
        context=context,
        provider="openai-responses",
        model="fixture-model",
    )
    revised_prefix = recorder.observe(
        {
            "instructions": "changed system policy",
            "input": [
                _wire_message("user", "question"),
                _wire_message("assistant", "new suffix"),
            ],
            "tools": [],
            "temperature": 0.2,
        },
        context=context,
        provider="openai-responses",
        model="fixture-model",
    )
    revised_options = recorder.observe(
        {
            "instructions": "changed system policy",
            "input": [
                _wire_message("user", "question"),
                _wire_message("assistant", "new suffix"),
                _wire_message("assistant", "another suffix"),
            ],
            "tools": [],
            "temperature": 0.4,
        },
        context=context,
        provider="openai-responses",
        model="fixture-model",
    )

    assert first is not None
    assert revised_prefix is not None
    assert revised_prefix.common_prefix_messages == 1
    assert revised_prefix.first_divergence_index is None
    assert revised_prefix.append_only is False
    assert revised_prefix.boundary_reason is CacheBoundaryReason.INSTRUCTION_AUTHORITY_CHANGE
    assert revised_options is not None
    assert revised_options.common_prefix_messages == 2
    assert revised_options.first_divergence_index is None
    assert revised_options.append_only is False
    assert revised_options.boundary_reason is CacheBoundaryReason.CONFIG_RELOAD


@pytest.mark.parametrize(
    "reason",
    [
        CacheBoundaryReason.MICROCOMPACTION_BATCH,
        CacheBoundaryReason.FULL_COMPACTION,
        CacheBoundaryReason.FRESH_CONTEXT_ROLLOVER,
        CacheBoundaryReason.MODEL_SWITCH,
        CacheBoundaryReason.PROVIDER_SWITCH,
        CacheBoundaryReason.CONFIG_RELOAD,
        CacheBoundaryReason.PROJECT_SCOPE_CHANGE,
    ],
)
def test_typed_cache_boundary_starts_a_new_stable_projection_epoch(
    reason: CacheBoundaryReason,
) -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"test-key-material-0123456789")
    state = CacheContinuityState()
    state.bind("session", "project", 0)
    prior = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id=state.trajectory_id,
        cache_epoch=state.cache_epoch,
        prompt_trajectory_enabled=True,
    )
    before = recorder.observe(
        {
            "messages": [_wire_message("system", "old"), _wire_message("user", "history")],
            "tools": [],
        },
        context=prior,
        provider="p",
        model="m",
    )
    state.advance(reason)
    boundary_context = ModelContext(
        (),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id=state.trajectory_id,
        cache_epoch=state.cache_epoch,
        cache_boundary_reason=state.pending_boundary,
        prompt_trajectory_enabled=True,
    )
    after = recorder.observe(
        {
            "messages": [_wire_message("system", "new"), _wire_message("user", "summary")],
            "tools": [],
        },
        context=boundary_context,
        provider="p",
        model="m",
    )
    state.consume_boundary()
    appended = recorder.observe(
        {
            "messages": [
                _wire_message("system", "new"),
                _wire_message("user", "summary"),
                _wire_message("assistant", "new suffix"),
            ],
            "tools": [],
        },
        context=ModelContext(
            (),
            request_source=ModelRequestSource.MAIN_TURN,
            trajectory_id=state.trajectory_id,
            cache_epoch=state.cache_epoch,
            prompt_trajectory_enabled=True,
        ),
        provider="p",
        model="m",
    )

    assert before is not None
    assert after is not None
    assert appended is not None
    assert after.boundary_reason is reason
    assert after.append_only is None
    assert appended.boundary_reason is None
    assert appended.append_only is True


def test_new_binding_and_process_restart_begin_an_unrelated_trajectory() -> None:
    first_state = CacheContinuityState()
    first_state.bind("session-a", "project-a", 0)
    first_id = first_state.trajectory_id
    next_state = CacheContinuityState()
    assert next_state.bind("session-a", "project-a", 0) is CacheBoundaryReason.NEW_BINDING
    assert next_state.trajectory_id != first_id
    assert next_state.cache_epoch == 0
    assert next_state.pending_boundary is CacheBoundaryReason.NEW_BINDING


def test_trajectory_opt_out_returns_no_diagnostic_and_never_exposes_body() -> None:
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"test-key-material-0123456789")
    context = ModelContext((Message(Role.USER, "credential-looking-secret"),))
    event = recorder.observe(
        {"messages": [_wire_message("user", "credential-looking-secret")], "tools": []},
        context=context,
        provider="provider",
        model="model",
    )
    assert event is None


def test_project_memory_store_refresh_does_not_mutate_active_snapshot() -> None:
    memory_index = ["Project Memory revision one"]
    builder = ContextBuilder(
        reasoning_effort=ReasoningEffort.HIGH,
        interaction_mode=InteractionMode.NORMAL,
        plan=None,
        instruction_provider=None,
        skill_provider=None,
        project_memory_provider=lambda: memory_index[0],
    )
    first = builder.build((Message(Role.SYSTEM, "system"), Message(Role.USER, "question")))
    memory_index[0] = "Project Memory revision two"
    second = builder.build(
        (
            Message(Role.SYSTEM, "system"),
            Message(Role.USER, "question"),
            Message(Role.ASSISTANT, "step"),
        )
    )
    first_wire = _serialized_context(first)
    second_wire = _serialized_context(second)

    assert second_wire[: len(first_wire)] == first_wire
    assert sum("Project Memory revision one" in text for _, text, _ in second_wire) == 1
    assert all("Project Memory revision two" not in text for _, text, _ in second_wire)

    builder.invalidate_project_memory_snapshot()
    builder.reset_projection_epoch()
    next_binding = builder.build(
        (Message(Role.SYSTEM, "system"), Message(Role.USER, "new binding"))
    )
    next_wire = _serialized_context(next_binding)
    assert any("Project Memory revision two" in text for _, text, _ in next_wire)


def test_final_wire_adapters_share_the_same_safe_trajectory_contract() -> None:
    tool = ToolDefinition("read_file", "Read a file", {"type": "object"})
    context = ModelContext(
        (Message(Role.SYSTEM, "stable system"), Message(Role.USER, "private request")),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id="adapter-fixture",
        prompt_trajectory_enabled=True,
    )
    providers_and_bodies = (
        (
            "openai-responses",
            OpenAIResponsesProvider(
                model="responses-fixture",
                base_url="https://provider.invalid/v1",
                api_key="fixture-secret",
                provider_name="openai-responses",
            ),
            lambda provider: provider._request_body(context, (tool,)),
        ),
        (
            "anthropic",
            AnthropicProvider(
                model="claude-fixture",
                base_url="https://provider.invalid",
                api_key="fixture-secret",
                provider_name="anthropic",
            ),
            lambda provider: provider._request_body(context, (tool,)),
        ),
        (
            "gemini",
            GeminiProvider(
                model="gemini-fixture",
                base_url="https://provider.invalid",
                api_key="fixture-secret",
                provider_name="gemini",
            ),
            lambda provider: provider._request_body(context.messages, (tool,)),
        ),
        (
            "gemini-interactions",
            GeminiInteractionsProvider(
                model="gemini-fixture",
                base_url="https://provider.invalid/v1beta",
                api_key="fixture-secret",
                provider_name="gemini-interactions",
            ),
            lambda provider: provider._request_body(context, (tool,)),
        ),
    )

    for index, (name, provider, build_body) in enumerate(providers_and_bodies):
        recorder = ProviderRequestTrajectoryRecorder(
            fingerprint_key=b"test-key-material-0123456789"
        )
        provider_context = ModelContext(
            context.items,
            request_source=ModelRequestSource.MAIN_TURN,
            trajectory_id=f"adapter-fixture-{index}",
            prompt_trajectory_enabled=True,
        )
        event = recorder.observe(
            build_body(provider),
            context=provider_context,
            provider=provider.provider_name,
            model=provider.model_name,
        )
        assert event is not None
        assert event.provider == provider.provider_name == name
        assert event.tool_count == 1
        assert event.message_count > 0


@pytest.mark.asyncio
async def test_each_provider_emits_trajectory_from_its_final_request_body() -> None:
    tool = ToolDefinition("read_file", "Read a file", {"type": "object"})
    context = ModelContext(
        (Message(Role.SYSTEM, "stable system"), Message(Role.USER, "private request")),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id=uuid.uuid4().hex,
        prompt_trajectory_enabled=True,
    )
    providers = (
        OpenAIResponsesProvider(
            model="responses-fixture",
            base_url="https://provider.invalid/v1",
            api_key="fixture-secret",
            provider_name="openai-responses",
        ),
        AnthropicProvider(
            model="claude-fixture",
            base_url="https://provider.invalid",
            api_key="fixture-secret",
            provider_name="anthropic",
        ),
        GeminiProvider(
            model="gemini-fixture",
            base_url="https://provider.invalid",
            api_key="fixture-secret",
            provider_name="gemini",
        ),
        GeminiInteractionsProvider(
            model="gemini-fixture",
            base_url="https://provider.invalid/v1beta",
            api_key="fixture-secret",
            provider_name="gemini-interactions",
        ),
    )

    for provider in providers:
        event = await anext(provider.stream(context, (tool,)))
        assert isinstance(event, ModelRequestTrajectoryObserved)
        assert event.provider == provider.provider_name
        assert event.tool_count == 1
        event_text = repr(event.to_event_data())
        assert "private request" not in event_text
        assert "fixture-secret" not in event_text


def test_golden_five_step_deepseek_projection_is_append_only() -> None:
    trajectory_id = "golden-five-step"
    builder = ContextBuilder(
        reasoning_effort=ReasoningEffort.HIGH,
        interaction_mode=InteractionMode.NORMAL,
        plan=None,
        instruction_provider=None,
        skill_provider=None,
    )
    provider = OpenAICompatibleProvider(
        model="deepseek-v4-flash",
        base_url="https://provider.invalid/v1",
        api_key="fixture-secret",
        provider_name="deepseek",
        dialect="deepseek-v4",
    )
    recorder = ProviderRequestTrajectoryRecorder(fingerprint_key=b"golden-key-material-012345")
    tool = ToolDefinition("read_file", "Read a file", {"type": "object"})
    history: tuple[Message, ...] = (
        Message(Role.SYSTEM, "system fixture"),
        Message(Role.USER, "Inspect two files and report the result."),
    )
    worksets = (
        "Working set revision 1",
        "Working set revision 2",
        "Working set revision 2",
        "Working set revision 3",
        "Working set revision 4",
    )
    observed_bodies: list[dict[str, Any]] = []
    observed_events: list[ModelRequestTrajectoryObserved] = []

    for index, workset in enumerate(worksets):
        if index == 1:
            history = (
                *history,
                Message(
                    Role.ASSISTANT,
                    "",
                    tool_calls=(ToolCall("call-1", "read_file", {"path": "src/a.py"}),),
                    reasoning_content="provider-private reasoning",
                ),
                Message(Role.TOOL, "file A", tool_call_id="call-1"),
            )
        elif index == 2:
            history = (
                *history,
                Message(
                    Role.ASSISTANT,
                    "",
                    tool_calls=(ToolCall("call-2", "read_file", {"path": "src/b.py"}),),
                    reasoning_content="provider-private reasoning two",
                ),
                Message(Role.TOOL, "file B", tool_call_id="call-2"),
            )
        elif index == 3:
            history = (*history, Message(Role.ASSISTANT, "Compare the files."))
        elif index == 4:
            history = (*history, Message(Role.USER, "Finish with a concise report."))

        projected_items = builder.build(
            history,
            working_set_message=Message(
                Role.USER,
                workset,
                synthetic_reason=SyntheticReason.WORKING_SET,
            ),
        )
        context = ModelContext(
            tuple(item for item in projected_items if isinstance(item, Message)),
            request_source=ModelRequestSource.MAIN_TURN,
            trajectory_id=trajectory_id,
            prompt_trajectory_enabled=True,
        )
        body = provider._request_body(context, (tool,))
        observed_bodies.append(body)
        event = recorder.observe(
            body,
            context=context,
            provider=provider.provider_name,
            model=provider.model_name,
        )
        assert event is not None
        observed_events.append(event)

    assert observed_events[0].append_only is None
    assert all(event.append_only is True for event in observed_events[1:])
    for previous, current in pairwise(observed_bodies):
        previous_messages = previous["messages"]
        current_messages = current["messages"]
        assert current_messages[: len(previous_messages)] == previous_messages
    assert [event.sequence for event in observed_events] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_final_openai_chat_wire_request_attaches_only_safe_trajectory_metadata() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        model="deepseek-v4-flash",
        base_url="https://provider.invalid/v1",
        api_key="secret-key-that-must-not-appear",
        provider_name="deepseek",
        dialect="deepseek-v4",
        transport=httpx.MockTransport(handler),
    )
    trajectory_id = uuid.uuid4().hex
    context = ModelContext(
        (
            Message(Role.SYSTEM, "system fixture"),
            Message(Role.USER, "private user prompt"),
        ),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id=trajectory_id,
        prompt_trajectory_enabled=True,
    )
    tool = ToolDefinition("read_file", "Read a file", {"type": "object"})

    first_events = [event async for event in provider.stream(context, (tool,))]
    first_trajectory = next(
        event for event in first_events if isinstance(event, ModelRequestTrajectoryObserved)
    )
    next_context = ModelContext(
        (
            *context.items,
            Message(
                Role.ASSISTANT,
                "calling read_file",
                tool_calls=(ToolCall("call-1", "read_file", {"path": "src/a.py"}),),
                reasoning_content="hidden reasoning remains only on the provider wire",
            ),
            Message(Role.TOOL, "result", tool_call_id="call-1"),
            Message(Role.USER, "continue"),
        ),
        request_source=ModelRequestSource.MAIN_TURN,
        trajectory_id=trajectory_id,
        prompt_trajectory_enabled=True,
    )
    second_events = [event async for event in provider.stream(next_context, (tool,))]
    trajectory = next(
        event for event in second_events if isinstance(event, ModelRequestTrajectoryObserved)
    )

    assert trajectory.provider == "deepseek"
    assert trajectory.source is ModelRequestSource.MAIN_TURN
    assert trajectory.message_count == 5
    assert trajectory.tool_count == 1
    assert trajectory.common_prefix_messages == 2
    assert trajectory.append_only is True
    assert trajectory.first_divergence_index is None
    assert first_trajectory.sequence == 1
    assert bodies[1]["messages"][:2] == bodies[0]["messages"]
    assert bodies[1]["messages"][2]["reasoning_content"] == (
        "hidden reasoning remains only on the provider wire"
    )
    event_text = repr(trajectory.to_event_data())
    assert "private user prompt" not in event_text
    assert "system fixture" not in event_text
    assert "secret-key-that-must-not-appear" not in event_text


@pytest.mark.asyncio
async def test_model_step_emits_wire_trajectory_as_a_developer_event() -> None:
    digest = "0" * 64
    trajectory = ModelRequestTrajectoryObserved(
        sequence=1,
        source=ModelRequestSource.MAIN_TURN,
        provider="fixture",
        model="fixture-model",
        context_generation=0,
        cache_epoch=0,
        boundary_reason=None,
        message_fingerprints=(digest,),
        message_count=1,
        tool_count=0,
        tools_fingerprint=digest,
        stable_prefix_fingerprint=digest,
        request_fingerprint=digest,
        common_prefix_messages=None,
        first_divergence_index=None,
        previous_message_count=None,
        append_only=None,
    )
    emitted: list[AgentEvent] = []

    async def provider_stream():
        yield trajectory
        yield ModelCompleted("stop")

    async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
        event = AgentEvent.create(len(emitted) + 1, kind, data)
        emitted.append(event)
        return event

    await ModelStepProcessor(session_store=None).consume(
        provider_stream(),  # type: ignore[arg-type]
        emit=emit,
        step=1,
        step_started_at=time.monotonic(),
        session_id=None,
        can_adopt_provider_origin=False,
        on_imperfect=lambda: None,
    )

    assert [event.kind for event in emitted].count(AgentEventKind.MODEL_REQUEST_TRAJECTORY) == 1
    trajectory_event = next(
        event for event in emitted if event.kind is AgentEventKind.MODEL_REQUEST_TRAJECTORY
    )
    assert trajectory_event.data["source"] == ModelRequestSource.MAIN_TURN.value
