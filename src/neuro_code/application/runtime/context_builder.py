"""Per-turn context builder collaborator.

Stage 3C of the Runtime Kernel split: this module owns the request-scoped
policy guidance, repository instruction refresh, and skill listing injection
previously embedded in ``AgentRuntime``.  It owns the stable request prefix
and the current ``reasoning_effort``, ``interaction_mode``, ``plan``, and
``plan_comments`` values so the runtime loop and external setters share one
source of truth.

The module intentionally does not import :mod:`agent`; it depends only on
domain values and callable providers.

提供每个回合使用的上下文构建协作者,负责策略指引、指令刷新和技能注入.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from neuro_code.application.runtime.projection_journal import (
    ProviderProjectionJournal,
)
from neuro_code.domain.conversation.interaction_mode import (
    InteractionMode,
    interaction_mode_guidance,
)
from neuro_code.domain.conversation.messages import Message, Role, SessionItem, SyntheticReason
from neuro_code.domain.conversation.reasoning import ReasoningEffort, reasoning_guidance
from neuro_code.domain.execution import (
    ExecutionBudgetPressure,
    ExecutionBudgetUsage,
    ExecutionSegmentCheckpoint,
)
from neuro_code.domain.plans import PlanComment, SessionPlan
from neuro_code.domain.workspace.instructions import InstructionDiscoveryResult
from neuro_code.domain.workspace.skills import SkillDiscoveryResult

BATCH_FIRST_RUNTIME_GUIDANCE = """Runtime evidence-gathering guidance:
When multiple read-only operations are independent, batch them in one model step. For repository
work, use list_tree, grep_many, and read_files to map the repository, find relevant symbols, and
batch-read the related evidence before follow-up. Use glob for known filename/path patterns. Review
edits with workspace_diff before verification; keep dependent operations sequential. Prefer
workspace evidence. For required public facts absent locally, use web_search; if unavailable,
state the blocker and mark them unverified. Do not silently substitute shell web scraping."""

REPLAN_RUNTIME_GUIDANCE = """Runtime supervision guidance:
The current approach is repeating results without sufficient progress. Change strategy. Avoid
repeating the same tool or action with equivalent arguments. Narrow or broaden the search,
inspect different evidence, or revise the current assumption. After local absence is established,
stop scanning. Use web_search for required public facts; if unavailable, state the blocker and
leave them unverified. Do not silently substitute shell scraping."""

_UNLOADED_PROJECT_MEMORY_SNAPSHOT = object()
_UNLOADED_WORKSPACE_SNAPSHOT = object()

_JOURNALLED_RUNTIME_REASONS = frozenset(
    {
        SyntheticReason.WORKING_SET,
        SyntheticReason.RUNTIME_PLAN,
        SyntheticReason.RUNTIME_BUDGET,
        SyntheticReason.RUNTIME_CHECKPOINT,
        SyntheticReason.RUNTIME_SUPERVISION,
        SyntheticReason.RUNTIME_BACKGROUND_TASK,
        SyntheticReason.RUNTIME_CONTEXT_ROLLOVER,
        SyntheticReason.INSTRUCTION_SCOPE_REVISION,
        SyntheticReason.SKILL_SCOPE_REVISION,
    }
)


def _budget_runtime_guidance(pressure: ExecutionBudgetPressure) -> str:
    pressure_guidance = {
        ExecutionBudgetPressure.CONSERVE: (
            "Prioritize the core question and avoid open-ended exploration."
        ),
        ExecutionBudgetPressure.FOCUS: (
            "Merge independent searches and reads; perform only necessary follow-up."
        ),
        ExecutionBudgetPressure.FINAL_STAGE: (
            "Stop nonessential exploration, complete only necessary verification, and prepare the answer."
        ),
    }[pressure]
    return (
        f"Runtime budget guidance ({pressure.value}):\n"
        "This latest pressure state supersedes earlier budget guidance.\n"
        f"{pressure_guidance}"
    )


def _segment_runtime_guidance(checkpoint: ExecutionSegmentCheckpoint) -> str:
    progress = ", ".join(kind.value for kind in checkpoint.progress_kinds)
    return (
        "Runtime segment checkpoint:\n"
        f"Segment {checkpoint.segment_number} completed after "
        f"{checkpoint.model_calls} model calls, {checkpoint.tool_rounds} tool rounds, and "
        f"{checkpoint.tool_calls} tool calls. Confirmed progress categories: {progress}. "
        "This is the latest execution position; earlier checkpoints remain confirmed progress. "
        "Continue the same user task in the next bounded segment. Reuse recorded evidence, avoid "
        "repeating equivalent actions, and do not claim unrecorded work."
    )


class ContextBuilder:
    """Build a stable prefix and an append-only synthetic-context projection.

    The first repository instruction and skill catalog are pinned for the
    active epoch. Later scope/catalog changes append anchored revisions to a
    bounded in-memory journal. Working Set and runtime notices use the same
    journal, while Project Memory remains generation-pinned. None of these
    synthetic messages enters canonical Session history.

    为请求构建稳定前缀与仅追加的 synthetic context 投影. 当前 epoch 的首份仓库指令和技能目录保持固定. 后续
    作用域变化以有界、带锚点的修订追加. Working Set 与运行时通知共用该内存日志, Project Memory 按 generation 固定.
    合成消息都不会进入规范 Session 历史。
    """

    __slots__ = (
        "_dag_result_relay_message",
        "_instruction_provider",
        "_interaction_mode",
        "_last_instruction_result",
        "_last_skill_result",
        "_latest_instruction_message",
        "_latest_skill_message",
        "_parent_relay_message",
        "_plan",
        "_plan_comments",
        "_project_memory_provider",
        "_project_memory_snapshot",
        "_projection_journal",
        "_reasoning_effort",
        "_skill_provider",
        "_stable_instruction_message",
        "_stable_skill_message",
    )

    def __init__(
        self,
        *,
        reasoning_effort: ReasoningEffort,
        interaction_mode: InteractionMode,
        plan: SessionPlan | None,
        instruction_provider: Callable[[], InstructionDiscoveryResult | None] | None,
        skill_provider: Callable[[], SkillDiscoveryResult | None] | None,
        project_memory_provider: Callable[[], str | None] | None = None,
        parent_relay_message: Message | None = None,
        dag_result_relay_message: Message | None = None,
    ) -> None:
        self._reasoning_effort = reasoning_effort
        self._interaction_mode = interaction_mode
        self._plan = plan
        self._plan_comments: tuple[PlanComment, ...] = ()
        self._instruction_provider = instruction_provider
        self._skill_provider = skill_provider
        self._project_memory_provider = project_memory_provider
        self._project_memory_snapshot: str | None | object = _UNLOADED_PROJECT_MEMORY_SNAPSHOT
        if parent_relay_message is not None and (
            not isinstance(parent_relay_message, Message)
            or parent_relay_message.synthetic_reason is not SyntheticReason.PARENT_RELAY
        ):
            raise TypeError("parent relay message must be canonical synthetic context")
        if dag_result_relay_message is not None and (
            not isinstance(dag_result_relay_message, Message)
            or dag_result_relay_message.synthetic_reason
            is not SyntheticReason.DAG_PREDECESSOR_RESULTS
        ):
            raise TypeError("DAG result relay message must be canonical synthetic context")
        self._parent_relay_message = parent_relay_message
        self._dag_result_relay_message = dag_result_relay_message
        self._last_instruction_result: InstructionDiscoveryResult | None = None
        self._last_skill_result: SkillDiscoveryResult | None = None
        self._projection_journal = ProviderProjectionJournal()
        self._stable_instruction_message: Message | None | object = _UNLOADED_WORKSPACE_SNAPSHOT
        self._stable_skill_message: Message | None | object = _UNLOADED_WORKSPACE_SNAPSHOT
        self._latest_instruction_message: Message | None | object = _UNLOADED_WORKSPACE_SNAPSHOT
        self._latest_skill_message: Message | None | object = _UNLOADED_WORKSPACE_SNAPSHOT

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self._reasoning_effort

    def set_reasoning_effort(self, effort: ReasoningEffort) -> None:
        if not isinstance(effort, ReasoningEffort):
            raise TypeError("reasoning effort must be a ReasoningEffort")
        self._reasoning_effort = effort

    @property
    def interaction_mode(self) -> InteractionMode:
        return self._interaction_mode

    def set_interaction_mode(self, mode: InteractionMode) -> None:
        if not isinstance(mode, InteractionMode):
            raise TypeError("interaction mode must be an InteractionMode")
        self._interaction_mode = mode

    def invalidate_project_memory_snapshot(self) -> None:
        """Reload Project Memory before the next request in a new context scope.

        The next ``build`` reads the bounded index once and pins it for the
        active generation. Scope changes and committed fresh-context rollovers
        call this method; ordinary turns and extraction writes do not.

        使当前项目记忆快照失效;在下一次请求构建时重新读取。普通回合和后台提取不会调用此方法。
        """

        self._project_memory_snapshot = _UNLOADED_PROJECT_MEMORY_SNAPSHOT

    def reset_projection_epoch(self) -> None:
        """Reset bounded synthetic revisions at an explicit cache boundary.

        Context rewriting is authorized only when the caller has already
        advanced its cache epoch. A new epoch can establish current instruction
        and skill snapshots without deleting or persisting conversation truth.
        """

        self._projection_journal.reset()
        self._stable_instruction_message = _UNLOADED_WORKSPACE_SNAPSHOT
        self._stable_skill_message = _UNLOADED_WORKSPACE_SNAPSHOT
        self._latest_instruction_message = _UNLOADED_WORKSPACE_SNAPSHOT
        self._latest_skill_message = _UNLOADED_WORKSPACE_SNAPSHOT

    def _load_project_memory_snapshot(self) -> str | None:
        if self._project_memory_provider is None:
            return None
        snapshot = self._project_memory_provider()
        if snapshot is not None and not isinstance(snapshot, str):
            raise TypeError("project memory provider must return text or None")
        if snapshot is not None and len(snapshot.encode("utf-8")) > 24_576:
            raise ValueError("project memory index exceeds its context byte limit")
        return snapshot

    @property
    def plan(self) -> SessionPlan | None:
        return self._plan

    def set_plan(self, plan: SessionPlan | None) -> None:
        if plan is not None and not isinstance(plan, SessionPlan):
            raise TypeError("plan must be a SessionPlan or None")
        self._plan = plan
        self._plan_comments = ()

    @property
    def plan_comments(self) -> tuple[PlanComment, ...]:
        return self._plan_comments

    def set_plan_comments(self, comments: Sequence[PlanComment]) -> None:
        normalized = tuple(comments)
        if not all(isinstance(comment, PlanComment) for comment in normalized):
            raise TypeError("plan comments must be PlanComment values")
        if normalized and self._plan is None:
            raise ValueError("plan comments require a saved plan")
        if self._plan is not None and any(
            comment.step_index > len(self._plan.steps) for comment in normalized
        ):
            raise ValueError("plan comments must refer to saved steps")
        self._plan_comments = normalized

    def plan_runtime_message(self) -> Message | None:
        """Render the current plan as one append-only runtime notice.

        The caller owns placement and must keep this message out of durable
        session history.  Rendering it separately prevents plan revisions
        from invalidating the stable system prefix of prior requests.

        将当前计划渲染为一条仅追加的运行时通知。调用方负责放置该消息并确保其不进入
        持久会话历史,独立渲染可避免计划修订使早先请求的稳定 system 前缀失效。
        """

        if self._plan is None:
            return None
        parts = [
            "Runtime plan update:\n"
            "The following plan supersedes every earlier runtime plan notice.",
            self._plan.model_guidance(),
        ]
        comments = self._plan.comment_guidance(self._plan_comments)
        if comments:
            parts.append(comments)
        return Message(
            Role.USER,
            "\n\n".join(parts),
            synthetic_reason=SyntheticReason.RUNTIME_PLAN,
        )

    @staticmethod
    def budget_runtime_message(usage: ExecutionBudgetUsage) -> Message | None:
        """Render a pressure transition without mutable exact counters.

        Normal execution receives no budget notice.  Higher pressure levels
        are discrete and intentionally contain no remaining-count values, so
        a stable pressure does not rewrite or churn request context.

        渲染不含精确动态计数的预算压力转换。正常执行不发送预算通知,更高压力级别
        使用离散状态,避免稳定压力反复改写或抖动请求上下文。
        """

        if usage.pressure is ExecutionBudgetPressure.NORMAL:
            return None
        return Message(
            Role.USER,
            _budget_runtime_guidance(usage.pressure),
            synthetic_reason=SyntheticReason.RUNTIME_BUDGET,
        )

    @staticmethod
    def segment_runtime_message(checkpoint: ExecutionSegmentCheckpoint) -> Message:
        """Render one immutable segment checkpoint runtime notice.

        渲染一条不可变的分段检查点运行时通知。
        """

        return Message(
            Role.USER,
            _segment_runtime_guidance(checkpoint),
            synthetic_reason=SyntheticReason.RUNTIME_CHECKPOINT,
        )

    @staticmethod
    def supervision_runtime_message(*, resolved: bool = False) -> Message:
        """Render a replan instruction or its append-only resolution notice.

        渲染重新规划指引,或其仅追加的已解决通知。
        """

        content = (
            "Runtime supervision update:\nNew evidence has been recorded after the prior "
            "replan notice. Continue from that evidence without repeating the earlier approach."
            if resolved
            else REPLAN_RUNTIME_GUIDANCE
        )
        return Message(
            Role.USER,
            content,
            synthetic_reason=SyntheticReason.RUNTIME_SUPERVISION,
        )

    @property
    def instruction_result(self) -> InstructionDiscoveryResult | None:
        """Return the most recent instruction discovery result, if any.

        返回最近一次指令发现结果,如果存在."""
        return self._last_instruction_result

    @property
    def skill_result(self) -> SkillDiscoveryResult | None:
        """Return the most recent skill discovery result, if any.

        返回最近一次技能发现结果,如果存在."""
        return self._last_skill_result

    def build(
        self,
        items: Sequence[SessionItem],
        *,
        working_set_message: Message | None = None,
    ) -> tuple[SessionItem, ...]:
        """Apply the selected policy to a request without persisting control text.

        The first applicable instruction and skill snapshots are placed after
        the system message. Later changes and runtime state are inserted at
        their anchored conversation boundary so an earlier request remains a
        prefix. All generated messages stay outside durable history.

        将策略应用于请求但不持久化控制文本. 首份指令与技能快照位于 system 之后; 后续变化按锚点追加, 保留旧请求前缀.
        """

        if working_set_message is not None and (
            not isinstance(working_set_message, Message)
            or working_set_message.synthetic_reason is not SyntheticReason.WORKING_SET
        ):
            raise TypeError("working set message must be canonical synthetic context")

        guidance_parts = [
            reasoning_guidance(self._reasoning_effort),
            interaction_mode_guidance(self._interaction_mode),
            BATCH_FIRST_RUNTIME_GUIDANCE,
        ]
        guidance = "\n\n".join(guidance_parts)
        raw_items = tuple(items)
        if not any(isinstance(item, Message) and item.role is Role.SYSTEM for item in raw_items):
            raw_items = (Message(Role.SYSTEM, ""), *raw_items)
        removable_reasons = _JOURNALLED_RUNTIME_REASONS | frozenset(
            {
                SyntheticReason.PARENT_RELAY,
                SyntheticReason.DAG_PREDECESSOR_RESULTS,
                SyntheticReason.PROJECT_MEMORY_INDEX,
                SyntheticReason.PROJECT_INSTRUCTIONS,
                SyntheticReason.AVAILABLE_SKILLS,
            }
        )
        canonical_anchor = sum(
            not (isinstance(item, Message) and item.synthetic_reason in removable_reasons)
            for item in raw_items
        )

        # Current instructions and catalog are pinned in the stable prefix for
        # this cache epoch. Later discovery changes append a typed revision at
        # the point where it became visible instead of replacing old bytes.
        instruction_result = self._refresh_instructions()
        current_instruction = (
            instruction_result.instruction_message()
            if instruction_result is not None and instruction_result.files
            else None
        )
        if self._instruction_provider is None:
            current_instruction = next(
                (
                    item
                    for item in raw_items
                    if isinstance(item, Message)
                    and item.synthetic_reason is SyntheticReason.PROJECT_INSTRUCTIONS
                ),
                None,
            )
        if self._stable_instruction_message is _UNLOADED_WORKSPACE_SNAPSHOT:
            self._stable_instruction_message = current_instruction
            self._latest_instruction_message = current_instruction
        elif current_instruction != self._latest_instruction_message:
            scope_files = (
                tuple(file.relative_path for file in instruction_result.files)
                if instruction_result is not None
                else ()
            )
            scope_label = ", ".join(scope_files) if scope_files else "none"
            revision = Message(
                Role.USER,
                (
                    "Project instruction scope revision. Current applicable AGENTS.md files "
                    f"(shallow to deep): {scope_label}. Only the files listed in this revision "
                    "apply to the current focus; directory scopes omitted here are no longer "
                    "active. This complete current scope supersedes earlier instruction "
                    "revisions.\n\n"
                    + (
                        current_instruction.content
                        if current_instruction is not None
                        else "No repository instructions are currently applicable."
                    )
                ),
                synthetic_reason=SyntheticReason.INSTRUCTION_SCOPE_REVISION,
            )
            self._projection_journal.append(canonical_anchor, revision)
            self._latest_instruction_message = current_instruction

        skill_result = self._refresh_skills()
        current_skill = (
            skill_result.skill_message()
            if skill_result is not None and skill_result.files
            else None
        )
        if self._skill_provider is None:
            current_skill = next(
                (
                    item
                    for item in raw_items
                    if isinstance(item, Message)
                    and item.synthetic_reason is SyntheticReason.AVAILABLE_SKILLS
                ),
                None,
            )
        if self._stable_skill_message is _UNLOADED_WORKSPACE_SNAPSHOT:
            self._stable_skill_message = current_skill
            self._latest_skill_message = current_skill
        elif current_skill != self._latest_skill_message:
            revision = Message(
                Role.USER,
                (
                    "Available skill catalog revision. This metadata catalog supersedes "
                    "earlier catalog revisions; read a skill body only through its existing "
                    "authorized tool.\n\n"
                    + (
                        current_skill.content
                        if current_skill is not None
                        else "No skills are currently available in this scope."
                    )
                ),
                synthetic_reason=SyntheticReason.SKILL_SCOPE_REVISION,
            )
            self._projection_journal.append(canonical_anchor, revision)
            self._latest_skill_message = current_skill

        # Runtime controls and the Working Set are projection revisions, not
        # canonical transcript entries. Anchor them to the number of canonical
        # items already present so later requests preserve their original order.
        canonical: list[SessionItem] = []
        durable_boundary = 0
        for item in raw_items:
            if isinstance(item, Message) and item.synthetic_reason in _JOURNALLED_RUNTIME_REASONS:
                self._projection_journal.append(durable_boundary, item)
                continue
            if isinstance(item, Message) and item.synthetic_reason in {
                SyntheticReason.PARENT_RELAY,
                SyntheticReason.DAG_PREDECESSOR_RESULTS,
                SyntheticReason.WORKING_SET,
                SyntheticReason.PROJECT_MEMORY_INDEX,
                SyntheticReason.PROJECT_INSTRUCTIONS,
                SyntheticReason.AVAILABLE_SKILLS,
            }:
                continue
            canonical.append(item)
            durable_boundary += 1
        if working_set_message is not None:
            self._projection_journal.append(
                durable_boundary,
                working_set_message,
            )
        rendered = self._projection_journal.project(tuple(canonical))

        # Apply policy guidance to the system message without changing the
        # order of any previously rendered provider-visible conversation item.
        system_index = next(
            index
            for index, item in enumerate(rendered)
            if isinstance(item, Message) and item.role is Role.SYSTEM
        )
        original = rendered[system_index]
        assert isinstance(original, Message)
        rendered_list = list(rendered)
        rendered_list[system_index] = Message(
            Role.SYSTEM,
            f"{original.model_content()}\n\n{guidance}",
        )
        rendered = tuple(rendered_list)

        stable_instruction = self._stable_instruction_message
        stable_skill = self._stable_skill_message
        prefix_context: list[Message] = []
        if isinstance(stable_instruction, Message):
            prefix_context.append(stable_instruction)
        if isinstance(stable_skill, Message):
            prefix_context.append(stable_skill)

        # Project memory is a generation-pinned bounded index of contextual
        # evidence. Extraction may update storage in the background, but that
        # must not rewrite an active request prefix.
        if self._project_memory_snapshot is _UNLOADED_PROJECT_MEMORY_SNAPSHOT:
            self._project_memory_snapshot = self._load_project_memory_snapshot()
        memory_index = self._project_memory_snapshot
        if memory_index:
            assert isinstance(memory_index, str)
            prefix_context.append(
                Message(
                    Role.USER,
                    memory_index,
                    synthetic_reason=SyntheticReason.PROJECT_MEMORY_INDEX,
                )
            )

        # The immutable parent relay is context rather than authority. Insert
        # its single owned copy after stable workspace context and before
        # genuine child history on every request.
        if self._parent_relay_message is not None:
            prefix_context.append(self._parent_relay_message)

        # The dependency relay is a separate channel from parent context.  Its
        # canonical copy is owned by the application and replaces any caller-
        # supplied synthetic message removed above.
        if self._dag_result_relay_message is not None:
            prefix_context.append(self._dag_result_relay_message)

        return (
            *rendered[: system_index + 1],
            *prefix_context,
            *rendered[system_index + 1 :],
        )

    def _refresh_instructions(self) -> InstructionDiscoveryResult | None:
        """Refresh observable discovery state before each request."""
        if self._instruction_provider is None:
            self._last_instruction_result = None
            return None
        self._last_instruction_result = self._instruction_provider()
        return self._last_instruction_result

    def _refresh_skills(self) -> SkillDiscoveryResult | None:
        """Refresh observable discovery state before each request."""
        if self._skill_provider is None:
            self._last_skill_result = None
            return None
        self._last_skill_result = self._skill_provider()
        return self._last_skill_result


__all__ = [
    "BATCH_FIRST_RUNTIME_GUIDANCE",
    "REPLAN_RUNTIME_GUIDANCE",
    "ContextBuilder",
]
