from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.benchmark.execution_efficiency_fixture import (
    REPOSITORY_REVIEW_FILES,
    REPOSITORY_REVIEW_FINDING,
    run_repository_review_benchmark,
    write_repository_review_fixture,
)

from neuro_code.application.trace.collector import TraceKind
from neuro_code.domain.conversation.messages import Message, Role, SyntheticReason


class ExecutionEfficiencyBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_review_batches_remaining_evidence_without_losing_correctness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_repository_review_fixture(root)
            baseline = await run_repository_review_benchmark(root=root, adaptive=False)
            optimized = await run_repository_review_benchmark(root=root, adaptive=True)

        self.assertEqual(baseline.response, REPOSITORY_REVIEW_FINDING)
        self.assertEqual(optimized.response, REPOSITORY_REVIEW_FINDING)
        self.assertIsNone(baseline.execution_status)
        self.assertIsNone(optimized.execution_status)
        self.assertEqual(baseline.tool_paths_read, frozenset(REPOSITORY_REVIEW_FILES))
        self.assertEqual(optimized.tool_paths_read, frozenset(REPOSITORY_REVIEW_FILES))

        before = baseline.snapshot.summary
        after = optimized.snapshot.summary
        self.assertLess(after.model_steps, before.model_steps)
        self.assertLess(after.main_model_requests, before.main_model_requests)
        self.assertEqual((before.tool_calls, after.tool_calls), (5, 5))
        self.assertLess(after.tool_batches, before.tool_batches)
        self.assertAlmostEqual(before.weighted_cache_reuse or 0.0, 0.8)
        self.assertAlmostEqual(after.weighted_cache_reuse or 0.0, 0.8)
        self.assertGreater(before.provider_time_main_ms - after.provider_time_main_ms, 20)
        self.assertEqual(before.analysis_backtrack_count, 0)
        self.assertEqual(after.analysis_backtrack_count, 0)
        self.assertEqual(before.analyze_tool_call_count, 0)
        self.assertEqual(after.analyze_tool_call_count, 0)
        self.assertEqual(
            sum(item.main_model_requests for item in before.phase_metrics),
            before.main_model_requests,
        )
        self.assertEqual(
            sum(item.main_model_requests for item in after.phase_metrics),
            after.main_model_requests,
        )
        phases = {item.phase: item for item in after.phase_metrics}
        self.assertEqual(phases["analyze"].main_model_requests, 0)
        before_phases = {item.phase: item for item in before.phase_metrics}
        self.assertEqual(before_phases["analyze"].main_model_requests, 0)
        self.assertEqual(before_phases["explore"].main_model_requests, before.main_model_requests)
        self.assertEqual(phases["explore"].main_model_requests, after.main_model_requests)
        self.assertEqual(before_phases["analyze"].main_output_tokens, 0)
        self.assertEqual(phases["analyze"].main_output_tokens, 0)
        self.assertEqual(after.finalizer_provider_requests, 0)

        baseline_model_records = tuple(
            record for record in baseline.snapshot.records if record.kind is TraceKind.MODEL
        )
        optimized_model_records = tuple(
            record for record in optimized.snapshot.records if record.kind is TraceKind.MODEL
        )
        self.assertEqual(
            sum(record.output_tokens or 0 for record in baseline_model_records),
            6_180,
        )
        self.assertEqual(sum(record.output_tokens or 0 for record in optimized_model_records), 44)
        self.assertAlmostEqual(
            len(REPOSITORY_REVIEW_FILES) / before.tool_batches,
            1.0,
        )
        self.assertAlmostEqual(after.tool_calls / after.tool_batches, 5 / 3)
        self.assertTrue(
            any(record.kind is TraceKind.EFFICIENCY for record in optimized.snapshot.records)
        )
        self.assertTrue(
            any(
                record.kind is TraceKind.EFFICIENCY
                and record.metadata.get("reason_code") == "low_information_exploration"
                and record.metadata.get("phase") == "explore"
                for record in optimized.snapshot.records
            )
        )
        self.assertTrue(
            all(
                record.metadata.get("execution_phase") != "finalize"
                for record in baseline_model_records
            )
        )
        self.assertFalse(
            any(
                record.metadata.get("execution_phase") == "analyze"
                for record in optimized_model_records
            )
        )

        for result in (baseline, optimized):
            for previous, current in zip(
                result.request_contexts,
                result.request_contexts[1:],
                strict=False,
            ):
                self.assertEqual(
                    previous.messages,
                    current.messages[: len(previous.messages)],
                )
                previous_system = next(
                    item.model_content()
                    for item in previous.messages
                    if isinstance(item, Message) and item.role is Role.SYSTEM
                )
                current_system = next(
                    item.model_content()
                    for item in current.messages
                    if isinstance(item, Message) and item.role is Role.SYSTEM
                )
                self.assertEqual(previous_system, current_system)

        self.assertFalse(
            any(
                isinstance(item, Message)
                and item.synthetic_reason is SyntheticReason.RUNTIME_EFFICIENCY
                for item in optimized.durable_items
            )
        )

    async def test_dependent_reads_remain_sequential_after_efficiency_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_repository_review_fixture(root)
            dependent = await run_repository_review_benchmark(
                root=root,
                adaptive=True,
                dependent=True,
            )

        self.assertEqual(dependent.response, REPOSITORY_REVIEW_FINDING)
        self.assertEqual(dependent.tool_paths_read, frozenset(REPOSITORY_REVIEW_FILES))
        self.assertEqual(dependent.provider_request_count, 6)
        self.assertEqual(dependent.snapshot.summary.tool_batches, 5)
        self.assertEqual(dependent.snapshot.summary.tool_calls, 5)
        self.assertTrue(
            any(
                record.kind is TraceKind.EFFICIENCY
                and record.metadata.get("reason_code") == "low_information_exploration"
                for record in dependent.snapshot.records
            )
        )


if __name__ == "__main__":
    unittest.main()
