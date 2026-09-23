from __future__ import annotations

import json
import unittest

from neuro_code.application.memory.microcompaction import (
    MAX_MICROCOMPACTION_CONTEXT_BYTES,
    MAX_TRACKED_TOOL_RESULTS,
    MicrocompactionNoopReason,
    MicrocompactionRuntimeState,
    MicrocompactionTriggerReason,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.messages import (
    ContentPart,
    Message,
    Role,
    SessionItem,
    ToolCall,
)


def _group(index: int, content: str, *, parallel: bool = False) -> tuple[Message, ...]:
    calls = (
        (ToolCall(f"call-{index}-a", "read_file", {"path": f"file-{index}-a"}),)
        if not parallel
        else (
            ToolCall(f"call-{index}-a", "read_file", {"path": f"file-{index}-a"}),
            ToolCall(f"call-{index}-b", "read_file", {"path": f"file-{index}-b"}),
        )
    )
    assistant = Message(Role.ASSISTANT, tool_calls=calls)
    results = tuple(
        Message(Role.TOOL, content, name=call.name, tool_call_id=call.id) for call in calls
    )
    return (assistant, *results)


def _serialized(items: tuple[SessionItem, ...]) -> bytes:
    return json.dumps(
        [
            {
                **item.to_dict(),
                **(
                    {"synthetic_reason": item.synthetic_reason.value}
                    if isinstance(item, Message) and item.synthetic_reason is not None
                    else {}
                ),
            }
            for item in items
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _state_for(groups: tuple[tuple[Message, ...], ...]) -> MicrocompactionRuntimeState:
    state = MicrocompactionRuntimeState()
    state.begin_scope("session-1", 0)
    for group in groups:
        for result in group[1:]:
            assert result.tool_call_id is not None
            state.record_tool_result_status(result.tool_call_id, is_error=False)
    return state


class MicrocompactionTests(unittest.TestCase):
    def test_trigger_compacts_multiple_old_groups_and_preserves_adjacency_and_history(self) -> None:
        groups = tuple(
            _group(index, f"result-{index}: " + ("x" * 1_800), parallel=index == 0)
            for index in range(7)
        )
        current_user = Message(Role.USER, "current request")
        current_turn_group = _group(99, "current turn result: " + ("c" * 1_800))
        source_items: tuple[SessionItem, ...] = (
            Message(Role.SYSTEM, "stable system instructions"),
            *[item for group in groups for item in group],
            current_user,
            *current_turn_group,
        )
        source_bytes = _serialized(source_items)
        state = _state_for(groups)
        for result_item in current_turn_group[1:]:
            assert result_item.tool_call_id is not None
            state.record_tool_result_status(result_item.tool_call_id, is_error=False)

        result = state.evaluate(
            ModelContext(source_items),
            session_id="session-1",
            context_generation=0,
            current_user_message=current_user,
            compaction_id=None,
            trigger_reason=MicrocompactionTriggerReason.CONTEXT_PRESSURE,
        )

        assert result.telemetry is not None
        self.assertEqual(result.telemetry.groups_compacted, 4)
        self.assertEqual(result.telemetry.results_compacted, 5)
        self.assertGreaterEqual(result.telemetry.estimated_bytes_saved, 1_024)
        self.assertGreaterEqual(result.telemetry.estimated_tokens_saved, 256)
        self.assertEqual(
            result.telemetry.estimated_bytes_before,
            result.telemetry.estimated_bytes_after + result.telemetry.estimated_bytes_saved,
        )
        self.assertEqual(
            result.telemetry.estimated_tokens_before,
            result.telemetry.estimated_tokens_after + result.telemetry.estimated_tokens_saved,
        )
        self.assertTrue(result.has_compacted_results)

        projected = result.context.items
        # Every call remains immediately adjacent to its corresponding result.
        cursor = 1
        for index, group in enumerate(groups):
            self.assertEqual(projected[cursor], group[0])
            for result_offset, original in enumerate(group[1:], start=1):
                projected_result = projected[cursor + result_offset]
                assert isinstance(original, Message)
                assert isinstance(projected_result, Message)
                self.assertEqual(projected_result.tool_call_id, original.tool_call_id)
                if index < 4:
                    self.assertNotEqual(projected_result.content, original.content)
                    self.assertNotIn("result-", projected_result.content)
                else:
                    self.assertEqual(projected_result, original)
            cursor += len(group)
        self.assertEqual(projected[cursor], current_user)
        self.assertEqual(
            projected[cursor + 1 : cursor + 1 + len(current_turn_group)], current_turn_group
        )
        self.assertEqual(_serialized(source_items), source_bytes)

    def test_error_media_incomplete_and_unknown_groups_are_protected(self) -> None:
        successful = _group(0, "sensitive/repo/path " + ("x" * 4_000))
        failed = _group(1, "error output " + ("y" * 4_000))
        media_call = ToolCall("call-media", "read_file", {"path": "media"})
        media_group = (
            Message(Role.ASSISTANT, tool_calls=(media_call,)),
            Message(
                Role.TOOL,
                name=media_call.name,
                tool_call_id=media_call.id,
                content_parts=(ContentPart.from_image("https://example.invalid/image"),),
            ),
        )
        unknown = _group(3, "unknown status " + ("z" * 4_000))
        incomplete_call = ToolCall("call-incomplete", "read_file", {"path": "incomplete"})
        incomplete = (Message(Role.ASSISTANT, tool_calls=(incomplete_call,)),)
        recent = tuple(_group(index, "recent " + ("r" * 32)) for index in range(4, 7))
        current_user = Message(Role.USER, "current")
        all_groups = (successful, failed, media_group, unknown, incomplete, *recent)
        items: tuple[SessionItem, ...] = (
            Message(Role.SYSTEM, "system"),
            *[item for group in all_groups for item in group],
            current_user,
        )
        state = _state_for((successful, failed, *recent))
        state.record_tool_result_status("call-1-a", is_error=True)
        state.record_tool_result_status("call-media", is_error=False)
        # The incomplete group deliberately has no terminal result status.

        result = state.evaluate(
            ModelContext(items),
            session_id="session-1",
            context_generation=0,
            current_user_message=current_user,
            compaction_id=None,
            trigger_reason=MicrocompactionTriggerReason.CONTEXT_PRESSURE,
        )

        self.assertIsNotNone(result.telemetry)
        assert result.telemetry is not None
        self.assertEqual(result.telemetry.groups_compacted, 1)
        projected = result.context.items
        for group in (failed, media_group, unknown, incomplete, *recent):
            for item in group:
                original_index = items.index(item)
                self.assertEqual(projected[original_index], item)
        marker = next(
            item.content
            for item in projected
            if isinstance(item, Message)
            and item.role is Role.TOOL
            and item.tool_call_id == "call-0-a"
        )
        self.assertNotIn("sensitive/repo/path", marker)
        telemetry_text = json.dumps(result.telemetry.to_event_data())
        self.assertNotIn("sensitive/repo/path", telemetry_text)
        self.assertNotIn("error output", telemetry_text)

    def test_cached_projection_is_byte_stable_and_new_generation_clears_it(self) -> None:
        groups = tuple(
            _group(index, f"large result {index} " + ("x" * 1_800)) for index in range(6)
        )
        first_user = Message(Role.USER, "first turn")
        first_items: tuple[SessionItem, ...] = (
            Message(Role.SYSTEM, "system"),
            *[item for group in groups for item in group],
            first_user,
        )
        state = _state_for(groups)
        first = state.evaluate(
            ModelContext(first_items),
            session_id="session-1",
            context_generation=0,
            current_user_message=first_user,
            compaction_id="compact-a",
            trigger_reason=MicrocompactionTriggerReason.CONTEXT_PRESSURE,
        )
        self.assertTrue(first.has_compacted_results)
        for index in range(MAX_TRACKED_TOOL_RESULTS + 1):
            state.record_tool_result_status(f"later-call-{index}", is_error=False)

        replay = state.evaluate(
            ModelContext(first_items),
            session_id="session-1",
            context_generation=0,
            current_user_message=first_user,
            compaction_id="compact-a",
            trigger_reason=None,
        )
        self.assertEqual(_serialized(replay.context.items), _serialized(first.context.items))
        restarted = MicrocompactionRuntimeState()
        restarted.begin_scope("session-1", 0)
        resumed_without_runtime_evidence = restarted.project_cached(
            ModelContext(first_items),
            session_id="session-1",
            context_generation=0,
            compaction_id="compact-a",
        )
        self.assertFalse(resumed_without_runtime_evidence.has_compacted_results)
        self.assertEqual(
            _serialized(resumed_without_runtime_evidence.context.items), _serialized(first_items)
        )

        second_user = Message(Role.USER, "second turn")
        second_items = (*first_items, second_user)
        next_turn = state.project_cached(
            ModelContext(second_items),
            session_id="session-1",
            context_generation=0,
            compaction_id="compact-a",
        )
        self.assertEqual(
            _serialized(next_turn.context.items[: len(first.context.items)]),
            _serialized(first.context.items),
        )
        self.assertEqual(next_turn.context.items[-1], second_user)
        too_early = state.evaluate(
            ModelContext(second_items),
            session_id="session-1",
            context_generation=0,
            current_user_message=second_user,
            compaction_id="compact-a",
            trigger_reason=MicrocompactionTriggerReason.CONTEXT_PRESSURE,
        )
        self.assertIsNone(too_early.telemetry)
        self.assertEqual(_serialized(too_early.context.items), _serialized(next_turn.context.items))

        later_groups = tuple(
            _group(index + 20, f"later result {index} " + ("z" * 1_800)) for index in range(6)
        )
        for group in groups[3:]:
            for result_item in group[1:]:
                assert result_item.tool_call_id is not None
                state.record_tool_result_status(result_item.tool_call_id, is_error=False)
        for group in later_groups:
            for result_item in group[1:]:
                assert result_item.tool_call_id is not None
                state.record_tool_result_status(result_item.tool_call_id, is_error=False)
        later_items = (
            *first_items,
            *[item for group in later_groups for item in group],
            second_user,
        )
        next_batch = state.evaluate(
            ModelContext(later_items),
            session_id="session-1",
            context_generation=0,
            current_user_message=second_user,
            compaction_id="compact-a",
            trigger_reason=MicrocompactionTriggerReason.CONTEXT_PRESSURE,
        )
        assert next_batch.telemetry is not None
        self.assertEqual(next_batch.telemetry.groups_compacted, 6)
        self.assertGreaterEqual(next_batch.telemetry.estimated_bytes_saved, 1_024)

        new_generation = state.project_cached(
            ModelContext(second_items),
            session_id="session-1",
            context_generation=1,
            compaction_id=None,
        )
        self.assertFalse(new_generation.has_compacted_results)
        self.assertEqual(_serialized(new_generation.context.items), _serialized(second_items))

    def test_insufficient_savings_is_noop_and_does_not_pin_a_rewrite(self) -> None:
        groups = tuple(_group(index, "short") for index in range(5))
        current_user = Message(Role.USER, "current")
        items: tuple[SessionItem, ...] = (
            Message(Role.SYSTEM, "system"),
            *[item for group in groups for item in group],
            current_user,
        )
        state = _state_for(groups)

        result = state.evaluate(
            ModelContext(items),
            session_id="session-1",
            context_generation=0,
            current_user_message=current_user,
            compaction_id=None,
            trigger_reason=MicrocompactionTriggerReason.CONTEXT_PRESSURE,
        )

        self.assertFalse(result.has_compacted_results)
        self.assertEqual(_serialized(result.context.items), _serialized(items))
        assert result.telemetry is not None
        self.assertEqual(
            result.telemetry.noop_reason, MicrocompactionNoopReason.INSUFFICIENT_SAVINGS
        )
        self.assertEqual(result.telemetry.groups_compacted, 0)

    def test_oversized_projection_fails_closed_with_bounded_noop_telemetry(self) -> None:
        current_user = Message(Role.USER, "x" * (MAX_MICROCOMPACTION_CONTEXT_BYTES // 6 + 1))
        items: tuple[SessionItem, ...] = (Message(Role.SYSTEM, "system"), current_user)
        state = MicrocompactionRuntimeState()

        result = state.evaluate(
            ModelContext(items),
            session_id="session-1",
            context_generation=0,
            current_user_message=current_user,
            compaction_id=None,
            trigger_reason=MicrocompactionTriggerReason.CONTEXT_PRESSURE,
        )

        self.assertEqual(result.context.items, items)
        self.assertFalse(result.has_compacted_results)
        assert result.telemetry is not None
        self.assertEqual(result.telemetry.noop_reason, MicrocompactionNoopReason.SOURCE_LIMIT)
        self.assertTrue(result.telemetry.estimates_saturated)
        self.assertEqual(
            result.telemetry.estimated_bytes_before,
            result.telemetry.estimated_bytes_after,
        )
        self.assertEqual(result.telemetry.estimated_bytes_saved, 0)


if __name__ == "__main__":
    unittest.main()
