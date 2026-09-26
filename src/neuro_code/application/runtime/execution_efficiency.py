"""Bounded, advisory execution-efficiency feedback for one Agent turn.

This controller observes already-redacted tool progress and emits at most one
low-information exploration checkpoint. It never schedules, suppresses, or
reorders tools and never changes Supervisor decisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from neuro_code.domain.execution import ProgressKind

MAX_EFFICIENCY_EVIDENCE_FINGERPRINTS = 64
LOW_INFORMATION_SINGLETON_BATCHES = 2

_SIMPLE_EVIDENCE_TOOLS = frozenset(
    {"read_file", "read_files", "grep_many", "list_tree", "glob", "git_inspect"}
)
_ANALYZE_GUIDANCE = (
    "Runtime execution phase: ANALYZE. Pause broad discovery and synthesize the evidence "
    "already gathered. Name only concrete evidence gaps; batch independent read/search needs "
    "in one request, and keep genuinely dependent follow-ups sequential. Continue targeted "
    "exploration when new evidence reveals a specific gap. This is guidance only; use the "
    "evidence required for a correct result."
)


class ExecutionPhase(StrEnum):
    EXPLORE = "explore"
    ANALYZE = "analyze"
    VERIFY = "verify"
    FINALIZE = "finalize"


class EfficiencyReason(StrEnum):
    TURN_STARTED = "turn_started"
    STRUCTURED_PLAN_COMPLETED = "structured_plan_completed"
    LOW_INFORMATION_EXPLORATION = "low_information_exploration"
    TARGETED_EVIDENCE_REQUESTED = "targeted_evidence_requested"
    WORKSPACE_CHANGED = "workspace_changed"
    VERIFICATION_OBSERVED = "verification_observed"
    VERIFICATION_SUCCEEDED = "verification_succeeded"
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
    guidance: str | None

    def __post_init__(self) -> None:
        if self.previous_phase is not None and not isinstance(self.previous_phase, ExecutionPhase):
            raise TypeError("previous_phase must be an ExecutionPhase or None")
        if not isinstance(self.phase, ExecutionPhase) or not isinstance(
            self.reason, EfficiencyReason
        ):
            raise TypeError("phase and reason must be canonical efficiency values")
        if min(self.singleton_streak, self.evidence_count) < 0:
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
            "guidance_emitted": self.guidance is not None,
        }


class ExecutionEfficiencyController:
    """Track a bounded advisory phase projection for a single turn."""

    __slots__ = (
        "_analysis_guidance_sent",
        "_evidence_fingerprints",
        "_phase",
        "_singleton_streak",
        "_started",
    )

    def __init__(self) -> None:
        self._phase = ExecutionPhase.EXPLORE
        self._started = False
        self._singleton_streak = 0
        self._analysis_guidance_sent = False
        self._evidence_fingerprints: set[tuple[str, str]] = set()

    @property
    def phase(self) -> ExecutionPhase:
        return self._phase

    @property
    def evidence_count(self) -> int:
        return len(self._evidence_fingerprints)

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
            guidance=None,
        )

    def observe_tool_batch(
        self,
        facts: Sequence[ToolEvidenceFact],
        *,
        plan_complete: bool = False,
    ) -> ExecutionEfficiencyUpdate | None:
        """Observe one completed model-request batch without controlling it."""

        if not isinstance(plan_complete, bool):
            raise TypeError("plan_complete must be a bool")
        normalized = tuple(facts)
        if not all(isinstance(fact, ToolEvidenceFact) for fact in normalized):
            raise TypeError("facts must contain ToolEvidenceFact values")
        if len(normalized) > 128:
            raise ValueError("tool batch exceeds the efficiency observation bound")
        if not normalized:
            self._singleton_streak = 0
            return None
        new_evidence = self._record_evidence(normalized)
        if self._phase is ExecutionPhase.FINALIZE:
            return None

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

        if new_evidence and self._phase is ExecutionPhase.ANALYZE:
            self._singleton_streak = 0
            return self._transition(
                ExecutionPhase.EXPLORE,
                EfficiencyReason.TARGETED_EVIDENCE_REQUESTED,
                None,
            )

        if plan_complete and self._phase is ExecutionPhase.EXPLORE:
            self._singleton_streak = 0
            guidance = None
            if not self._analysis_guidance_sent:
                self._analysis_guidance_sent = True
                guidance = _ANALYZE_GUIDANCE
            return self._transition(
                ExecutionPhase.ANALYZE,
                EfficiencyReason.STRUCTURED_PLAN_COMPLETED,
                guidance,
            )

        candidate = (
            len(normalized) == 1 and self._is_simple_singleton(normalized[0]) and new_evidence
        )
        self._singleton_streak = self._singleton_streak + 1 if candidate else 0
        if (
            self._phase is ExecutionPhase.EXPLORE
            and not self._analysis_guidance_sent
            and self._singleton_streak >= LOW_INFORMATION_SINGLETON_BATCHES
        ):
            self._analysis_guidance_sent = True
            return self._transition(
                ExecutionPhase.ANALYZE,
                EfficiencyReason.LOW_INFORMATION_EXPLORATION,
                _ANALYZE_GUIDANCE,
            )
        return None

    def verification_completed(self, *, successful: bool) -> ExecutionEfficiencyUpdate | None:
        """Observe a verification boundary that runs outside the tool batch loop."""

        if not isinstance(successful, bool):
            raise TypeError("successful must be a bool")
        self._singleton_streak = 0
        if self._phase is ExecutionPhase.FINALIZE:
            return None
        if self._phase is ExecutionPhase.VERIFY and successful:
            return self._transition(
                ExecutionPhase.FINALIZE,
                EfficiencyReason.VERIFICATION_SUCCEEDED,
                "Runtime execution phase: FINALIZE. The current verification completed "
                "successfully. Summarize the result and checks; do not continue nonessential "
                "exploration.",
            )
        return self._transition(
            ExecutionPhase.VERIFY,
            EfficiencyReason.VERIFICATION_OBSERVED,
            "Runtime execution phase: VERIFY. Interpret this verification result and close "
            "only a concrete remaining check. If verification passed, prepare the final "
            "response; do not broaden exploration.",
        )

    def finalize(self) -> ExecutionEfficiencyUpdate | None:
        """Record terminal phase for diagnostics; no prompt is changed."""

        return self._transition(
            ExecutionPhase.FINALIZE,
            EfficiencyReason.TURN_FINALIZED,
            None,
        )

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
        return ExecutionEfficiencyUpdate(
            phase=phase,
            previous_phase=previous,
            reason=reason,
            singleton_streak=self._singleton_streak,
            evidence_count=self.evidence_count,
            guidance=guidance,
        )


__all__ = [
    "LOW_INFORMATION_SINGLETON_BATCHES",
    "MAX_EFFICIENCY_EVIDENCE_FINGERPRINTS",
    "EfficiencyReason",
    "ExecutionEfficiencyController",
    "ExecutionEfficiencyUpdate",
    "ExecutionPhase",
    "ToolEvidenceFact",
]
