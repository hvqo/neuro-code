from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from time import monotonic

from neuro_code.application.runtime.model_step import ModelStepProcessor
from neuro_code.application.trace.collector import (
    MAX_TRACE_RECORDS,
    TRACE_LEDGER_PAGE_SIZE,
    TraceCollector,
    TraceKind,
    TraceStatus,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    AgentEventKind,
    ModelCompleted,
    ModelProviderSelected,
    ModelTextDelta,
    ModelUsage,
)
from neuro_code.domain.conversation.messages import Message, Role
from neuro_code.interfaces.tui.screens.trace import TraceScreen
from neuro_code.shared.ui_language import UiLanguage


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = value


class RuntimeTraceCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.collector = TraceCollector(clock=self.clock)
        self.sequence = 0
        self.trace_id = self.collector.begin_turn(turn_id="turn-1")

    def emit(self, kind: AgentEventKind, **data: object) -> None:
        self.sequence += 1
        self.collector.observe(AgentEvent.create(self.sequence, kind, data))

    def test_collects_model_tool_context_and_runtime_hierarchy_without_payloads(self) -> None:
        self.clock.set(100.1)
        self.emit(AgentEventKind.MODEL_STEP_STARTED, step=1)
        self.clock.set(100.15)
        self.emit(
            AgentEventKind.MODEL_REQUEST_SNAPSHOT,
            request_id="req-1",
            step=1,
            provider="deepseek",
            model="deepseek-flash",
            message_count=4,
            tool_count=2,
            prompt="PRIVATE_PROMPT_SENTINEL",
        )
        self.emit(
            AgentEventKind.MODEL_REQUEST_TRAJECTORY,
            request_id="req-1",
            source="main",
            context_generation=3,
            cache_epoch=7,
            boundary_reason="none",
            append_only=True,
            common_prefix_messages=4,
            message_count=4,
            tool_count=2,
            stable_prefix_fingerprint="PRIVATE_FINGERPRINT_SENTINEL",
        )
        self.clock.set(100.2)
        self.emit(
            AgentEventKind.PROVIDER_ATTEMPT_FAILED,
            request_id="req-1",
            provider="deepseek",
            model="deepseek-flash",
            error_type="TimeoutError",
            failure_kind="timeout",
            status_code=503,
            duration_seconds=0.05,
            message="PRIVATE_PROVIDER_ERROR_SENTINEL",
        )
        self.clock.set(100.65)
        self.emit(
            AgentEventKind.RUNTIME_TRACE_MODEL_REQUEST,
            request_id="req-1",
            step=1,
            provider="deepseek",
            model="deepseek-flash",
            source="main",
            status="succeeded",
            duration_ms=500,
            ttft_ms=150,
            stream_duration_ms=350,
            input_tokens=2_000,
            output_tokens=200,
            cache_read_tokens=1_500,
            cache_write_tokens=100,
            cache_miss_tokens=400,
            cache_reuse_ratio=0.75,
            estimated_context_tokens=2_100,
            capacity_tokens=32_000,
            context_generation=3,
            cache_epoch=7,
            cache_boundary_reason="none",
            append_only=True,
            common_prefix_messages=4,
            retry_count=1,
            failover_count=1,
            provider_attempts=(
                {
                    "attempt_index": 0,
                    "provider": "deepseek",
                    "model": "deepseek-flash",
                    "status": "failed",
                    "duration_ms": 50,
                    "error_type": "TimeoutError",
                    "failure_kind": "timeout",
                    "status_code": 503,
                },
                {
                    "attempt_index": 1,
                    "provider": "openai",
                    "model": "gpt-test",
                    "status": "succeeded",
                    "duration_ms": 450,
                    "failover": True,
                },
            ),
            output_text="PRIVATE_MODEL_OUTPUT_SENTINEL",
        )
        self.clock.set(100.7)
        self.emit(AgentEventKind.TEXT_DELTA, text="Visible response")

        self.clock.set(100.8)
        self.emit(
            AgentEventKind.TOOL_REQUESTED,
            id="call-a",
            name="read_file",
            arguments={"path": "/private/repository/file.py", "token": "PRIVATE_TOOL_ARG_SENTINEL"},
        )
        self.clock.set(100.81)
        self.emit(AgentEventKind.TOOL_REQUESTED, id="call-b", name="grep")
        self.clock.set(100.91)
        self.emit(AgentEventKind.TOOL_STARTED, id="call-a", name="read_file")
        self.clock.set(100.92)
        self.emit(AgentEventKind.TOOL_STARTED, id="call-b", name="grep")
        self.clock.set(101.12)
        self.emit(
            AgentEventKind.TOOL_COMPLETED,
            id="call-a",
            name="read_file",
            content="PRIVATE_TOOL_RESULT_SENTINEL",
            model_context_projection={
                "truncated": True,
                "original_bytes": 4_000,
                "projected_bytes": 500,
                "artifact_available": True,
                "strategy": "bounded",
                "secret": "PRIVATE_PROJECTION_SENTINEL",
            },
        )
        self.clock.set(101.14)
        self.emit(
            AgentEventKind.TOOL_FAILED,
            id="call-b",
            name="grep",
            content="PRIVATE_TOOL_ERROR_SENTINEL",
            cancelled=False,
        )
        self.clock.set(101.2)
        self.emit(AgentEventKind.MODEL_STEP_STARTED, step=2)

        self.clock.set(101.25)
        self.emit(
            AgentEventKind.RUNTIME_TRACE_CONTEXT_BUILD,
            duration_ms=12,
            item_count=9,
            estimated_tokens=2_345,
            context_generation=3,
            cache_epoch=7,
            request_source="main",
            prompt="PRIVATE_CONTEXT_SENTINEL",
        )
        self.emit(
            AgentEventKind.CONTEXT_PREFLIGHT,
            status="safe",
            estimated_input_tokens=2_100,
            microcompaction={
                "trigger_reason": "pressure",
                "groups_compacted": 3,
                "results_compacted": 4,
                "estimated_bytes_before": 9_000,
                "estimated_bytes_after": 3_000,
                "estimated_bytes_saved": 6_000,
                "estimated_tokens_before": 1_900,
                "estimated_tokens_after": 700,
                "estimated_tokens_saved": 1_200,
                "noop_reason": None,
                "estimates_saturated": False,
                "body": "PRIVATE_MICROCOMPACTION_SENTINEL",
            },
        )
        self.clock.set(101.3)
        self.emit(AgentEventKind.CONTEXT_COMPACTION_STARTED, safe_point="turn_end")
        self.clock.set(101.31)
        self.emit(
            AgentEventKind.CONTEXT_COMPACTION_COMPLETED,
            source_item_count=8,
            candidate_item_count=4,
            summary_tokens=80,
        )
        self.emit(
            AgentEventKind.RUNTIME_TRACE_CONTEXT_ROLLOVER,
            context_generation=4,
            reason="explicit",
        )
        self.emit(
            AgentEventKind.RUNTIME_TRACE_REPLAN,
            state="replanned",
            reason_code="no_progress",
            replan_count=2,
            plan="PRIVATE_PLAN_SENTINEL",
        )
        self.emit(
            AgentEventKind.RUNTIME_TRACE_VERIFICATION,
            status="failed",
            duration_ms=21,
            error_type="VerificationError",
        )
        self.emit(
            AgentEventKind.RUNTIME_TRACE_FINALIZER,
            status="succeeded",
            duration_ms=42,
            attempts=2,
            input_tokens=100,
            output_tokens=30,
        )
        self.clock.set(101.5)
        self.emit(
            AgentEventKind.TURN_COMPLETED,
            turn_id="turn-1",
            duration_seconds=1.5,
        )

        snapshot = self.collector.snapshot(self.trace_id)
        assert snapshot is not None
        by_kind = {
            kind: [record for record in snapshot.records if record.kind is kind]
            for kind in TraceKind
        }
        model = by_kind[TraceKind.MODEL][0]
        batch = by_kind[TraceKind.TOOL_BATCH][0]
        call_a = next(
            record for record in by_kind[TraceKind.TOOL] if record.tool_call_id == "call-a"
        )

        self.assertEqual(snapshot.status, TraceStatus.SUCCEEDED)
        self.assertEqual(model.ttft_ms, 150)
        self.assertEqual(model.metadata["stream_duration_ms"], 350)
        self.assertEqual(model.metadata["append_only"], True)
        self.assertEqual(len(by_kind[TraceKind.PROVIDER_ATTEMPT]), 2)
        failed_attempt = next(
            record
            for record in by_kind[TraceKind.PROVIDER_ATTEMPT]
            if record.status is TraceStatus.FAILED
        )
        successful_attempt = next(
            record
            for record in by_kind[TraceKind.PROVIDER_ATTEMPT]
            if record.status is TraceStatus.SUCCEEDED
        )
        model_end = model.start_offset_ms + (model.duration_ms or 0)
        self.assertEqual(failed_attempt.start_offset_ms, model.start_offset_ms)
        self.assertAlmostEqual(
            failed_attempt.start_offset_ms + (failed_attempt.duration_ms or 0),
            successful_attempt.start_offset_ms,
        )
        self.assertLessEqual(
            successful_attempt.start_offset_ms + (successful_attempt.duration_ms or 0),
            model_end,
        )
        model_started_at = datetime.fromisoformat(model.started_at)
        failed_started_at = datetime.fromisoformat(failed_attempt.started_at)
        successful_started_at = datetime.fromisoformat(successful_attempt.started_at)
        model_ended_at = model_started_at + timedelta(milliseconds=model.duration_ms or 0)
        self.assertLessEqual(model_started_at, failed_started_at)
        self.assertLessEqual(failed_started_at + timedelta(milliseconds=50), successful_started_at)
        self.assertLessEqual(
            successful_started_at + timedelta(milliseconds=450),
            model_ended_at,
        )
        self.assertEqual(snapshot.summary.retries, 1)
        self.assertEqual(snapshot.summary.failovers, 1)
        self.assertAlmostEqual(snapshot.summary.user_visible_ttft_ms or 0, 700)
        self.assertEqual(snapshot.summary.tool_calls, 2)
        self.assertEqual(snapshot.summary.tool_batches, 1)
        self.assertTrue(batch.metadata["parallel"])
        self.assertEqual(call_a.metadata["permission_wait_ms"], 110.0)
        self.assertAlmostEqual(call_a.metadata["execution_ms"], 210.0)
        self.assertAlmostEqual(snapshot.summary.permission_wait_ms, 220.0)
        self.assertAlmostEqual(snapshot.summary.tool_time_ms, 430.0)
        self.assertAlmostEqual(snapshot.summary.context_time_ms, 22.0)
        self.assertEqual(call_a.metadata["projected_bytes"], 500)
        self.assertTrue(call_a.metadata["artifact_available"])
        self.assertEqual(snapshot.summary.compactions, 1)
        self.assertEqual(snapshot.summary.replans, 1)
        self.assertEqual(snapshot.summary.finalizer_calls, 1)

        exported = self.collector.export_json(trace_id=self.trace_id)
        for sensitive in (
            "PRIVATE_PROMPT_SENTINEL",
            "PRIVATE_FINGERPRINT_SENTINEL",
            "PRIVATE_PROVIDER_ERROR_SENTINEL",
            "PRIVATE_MODEL_OUTPUT_SENTINEL",
            "PRIVATE_TOOL_ARG_SENTINEL",
            "PRIVATE_TOOL_RESULT_SENTINEL",
            "PRIVATE_TOOL_ERROR_SENTINEL",
            "PRIVATE_PROJECTION_SENTINEL",
            "PRIVATE_MICROCOMPACTION_SENTINEL",
            "PRIVATE_CONTEXT_SENTINEL",
            "PRIVATE_PLAN_SENTINEL",
        ):
            self.assertNotIn(sensitive, exported)
        export_data = json.loads(exported)
        self.assertEqual(export_data["traces"][0]["trace_id"], self.trace_id)

    def test_terminal_turn_identity_is_synchronized_to_every_record(self) -> None:
        collector = TraceCollector(clock=self.clock)
        trace_id = collector.begin_turn()
        self.clock.set(100.4)
        collector.observe(
            AgentEvent.create(
                1,
                AgentEventKind.TURN_COMPLETED,
                {"turn_id": "durable-turn-id", "duration_seconds": 0.4},
            )
        )

        snapshot = collector.snapshot(trace_id)
        assert snapshot is not None
        self.assertEqual(snapshot.turn_id, "durable-turn-id")
        self.assertTrue(snapshot.records)
        self.assertEqual({record.turn_id for record in snapshot.records}, {snapshot.turn_id})

    def test_weighted_cache_reuse_uses_total_cached_over_total_input_tokens(self) -> None:
        collector = TraceCollector(clock=self.clock)
        trace_id = collector.begin_turn(turn_id="weighted-cache")

        collector.observe(
            AgentEvent.create(
                1,
                AgentEventKind.MODEL_REQUEST_SNAPSHOT,
                {"request_id": "request-1", "provider": "p", "model": "m"},
            )
        )
        self.clock.set(100.1)
        collector.observe(
            AgentEvent.create(
                2,
                AgentEventKind.RUNTIME_TRACE_MODEL_REQUEST,
                {
                    "request_id": "request-1",
                    "status": "succeeded",
                    "duration_ms": 100,
                    "input_tokens": 1_000,
                    "cache_read_tokens": 100,
                },
            )
        )
        self.clock.set(100.2)
        collector.observe(
            AgentEvent.create(
                3,
                AgentEventKind.MODEL_REQUEST_SNAPSHOT,
                {"request_id": "request-2", "provider": "p", "model": "m"},
            )
        )
        self.clock.set(100.3)
        collector.observe(
            AgentEvent.create(
                4,
                AgentEventKind.RUNTIME_TRACE_MODEL_REQUEST,
                {
                    "request_id": "request-2",
                    "status": "succeeded",
                    "duration_ms": 100,
                    "input_tokens": 100,
                    "cache_read_tokens": 90,
                },
            )
        )

        snapshot = collector.snapshot(trace_id)
        assert snapshot is not None
        self.assertAlmostEqual(snapshot.summary.weighted_cache_reuse or 0, 190 / 1_100)
        exported = json.loads(collector.export_json(trace_id=trace_id))
        self.assertAlmostEqual(
            exported["traces"][0]["summary"]["weighted_cache_reuse"],
            190 / 1_100,
            places=3,
        )

    def test_context_rollover_and_subagent_parent_trace_are_explicit(self) -> None:
        parent_id = self.trace_id
        self.clock.set(100.4)
        self.emit(
            AgentEventKind.TURN_COMPLETED,
            turn_id="turn-1",
            duration_seconds=0.4,
        )
        self.clock.set(101.0)
        child_id = self.collector.begin_turn(
            turn_id="child-turn",
            source="subagent",
            parent_trace_id=parent_id,
        )
        self.clock.set(101.5)
        self.emit(
            AgentEventKind.RUNTIME_TRACE_SUBAGENT,
            duration_ms=250,
            status="completed",
            task_id="task-safe-id",
            child_session_id="child-session-safe-id",
            steps=4,
        )
        self.clock.set(101.6)
        self.emit(AgentEventKind.TURN_COMPLETED, turn_id="child-turn")
        child = self.collector.snapshot(child_id)
        assert child is not None
        subagent = next(record for record in child.records if record.kind is TraceKind.SUBAGENT)
        self.assertEqual(child.parent_trace_id, parent_id)
        self.assertEqual(subagent.duration_ms, 250)
        self.assertEqual(subagent.start_offset_ms, 250)
        self.assertEqual(subagent.status, TraceStatus.SUCCEEDED)
        self.assertEqual(child.summary.model_steps, 0)

    def test_cancellation_failure_and_bounded_retention(self) -> None:
        self.collector.end_turn("cancelled")
        cancelled = self.collector.snapshot(self.trace_id)
        assert cancelled is not None
        self.assertEqual(cancelled.status, TraceStatus.CANCELLED)

        bounded = TraceCollector(max_turns=2, max_records=MAX_TRACE_RECORDS)
        bounded.begin_turn()
        for index in range(2_100):
            bounded.observe(
                AgentEvent.create(
                    index + 1,
                    AgentEventKind.RUNTIME_TRACE_CONTEXT_BUILD,
                    {"duration_ms": 1, "item_count": index, "prompt": "must not be retained"},
                )
            )
        snapshot = bounded.snapshot()
        assert snapshot is not None
        self.assertEqual(len(snapshot.records), 2_048)
        self.assertGreater(snapshot.dropped_records, 0)
        self.assertLessEqual(len(bounded.export_json().encode("utf-8")), 4 * 1024 * 1024)


class RuntimeTraceModelStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_leave_model_result_and_context_unchanged(self) -> None:
        context = ModelContext.from_messages((Message(Role.USER, "cache-stable prompt"),))

        async def run(
            *, diagnostic: bool
        ) -> tuple[object, list[tuple[AgentEventKind, dict[str, object]]], list[dict[str, object]]]:
            emitted: list[tuple[AgentEventKind, dict[str, object]]] = []
            diagnostics: list[dict[str, object]] = []

            async def stream() -> AsyncIterator[object]:
                yield ModelTextDelta("answer")
                yield ModelCompleted(
                    "stop",
                    usage=ModelUsage(
                        input_tokens=20,
                        output_tokens=2,
                        cache_read_tokens=10,
                        cache_write_tokens=4,
                        cache_miss_tokens=6,
                    ),
                )

            async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
                emitted.append((kind, dict(data)))
                return AgentEvent.create(len(emitted), kind, data)

            async def trace_sink(kind: AgentEventKind, data: dict[str, object]) -> None:
                diagnostics.append(dict(data))

            result = await ModelStepProcessor(session_store=None).consume(
                stream(),
                emit=emit,
                step=1,
                step_started_at=monotonic(),
                session_id=None,
                can_adopt_provider_origin=False,
                on_imperfect=lambda: None,
                request_id="request-1",
                request_started_at=monotonic(),
                provider_name="provider",
                model_name="model",
                context=context,
                diagnostic_sink=trace_sink if diagnostic else None,
            )
            return result, emitted, diagnostics

        traced, traced_events, diagnostics = await run(diagnostic=True)
        untraced, untraced_events, _ = await run(diagnostic=False)
        self.assertEqual(traced, untraced)
        self.assertEqual([kind for kind, _ in traced_events], [kind for kind, _ in untraced_events])
        self.assertEqual(context.items, (Message(Role.USER, "cache-stable prompt"),))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["input_tokens"], 20)
        self.assertEqual(diagnostics[0]["cache_read_tokens"], 10)

    async def test_provider_exception_records_current_attempt_without_error_body(self) -> None:
        diagnostics: list[dict[str, object]] = []

        async def stream() -> AsyncIterator[object]:
            yield ModelProviderSelected("fallback", "fallback-model", None, True)
            yield ModelTextDelta("partial")
            raise RuntimeError("PRIVATE_PROVIDER_ERROR_SENTINEL")

        async def emit(kind: AgentEventKind, data: dict[str, object]) -> AgentEvent:
            return AgentEvent.create(1, kind, data)

        async def trace_sink(_kind: AgentEventKind, data: dict[str, object]) -> None:
            diagnostics.append(dict(data))

        with self.assertRaisesRegex(RuntimeError, "PRIVATE_PROVIDER_ERROR_SENTINEL"):
            await ModelStepProcessor(session_store=None).consume(
                stream(),
                emit=emit,
                step=1,
                step_started_at=monotonic(),
                session_id=None,
                can_adopt_provider_origin=False,
                on_imperfect=lambda: None,
                request_id="request-failed",
                request_started_at=monotonic(),
                provider_name="primary",
                model_name="primary-model",
                diagnostic_sink=trace_sink,
            )

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["provider"], "fallback")
        self.assertEqual(diagnostics[0]["model"], "fallback-model")
        self.assertEqual(diagnostics[0]["status"], "failed")
        self.assertEqual(diagnostics[0]["error_type"], "RuntimeError")
        self.assertNotIn("PRIVATE_PROVIDER_ERROR_SENTINEL", json.dumps(diagnostics))
        attempts = diagnostics[0]["provider_attempts"]
        self.assertEqual(attempts[0]["failover"], True)  # type: ignore[index]


class RuntimeTraceScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_virtualized_ledger_filters_and_follows_large_trace(self) -> None:
        from textual.app import App, ComposeResult
        from textual.widgets import Label, Static

        from neuro_code.interfaces.tui.theme import TEXTUAL_THEME

        collector = TraceCollector()
        collector.begin_turn(turn_id="large-turn")
        for index in range(1_100):
            collector.observe(
                AgentEvent.create(
                    index + 1,
                    AgentEventKind.RUNTIME_TRACE_CONTEXT_BUILD,
                    {"duration_ms": 1, "item_count": index, "estimated_tokens": index},
                )
            )

        class Host(App[None]):
            def compose(self) -> ComposeResult:
                yield Static()

            def on_mount(self) -> None:
                self.push_screen(TraceScreen(collector, language=UiLanguage.ENGLISH))

        app = Host()
        app.register_theme(TEXTUAL_THEME)
        app.theme = TEXTUAL_THEME.name
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, TraceScreen)
            self.assertEqual(len(screen._filtered), 1_101)
            screen._refresh_view()
            summary_text = str(screen.query_one("#trace-summary", Label).renderable)
            for category in (
                "Weighted cache reuse",
                "Permission wait",
                "Tool execution",
                "Context",
                "Runtime/Other",
            ):
                self.assertIn(category, summary_text)
            self.assertLessEqual(
                len(str(screen.query_one("#trace-ledger").renderable).splitlines()),
                TRACE_LEDGER_PAGE_SIZE + 2,
            )
            search = screen.query_one("#trace-filter")
            search.value = "1099"
            await pilot.pause()
            self.assertEqual(len(screen._filtered), 1)
            search.value = ""
            await pilot.pause()
            await pilot.press("pagedown")
            self.assertEqual(screen.selected_record, screen._filtered[TRACE_LEDGER_PAGE_SIZE])
            await pilot.press("ctrl+f")
            self.assertFalse(screen._follow)
            await pilot.press("ctrl+f")
            self.assertTrue(screen._follow)
            collector.observe(
                AgentEvent.create(
                    1_101,
                    AgentEventKind.RUNTIME_TRACE_CONTEXT_BUILD,
                    {"duration_ms": 1, "item_count": 1_101, "estimated_tokens": 1_101},
                )
            )
            screen.refresh_trace()
            self.assertEqual(screen.selected_record, screen._filtered[-1])
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
