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

    def test_low_information_round_emits_batching_guidance_and_stays_in_explore(self) -> None:
        controller = ExecutionEfficiencyController()

        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="a"),)))
        update = controller.observe_tool_batch((_fact("grep_many", action="b"),))

        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(update.previous_phase, ExecutionPhase.EXPLORE)
        self.assertEqual(update.reason, EfficiencyReason.LOW_INFORMATION_EXPLORATION)
        self.assertEqual(update.evidence_count, 2)
        self.assertIn("request the independent tool calls together", update.guidance or "")
        self.assertIn("Do not begin broad synthesis", update.guidance or "")
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)
        follow_up = controller.observe_tool_batch((_fact("read_file", action="c"),))
        self.assertIsNone(follow_up)
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(controller.evidence_count, 3)
        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="d"),)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_evidence_and_completed_plan_are_required_to_enter_analysis(self) -> None:
        controller = ExecutionEfficiencyController()
        self.assertIsNone(
            controller.observe_tool_batch(
                (_fact("update_plan", progress=ProgressKind.PLAN),),
                plan_complete=True,
            )
        )
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

        update = controller.observe_tool_batch(
            (_fact("read_file", action="new-evidence"),),
            plan_complete=True,
        )

        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.ANALYZE)
        self.assertEqual(update.reason, EfficiencyReason.EVIDENCE_SUFFICIENT)
        self.assertEqual(update.evidence_count, 1)
        follow_up = controller.observe_tool_batch((_fact("read_file", action="specific-gap"),))
        assert follow_up is not None
        self.assertEqual(follow_up.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(follow_up.reason, EfficiencyReason.ANALYSIS_BACKTRACK)
        self.assertIn("name the remaining targeted evidence needs", follow_up.guidance or "")
        self.assertEqual(follow_up.analysis_backtrack_count, 1)
        self.assertEqual(follow_up.explore_backtracks_before_finalize, 1)
        self.assertEqual(controller.evidence_count, 2)
        self.assertIsNone(controller.observe_tool_batch((_fact("read_file", action="next"),)))
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_insufficient_evidence_or_open_task_state_stays_in_explore(self) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch((_fact("read_file", action="one"),))
        low_information = controller.observe_tool_batch(
            (_fact("read_file", action="two"),),
            plan_complete=False,
            working_set_complete=True,
        )
        assert low_information is not None
        self.assertEqual(low_information.reason, EfficiencyReason.LOW_INFORMATION_EXPLORATION)
        self.assertEqual(low_information.phase, ExecutionPhase.EXPLORE)
        self.assertIsNone(
            controller.observe_tool_batch(
                (_fact("read_file", action="three"),),
                plan_complete=True,
                unresolved_work=True,
            )
        )
        self.assertIsNone(
            controller.observe_tool_batch(
                (_fact("read_file", action="four"),),
                plan_complete=True,
                verification_pending=True,
            )
        )
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

    def test_complete_working_set_can_establish_progress_when_no_plan_exists(self) -> None:
        controller = ExecutionEfficiencyController()
        update = controller.observe_tool_batch(
            (_fact("read_file", action="evidence"),),
            plan_complete=None,
            working_set_complete=True,
        )
        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.ANALYZE)
        self.assertEqual(update.reason, EfficiencyReason.EVIDENCE_SUFFICIENT)

    def test_low_information_guidance_does_not_override_missing_task_progress(self) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch((_fact("read_file", action="one"),))
        guidance = controller.observe_tool_batch((_fact("read_file", action="two"),))
        assert guidance is not None
        self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)
        for index in range(3, 7):
            update = controller.observe_tool_batch((_fact("read_file", action=str(index)),))
            self.assertIsNone(update)
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
        self.assertEqual(verified.phase, ExecutionPhase.VERIFY)
        self.assertEqual(verified.reason, EfficiencyReason.VERIFICATION_SUCCEEDED)
        self.assertIn("Check the current Plan and Working Set", verified.guidance or "")

        blocked = controller.finalize(
            model_has_no_tool_calls=False,
            plan_complete=True,
            verification_ready=True,
        )
        assert blocked is not None
        self.assertEqual(blocked.reason, EfficiencyReason.FINALIZE_GATE_BLOCKED)
        finalized = controller.finalize(
            model_has_no_tool_calls=True,
            plan_complete=True,
            verification_ready=True,
        )
        assert finalized is not None
        self.assertEqual(finalized.phase, ExecutionPhase.FINALIZE)

    def test_non_successful_verification_stays_in_verify_and_gate_blocks_finalization(
        self,
    ) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch(
            (_fact("edit", progress=ProgressKind.WORKSPACE, workspace_changed=True),)
        )
        failed = controller.observe_tool_batch(
            (_fact("run_tests", progress=ProgressKind.VERIFICATION, error=True),)
        )
        assert failed is not None
        self.assertEqual(failed.reason, EfficiencyReason.VERIFICATION_OBSERVED)
        self.assertEqual(controller.phase, ExecutionPhase.VERIFY)
        blocked = controller.finalize(
            model_has_no_tool_calls=True,
            plan_complete=True,
            verification_ready=False,
        )
        assert blocked is not None
        self.assertEqual(blocked.reason, EfficiencyReason.FINALIZE_GATE_BLOCKED)
        self.assertEqual(controller.phase, ExecutionPhase.VERIFY)
        finalized = controller.finalize(
            model_has_no_tool_calls=True,
            plan_complete=True,
            verification_ready=True,
        )
        assert finalized is not None
        self.assertEqual(finalized.reason, EfficiencyReason.TURN_FINALIZED)
        self.assertIsNone(
            controller.finalize(
                model_has_no_tool_calls=True,
                plan_complete=True,
                verification_ready=True,
            )
        )

    def test_verification_observation_can_enter_verify_without_workspace_mutation(self) -> None:
        controller = ExecutionEfficiencyController()
        update = controller.observe_tool_batch(
            (_fact("run_tests", progress=ProgressKind.VERIFICATION),)
        )

        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.VERIFY)
        self.assertEqual(update.reason, EfficiencyReason.VERIFICATION_OBSERVED)
        self.assertIn("specific gap", update.guidance or "")

    def test_out_of_band_verification_boundary_is_visible_to_phase_controller(self) -> None:
        controller = ExecutionEfficiencyController()
        verify = controller.verification_completed(successful=False)
        assert verify is not None
        self.assertEqual(verify.phase, ExecutionPhase.VERIFY)

        verified = controller.verification_completed(successful=True)
        assert verified is not None
        self.assertEqual(verified.phase, ExecutionPhase.VERIFY)
        finalize = controller.finalize(
            model_has_no_tool_calls=True,
            plan_complete=None,
            verification_ready=True,
        )
        assert finalize is not None
        self.assertEqual(finalize.phase, ExecutionPhase.FINALIZE)

    def test_verification_backtrack_returns_to_explore_for_targeted_evidence(self) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch(
            (_fact("apply_patch", progress=ProgressKind.WORKSPACE, workspace_changed=True),)
        )
        update = controller.observe_tool_batch((_fact("read_file", action="validation-gap"),))
        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(update.reason, EfficiencyReason.VERIFICATION_BACKTRACK)
        self.assertEqual(update.explore_backtracks_before_finalize, 1)

    def test_analyze_workspace_tool_is_counted_before_entering_verify(self) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch(
            (_fact("read_file", action="evidence"),),
            plan_complete=True,
        )
        update = controller.observe_tool_batch(
            (
                _fact(
                    "apply_patch",
                    progress=ProgressKind.WORKSPACE,
                    workspace_changed=True,
                ),
            ),
            plan_complete=False,
            unresolved_work=True,
        )
        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.VERIFY)
        self.assertEqual(update.reason, EfficiencyReason.WORKSPACE_CHANGED)
        self.assertEqual(update.analysis_backtrack_count, 1)

    def test_analyze_tool_request_is_counted_when_result_cannot_be_classified(self) -> None:
        controller = ExecutionEfficiencyController()
        controller.observe_tool_batch(
            (_fact("read_file", action="evidence"),),
            plan_complete=True,
        )
        update = controller.observe_tool_batch((), model_requested_tools=True)
        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.EXPLORE)
        self.assertEqual(update.reason, EfficiencyReason.ANALYSIS_BACKTRACK)
        self.assertEqual(update.analysis_backtrack_count, 1)

    def test_finalization_requires_no_tools_resolved_plan_and_verification(self) -> None:
        controller = ExecutionEfficiencyController()
        for model_has_no_tool_calls, plan_complete, unresolved_work, verification_ready in (
            (False, True, False, True),
            (True, False, False, True),
            (True, True, True, True),
            (True, True, False, False),
        ):
            update = controller.finalize(
                model_has_no_tool_calls=model_has_no_tool_calls,
                plan_complete=plan_complete,
                unresolved_work=unresolved_work,
                verification_ready=verification_ready,
            )
            assert update is not None
            self.assertEqual(update.reason, EfficiencyReason.FINALIZE_GATE_BLOCKED)
            self.assertEqual(controller.phase, ExecutionPhase.EXPLORE)

        update = controller.finalize(
            model_has_no_tool_calls=True,
            plan_complete=True,
            unresolved_work=False,
            verification_ready=True,
        )
        assert update is not None
        self.assertEqual(update.phase, ExecutionPhase.FINALIZE)
        self.assertEqual(update.reason, EfficiencyReason.TURN_FINALIZED)

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
