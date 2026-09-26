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
        self.assertEqual(baseline.tool_paths_read, frozenset(REPOSITORY_REVIEW_FILES))
        self.assertEqual(optimized.tool_paths_read, frozenset(REPOSITORY_REVIEW_FILES))

        before = baseline.snapshot.summary
        after = optimized.snapshot.summary
        self.assertEqual((before.model_steps, before.model_requests), (6, 6))
        self.assertEqual((after.model_steps, after.model_requests), (4, 4))
        self.assertEqual((before.tool_calls, after.tool_calls), (5, 5))
        self.assertEqual((before.tool_batches, after.tool_batches), (5, 3))
        self.assertAlmostEqual(before.weighted_cache_reuse or 0.0, 0.8)
        self.assertAlmostEqual(after.weighted_cache_reuse or 0.0, 0.8)
        self.assertLess(after.provider_time_ms, before.provider_time_ms)

        baseline_model_records = tuple(
            record for record in baseline.snapshot.records if record.kind is TraceKind.MODEL
        )
        optimized_model_records = tuple(
            record for record in optimized.snapshot.records if record.kind is TraceKind.MODEL
        )
        self.assertEqual(sum(record.output_tokens or 0 for record in baseline_model_records), 60)
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
                for record in optimized.snapshot.records
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
