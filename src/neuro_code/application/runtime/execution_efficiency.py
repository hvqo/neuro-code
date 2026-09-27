"""Bounded, advisory execution-efficiency feedback for one Agent turn.

This controller observes already-redacted progress and existing task state. It
can encourage evidence batching and annotate phase boundaries, but it never
schedules, suppresses, or reorders tools and never changes Supervisor
decisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from neuro_code.domain.execution import ProgressKind

MAX_EFFICIENCY_EVIDENCE_FINGERPRINTS = 64
LOW_INFORMATION_SINGLETON_BATCHES = 2
MAX_EFFICIENCY_BACKTRACKS = 128

_SIMPLE_EVIDENCE_TOOLS = frozenset(
    {"read_file", "read_files", "grep_many", "list_tree", "glob", "git_inspect"}
)
_EXPLORE_BATCH_GUIDANCE = (
    "Runtime execution phase: EXPLORE. Reconcile the remaining evidence needs with the current "
    "Plan and Working Set. If the needed files, searches, or checks are identifiable, request "
    "the independent tool calls together now; wait for results before dependent follow-ups. "
    "Keep gathering evidence while planned or unresolved work remains. Do not begin broad "
    "synthesis yet."
)
_ANALYZE_GUIDANCE = (
    "Runtime execution phase: ANALYZE. Existing task progress and recorded evidence indicate "
    "that exploration is substantially complete. Synthesize the evidence now. Request more "
    "tools only for a concrete gap; when needed, state the targeted evidence needs and batch "
    "independent reads/searches while keeping dependent steps sequential."
)
_ANALYSIS_BACKTRACK_GUIDANCE = (
    "Runtime execution phase: EXPLORE after analysis requested more evidence. In this next "
    "response, name the remaining targeted evidence needs and request independent reads/searches "
    "together. Keep dependent follow-ups sequential, then return to synthesis after the results."
)
_VERIFICATION_BACKTRACK_GUIDANCE = (
    "Runtime execution phase: EXPLORE after verification exposed a concrete evidence gap. "
    "Request only the targeted evidence needed to close that gap, batching independent reads "
    "and keeping dependent checks sequential."
)


class ExecutionPhase(StrEnum):
    EXPLORE = "explore"
    ANALYZE = "analyze"
    VERIFY = "verify"
    FINALIZE = "finalize"


class EfficiencyReason(StrEnum):
    TURN_STARTED = "turn_started"
    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    LOW_INFORMATION_EXPLORATION = "low_information_exploration"
    ANALYSIS_BACKTRACK = "analysis_backtrack"
    VERIFICATION_BACKTRACK = "verification_backtrack"
    WORKSPACE_CHANGED = "workspace_changed"
    VERIFICATION_OBSERVED = "verification_observed"
    VERIFICATION_SUCCEEDED = "verification_succeeded"
    FINALIZE_GATE_BLOCKED = "finalize_gate_blocked"
    TURN_FINALIZED = "turn_finalized"


@dataclass(frozen=True, slots=True)
class ToolEvidenceFact:
    """Bounded facts needed to classify one completed tool call."""

    tool_name: str
    action_digest: str
    observation_digest: str
    is_error: bool
    progress_kind: ProgressKind
    parallel_safe: bool
    composite_batch: bool = False
    workspace_changed: bool = False
    verification_succeeded: bool = False

    def __post_init__(self) -> None:
        if not self.tool_name or len(self.tool_name) > 96:
            raise ValueError("tool_name must be a bounded non-empty name")
        for name, digest in (
            ("action_digest", self.action_digest),
            ("observation_digest", self.observation_digest),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not isinstance(self.progress_kind, ProgressKind):
            raise TypeError("progress_kind must be a ProgressKind")
        for name in (
            "is_error",
            "parallel_safe",
            "composite_batch",
            "workspace_changed",
            "verification_succeeded",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")

    @property
    def evidence_fingerprint(self) -> tuple[str, str]:
        return self.action_digest, self.observation_digest


@dataclass(frozen=True, slots=True)
class ExecutionEfficiencyUpdate:
    phase: ExecutionPhase
    previous_phase: ExecutionPhase | None
    reason: EfficiencyReason
    singleton_streak: int
    evidence_count: int
    analysis_backtrack_count: int
    explore_backtracks_before_finalize: int
    guidance: str | None

    def __post_init__(self) -> None:
        if self.previous_phase is not None and not isinstance(self.previous_phase, ExecutionPhase):
            raise TypeError("previous_phase must be an ExecutionPhase or None")
        if not isinstance(self.phase, ExecutionPhase) or not isinstance(
            self.reason, EfficiencyReason
        ):
            raise TypeError("phase and reason must be canonical efficiency values")
        if (
            min(
                self.singleton_streak,
                self.evidence_count,
                self.analysis_backtrack_count,
                self.explore_backtracks_before_finalize,
            )
            < 0
        ):
            raise ValueError("efficiency counts must be non-negative")
        if self.guidance is not None and len(self.guidance.encode("utf-8")) > 1200:
            raise ValueError("efficiency guidance exceeds its byte bound")

    def to_event_data(self, *, step: int) -> dict[str, object]:
        return {
            "step": step,
            "phase": self.phase.value,
            "previous_phase": self.previous_phase.value if self.previous_phase else None,
            "reason_code": self.reason.value,
            "singleton_streak": self.singleton_streak,
            "evidence_count": self.evidence_count,
            "analysis_backtrack_count": self.analysis_backtrack_count,
            "explore_backtracks_before_finalize": self.explore_backtracks_before_finalize,
            "guidance_emitted": self.guidance is not None,
        }


class ExecutionEfficiencyController:
    """Track a bounded advisory phase projection for a single turn."""

    __slots__ = (
        "_analysis_backtrack_count",
        "_evidence_fingerprints",
        "_explore_backtracks_before_finalize",
        "_finalized",
        "_low_information_guidance_sent",
        "_phase",
        "_singleton_streak",
        "_started",
    )

    def __init__(self) -> None:
        self._phase = ExecutionPhase.EXPLORE
        self._started = False
        self._singleton_streak = 0
        self._low_information_guidance_sent = False
        self._analysis_backtrack_count = 0
        self._explore_backtracks_before_finalize = 0
        self._finalized = False
        self._evidence_fingerprints: set[tuple[str, str]] = set()

    @property
    def phase(self) -> ExecutionPhase:
        return self._phase

    @property
    def evidence_count(self) -> int:
        return len(self._evidence_fingerprints)

    @property
    def analysis_backtrack_count(self) -> int:
        return self._analysis_backtrack_count

    @property
    def explore_backtracks_before_finalize(self) -> int:
        return self._explore_backtracks_before_finalize

    def start(self) -> ExecutionEfficiencyUpdate | None:
        """Emit the initial phase fact once without changing model context."""

        if self._started:
            return None
        self._started = True
        return ExecutionEfficiencyUpdate(
            phase=ExecutionPhase.EXPLORE,
            previous_phase=None,
            reason=EfficiencyReason.TURN_STARTED,
            singleton_streak=0,
            evidence_count=0,
            analysis_backtrack_count=0,
            explore_backtracks_before_finalize=0,
            guidance=None,
        )

    def observe_tool_batch(
        self,
        facts: Sequence[ToolEvidenceFact],
        *,
        plan_complete: bool | None = None,
        working_set_complete: bool = False,
        unresolved_work: bool = False,
        verification_pending: bool = False,
        model_requested_tools: bool = False,
    ) -> ExecutionEfficiencyUpdate | None:
        """Observe one completed model-request batch and existing task signals.

        ``None`` means there is no structured Plan. A completed Plan or a
        complete Working Set can establish task progress, but only when new
        evidence exists and no known unresolved work or pending verification
        remains.
        """

        if plan_complete is not None and not isinstance(plan_complete, bool):
            raise TypeError("plan_complete must be a bool or None")
        for name, value in (
            ("working_set_complete", working_set_complete),
            ("unresolved_work", unresolved_work),
            ("verification_pending", verification_pending),
            ("model_requested_tools", model_requested_tools),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")
        normalized = tuple(facts)
        if not all(isinstance(fact, ToolEvidenceFact) for fact in normalized):
            raise TypeError("facts must contain ToolEvidenceFact values")
        if len(normalized) > 128:
            raise ValueError("tool batch exceeds the efficiency observation bound")
        if not normalized:
            self._singleton_streak = 0
            if model_requested_tools and self._phase is ExecutionPhase.ANALYZE:
                self._record_analysis_backtrack()
                return self._transition(
                    ExecutionPhase.EXPLORE,
                    EfficiencyReason.ANALYSIS_BACKTRACK,
                    _ANALYSIS_BACKTRACK_GUIDANCE,
                )
            return None
        new_evidence = self._record_evidence(normalized)
        if self._phase is ExecutionPhase.FINALIZE:
            return None

        requested_tools_after_analysis = self._phase is ExecutionPhase.ANALYZE
        if requested_tools_after_analysis:
            self._record_analysis_backtrack()

        changed_workspace = any(
            fact.workspace_changed or fact.progress_kind is ProgressKind.WORKSPACE
            for fact in normalized
        )
        verification = tuple(
            fact for fact in normalized if fact.progress_kind is ProgressKind.VERIFICATION
        )
        if changed_workspace:
            self._singleton_streak = 0
            return self._transition(
                ExecutionPhase.VERIFY,
                EfficiencyReason.WORKSPACE_CHANGED,
                "Runtime execution phase: VERIFY. Review the current workspace diff and run only "
                "the checks needed to confirm the requested change. Keep unrelated discovery out "
                "of this phase; report any remaining verification gap explicitly.",
            )
        if verification:
            successful = all(fact.verification_succeeded for fact in verification)
            return self.verification_completed(successful=successful)

        if self._phase is ExecutionPhase.ANALYZE:
            self._singleton_streak = 0
            return self._transition(
                ExecutionPhase.EXPLORE,
                EfficiencyReason.ANALYSIS_BACKTRACK,
                _ANALYSIS_BACKTRACK_GUIDANCE,
            )

        if self._phase is ExecutionPhase.VERIFY and new_evidence:
            self._singleton_streak = 0
            return self._transition(
                ExecutionPhase.EXPLORE,
                EfficiencyReason.VERIFICATION_BACKTRACK,
                _VERIFICATION_BACKTRACK_GUIDANCE,
            )

        if self._phase is ExecutionPhase.EXPLORE and self._evidence_is_sufficient(
            plan_complete=plan_complete,
            working_set_complete=working_set_complete,
            unresolved_work=unresolved_work,
            verification_pending=verification_pending,
        ):
            self._singleton_streak = 0
            return self._transition(
                ExecutionPhase.ANALYZE,
                EfficiencyReason.EVIDENCE_SUFFICIENT,
                _ANALYZE_GUIDANCE,
            )

        candidate = (
            len(normalized) == 1 and self._is_simple_singleton(normalized[0]) and new_evidence
        )
        self._singleton_streak = self._singleton_streak + 1 if candidate else 0
        if (
            self._phase is ExecutionPhase.EXPLORE
            and not self._low_information_guidance_sent
            and self._singleton_streak >= LOW_INFORMATION_SINGLETON_BATCHES
        ):
            self._low_information_guidance_sent = True
            return self._checkpoint(
                EfficiencyReason.LOW_INFORMATION_EXPLORATION,
                _EXPLORE_BATCH_GUIDANCE,
            )
        return None

    def verification_completed(self, *, successful: bool) -> ExecutionEfficiencyUpdate | None:
        """Observe a verification boundary that runs outside the tool batch loop."""

        if not isinstance(successful, bool):
            raise TypeError("successful must be a bool")
        self._singleton_streak = 0
        if self._phase is ExecutionPhase.FINALIZE:
            return None
        if self._phase is not ExecutionPhase.VERIFY:
            return self._transition(
                ExecutionPhase.VERIFY,
                (
                    EfficiencyReason.VERIFICATION_SUCCEEDED
                    if successful
                    else EfficiencyReason.VERIFICATION_OBSERVED
                ),
                self._verification_guidance(successful),
            )
        return self._checkpoint(
            (
                EfficiencyReason.VERIFICATION_SUCCEEDED
                if successful
                else EfficiencyReason.VERIFICATION_OBSERVED
            ),
            self._verification_guidance(successful),
        )

    def finalize(
        self,
        *,
        model_has_no_tool_calls: bool = False,
        plan_complete: bool | None = None,
        unresolved_work: bool = False,
        verification_ready: bool = True,
    ) -> ExecutionEfficiencyUpdate | None:
        """Record FINALIZE only after deterministic completion gates pass.

        A terminal Supervisor decision does not imply that the model no longer
        needs tools, so callers must explicitly provide the no-tool signal.
        This remains diagnostic/advisory and never delays or changes the turn.
        """

        for name, value in (
            ("model_has_no_tool_calls", model_has_no_tool_calls),
            ("unresolved_work", unresolved_work),
            ("verification_ready", verification_ready),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")
        if plan_complete is not None and not isinstance(plan_complete, bool):
            raise TypeError("plan_complete must be a bool or None")
        self._singleton_streak = 0
        if self._phase is ExecutionPhase.FINALIZE:
            return None
        if (
            not model_has_no_tool_calls
            or plan_complete is False
            or unresolved_work
            or not verification_ready
        ):
            return self._checkpoint(EfficiencyReason.FINALIZE_GATE_BLOCKED, None)
        self._finalized = True
        return self._transition(ExecutionPhase.FINALIZE, EfficiencyReason.TURN_FINALIZED, None)

    def reset_low_information_streak(self) -> None:
        """Break a candidate streak when a tool outcome cannot be classified."""

        self._singleton_streak = 0

    def _record_evidence(self, facts: Sequence[ToolEvidenceFact]) -> bool:
        added = False
        for fact in facts:
            if (
                fact.is_error
                or fact.progress_kind is not ProgressKind.EVIDENCE
                or len(self._evidence_fingerprints) >= MAX_EFFICIENCY_EVIDENCE_FINGERPRINTS
            ):
                continue
            if fact.evidence_fingerprint not in self._evidence_fingerprints:
                self._evidence_fingerprints.add(fact.evidence_fingerprint)
                added = True
        return added

    @staticmethod
    def _is_simple_singleton(fact: ToolEvidenceFact) -> bool:
        return (
            fact.tool_name in _SIMPLE_EVIDENCE_TOOLS
            and fact.parallel_safe
            and not fact.composite_batch
            and not fact.is_error
            and fact.progress_kind is ProgressKind.EVIDENCE
        )

    def _evidence_is_sufficient(
        self,
        *,
        plan_complete: bool | None,
        working_set_complete: bool,
        unresolved_work: bool,
        verification_pending: bool,
    ) -> bool:
        task_progress_complete = (
            plan_complete is True if plan_complete is not None else working_set_complete
        )
        return (
            self.evidence_count > 0
            and task_progress_complete
            and not unresolved_work
            and not verification_pending
        )

    @staticmethod
    def _verification_guidance(successful: bool) -> str:
        if successful:
            return (
                "Runtime execution phase: VERIFY. This verification passed. Check the current "
                "Plan and Working Set for unresolved work; request only a concrete remaining "
                "evidence need or check, otherwise return the result."
            )
        return (
            "Runtime execution phase: VERIFY. This verification did not pass or remains "
            "incomplete. Inspect the reported verification evidence and close only the "
            "specific gap; do not broaden discovery."
        )

    def _checkpoint(
        self,
        reason: EfficiencyReason,
        guidance: str | None,
    ) -> ExecutionEfficiencyUpdate:
        return ExecutionEfficiencyUpdate(
            phase=self._phase,
            previous_phase=self._phase,
            reason=reason,
            singleton_streak=self._singleton_streak,
            evidence_count=self.evidence_count,
            analysis_backtrack_count=self._analysis_backtrack_count,
            explore_backtracks_before_finalize=self._explore_backtracks_before_finalize,
            guidance=guidance,
        )

    def _record_analysis_backtrack(self) -> None:
        self._analysis_backtrack_count = min(
            MAX_EFFICIENCY_BACKTRACKS,
            self._analysis_backtrack_count + 1,
        )

    def _transition(
        self,
        phase: ExecutionPhase,
        reason: EfficiencyReason,
        guidance: str | None,
    ) -> ExecutionEfficiencyUpdate | None:
        if phase is self._phase:
            return None
        previous = self._phase
        self._phase = phase
        if (
            phase is ExecutionPhase.EXPLORE
            and previous in {ExecutionPhase.ANALYZE, ExecutionPhase.VERIFY}
            and not self._finalized
        ):
            self._explore_backtracks_before_finalize = min(
                MAX_EFFICIENCY_BACKTRACKS,
                self._explore_backtracks_before_finalize + 1,
            )
        return ExecutionEfficiencyUpdate(
            phase=phase,
            previous_phase=previous,
            reason=reason,
            singleton_streak=self._singleton_streak,
            evidence_count=self.evidence_count,
            analysis_backtrack_count=self._analysis_backtrack_count,
            explore_backtracks_before_finalize=self._explore_backtracks_before_finalize,
            guidance=guidance,
        )


__all__ = [
    "LOW_INFORMATION_SINGLETON_BATCHES",
    "MAX_EFFICIENCY_BACKTRACKS",
    "MAX_EFFICIENCY_EVIDENCE_FINGERPRINTS",
    "EfficiencyReason",
    "ExecutionEfficiencyController",
    "ExecutionEfficiencyUpdate",
    "ExecutionPhase",
    "ToolEvidenceFact",
]
