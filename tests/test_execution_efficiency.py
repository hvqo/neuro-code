from __future__ import annotations

import hashlib
import unittest

from neuro_code.application.runtime.execution_efficiency import (
    EfficiencyReason,
    ExecutionEfficiencyController,
    ExecutionPhase,
    ToolEvidenceFact,
)
from neuro_code.domain.execution import ProgressKind


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fact(
    name: str,
    *,
    action: str | None = None,
    observation: str | None = None,
    progress: ProgressKind = ProgressKind.EVIDENCE,
    error: bool = False,
    parallel: bool = True,
    composite: bool = False,
    workspace_changed: bool = False,
    verification_succeeded: bool = False,
) -> ToolEvidenceFact:
    return ToolEvidenceFact(
        tool_name=name,
        action_digest=_digest(action or name),
        observation_digest=_digest(observation or f"{name}-result"),
        is_error=error,
        progress_kind=progress,
        parallel_safe=parallel,
        composite_batch=composite,
        workspace_changed=workspace_changed,
        verification_succeeded=verification_succeeded,
    )


class ExecutionEfficiencyControllerTests(unittest.TestCase):
    def test_initial_phase_is_reported_once_without_guidance(self) -> None:
        controller = ExecutionEfficiencyController()
        update = controller.start()

        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.EXPLORE)
        self.assertIsNone(update.previous_phase)
        self.assertEqual(update.reason, EfficiencyReason.TURN_STARTED)
        self.assertIsNone(update.guidance)
        self.assertIsNone(controller.start())

    def test_two_new_singleton_evidence_batches_emit_one_bounded_checkpoint(self) -> None:
        controller = ExecutionEfficiencyController()

        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="a"),)))
        update = controller.observe_tool_batch((_fact("grep_many", action="b"),))

        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.ANALYZE)
        self.assertEqual(update.previous_phase, ExecutionPhase.EXPLORE)
        self.assertEqual(update.reason, EfficiencyReason.LOW_INFORMATION_EXPLORATION)
        self.assertEqual(update.evidence_count, 2)
        self.assertIn("batch independent", update.guidance or "")
        self.assertIn("Continue targeted exploration", update.guidance or "")
        follow_up = controller.observe_tool_batch((_fact("read_file", action="c"),))
        assert follow_up is not None
        self.assertEqual(follow_up.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(follow_up.reason, EfficiencyReason.TARGETED_EVIDENCE_REQUESTED)
        self.assertIsNone(follow_up.guidance)
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(controller.evidence_count, 3)
        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="d"),)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_completed_structured_plan_enters_analysis_without_counting_more_steps(self) -> None:
        controller = ExecutionEfficiencyController()
        update = controller.observe_tool_batch(
            (_fact("update_plan", progress=ProgressKind.PLAN),),
            plan_complete=True,
        )

        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.ANALYZE)
        self.assertEqual(update.reason, EfficiencyReason.STRUCTURED_PLAN_COMPLETED)
        self.assertEqual(update.evidence_count, 0)
        follow_up = controller.observe_tool_batch((_fact("read_file", action="new-evidence"),))
        assert follow_up is not None
        self.assertEqual(follow_up.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(follow_up.reason, EfficiencyReason.TARGETED_EVIDENCE_REQUESTED)
        self.assertIsNone(follow_up.guidance)
        self.assertEqual(controller.evidence_count, 1)
        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="next"),)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_duplicate_or_unclassified_evidence_does_not_fake_low_information_progress(
        self,
    ) -> None:
        controller = ExecutionEfficiencyController()
        repeated = _fact("read_file", action="same", observation="same")

        self.assertIsNone(controller.observe_tool_batch((repeated,)))
        self.assertIsNone(controller.observe_tool_batch((repeated,)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_empty_batch_breaks_singleton_streak(self) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch((_fact("read_file", action="one"),))

        self.assertIsNone(controller.observe_tool_batch(()))
        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="two"),)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_composite_reads_and_multi_call_batches_are_not_singleton_rounds(self) -> None:
        controller = ExecutionEfficiencyController()

        self.assertIsNone(
            controller.observe_tool_batch(
                (_fact("read_files", action="multi-files", composite=True),)
            )
        )
        self.assertIsNone(
            controller.observe_tool_batch(
                (
                    _fact("read_file", action="one"),
                    _fact("grep_many", action="two"),
                )
            )
        )
        self.assertIsNone(controller.observe_tool_batch((_fact("list_tree", action="three"),)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_errors_exclusive_tools_and_non_evidence_break_the_streak(self) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch((_fact("read_file", action="one"),))
        controller.observe_tool_batch((_fact("read_file", action="error", error=True),))
        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="two"),)))
        self.assertIsNone(
            controller.observe_tool_batch((_fact("read_file", action="exclusive", parallel=False),))
        )
        self.assertIsNone(
            controller.observe_tool_batch((_fact("update_plan", progress=ProgressKind.PLAN),))
        )
        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="three"),)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_workspace_verification_and_finalization_are_advisory_phase_boundaries(self) -> None:
        controller = ExecutionEfficiencyController()
        verify = controller.observe_tool_batch(
            (
                _fact(
                    "apply_patch",
                    progress=ProgressKind.WORKSPACE,
                    workspace_changed=True,
                ),
            )
        )
        assert verify is not None
        self.assertEqual(verify.phase, ExecutionPhase.VERIFY)
        self.assertIn("current workspace diff", verify.guidance or "")

        verified = controller.observe_tool_batch(
            (
                _fact(
                    "run_tests",
                    progress=ProgressKind.VERIFICATION,
                    verification_succeeded=True,
                ),
            )
        )
        assert verified is not None
        self.assertEqual(verified.phase, ExecutionPhase.FINALIZE)
        self.assertEqual(verified.reason, EfficiencyReason.VERIFICATION_SUCCEEDED)

        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="new"),)))
        self.assertIsNone(controller.finalize())

    def test_non_successful_verification_stays_in_verify_and_finalization_is_idempotent(
        self,
    ) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch(
            (_fact("edit", progress=ProgressKind.WORKSPACE, workspace_changed=True),)
        )
        failed = controller.observe_tool_batch(
            (_fact("run_tests", progress=ProgressKind.VERIFICATION, error=True),)
        )
        self.assertIsNone(failed)
        self.assertEqual(controller.phase, ExecutionPhase.VERIFY)
        finalized = controller.finalize()
        assert finalized is not None
        self.assertEqual(finalized.reason, EfficiencyReason.TURN_FINALIZED)
        self.assertIsNone(controller.finalize())

    def test_verification_observation_can_enter_verify_without_workspace_mutation(self) -> None:
        controller = ExecutionEfficiencyController()
        update = controller.observe_tool_batch(
            (_fact("run_tests", progress=ProgressKind.VERIFICATION),)
        )

        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.VERIFY)
        self.assertEqual(update.reason, EfficiencyReason.VERIFICATION_OBSERVED)
        self.assertIn("close only a concrete", update.guidance or "")

    def test_out_of_band_verification_boundary_is_visible_to_phase_controller(self) -> None:
        controller = ExecutionEfficiencyController()
        verify = controller.verification_completed(successful=False)
        assert verify is not None
        self.assertEqual(verify.phase, ExecutionPhase.VERIFY)

        finalize = controller.verification_completed(successful=True)
        assert finalize is not None
        self.assertEqual(finalize.phase, ExecutionPhase.FINALIZE)
        self.assertEqual(finalize.reason, EfficiencyReason.VERIFICATION_SUCCEEDED)

    def test_evidence_fingerprint_storage_is_bounded(self) -> None:
        controller = ExecutionEfficiencyController()
        facts = tuple(
            _fact("read_file", action=f"action-{index}", observation=f"output-{index}")
            for index in range(64)
        )
        self.assertIsNone(controller.observe_tool_batch(facts))
        self.assertEqual(controller.evidence_count, 64)

        self.assertIsNone(
            controller.observe_tool_batch((_fact("read_file", action="beyond-bound"),))
        )
        self.assertEqual(controller.evidence_count, 64)

    def test_unbounded_observation_batch_is_rejected(self) -> None:
        controller = ExecutionEfficiencyController()
        facts = tuple(_fact("read_file", action=f"action-{index}") for index in range(129))
        with self.assertRaisesRegex(ValueError, "observation bound"):
            controller.observe_tool_batch(facts)


if __name__ == "__main__":
    unittest.main()
