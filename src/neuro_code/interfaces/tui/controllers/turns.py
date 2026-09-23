from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from textual.containers import Horizontal
from textual.widgets import Button

from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.sessions.attachments import (
    MAX_IMAGE_ATTACHMENT_BYTES,
    Attachment,
    AttachmentError,
    build_attachments,
    compose_turn_input,
)
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.application.workflows.plan_execution import (
    ExecutePlanRequest,
)
from neuro_code.application.workflows.session_task_execution import (
    RunSessionTaskRequest,
)
from neuro_code.domain.conversation.context import estimate_context_tokens, estimate_text_tokens
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.messages import ContentPart
from neuro_code.domain.execution import (
    AgentExecutionStatus,
    SupervisorReasonCode,
    TurnCancellationPolicy,
)
from neuro_code.domain.plans import SessionPlan
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.execution import (
    BudgetUsageProjection,
    budget_limited_detail,
    recoverable_execution_reason,
    recoverable_terminal_status,
)
from neuro_code.interfaces.tui.state import (
    _MAX_QUEUED_INTERJECTIONS,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.widgets import PromptInput
from neuro_code.shared.errors import ProviderError
from neuro_code.shared.redaction import redact_sensitive_text

_BUDGET_REASON_TEXT_KEYS = {
    SupervisorReasonCode.MODEL_CALL_RESERVE: "turn.budget_reason.model_call_reserve",
    SupervisorReasonCode.MODEL_CALL_BUDGET: "turn.budget_reason.model_call_budget",
    SupervisorReasonCode.MODEL_STEP_LIMIT: "turn.budget_reason.model_step_limit",
    SupervisorReasonCode.TOOL_ROUND_BUDGET: "turn.budget_reason.tool_round_budget",
    SupervisorReasonCode.TOOL_CALL_BUDGET: "turn.budget_reason.tool_call_budget",
    SupervisorReasonCode.PER_TOOL_CALL_BUDGET: "turn.budget_reason.per_tool_call_budget",
    SupervisorReasonCode.WALL_TIME_BUDGET: "turn.budget_reason.wall_time_budget",
    SupervisorReasonCode.INPUT_TOKEN_BUDGET: "turn.budget_reason.input_token_budget",
    SupervisorReasonCode.OUTPUT_TOKEN_BUDGET: "turn.budget_reason.output_token_budget",
    SupervisorReasonCode.TOTAL_TOKEN_BUDGET: "turn.budget_reason.total_token_budget",
    SupervisorReasonCode.CONTEXT_WINDOW_BUDGET: "turn.budget_reason.context_window_budget",
}
_STUCK_REASON_TEXT_KEYS = {
    SupervisorReasonCode.REPEATED_ACTION_OBSERVATION: "turn.stuck_repeated_observation",
    SupervisorReasonCode.REPEATED_ACTION_ERROR: "turn.stuck_repeated_error",
    SupervisorReasonCode.PERIODIC_CYCLE: "turn.stuck_periodic_cycle",
    SupervisorReasonCode.NO_PROGRESS: "turn.stuck_no_progress",
    SupervisorReasonCode.WEB_SEARCH_UNAVAILABLE: "turn.stuck_web_search_unavailable",
}


def _existing_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Filter one attachment snapshot down to the paths still present.

    将附件快照过滤为仍然存在的路径."""

    return tuple(path for path in paths if os.path.exists(path))


def _human_size(size_bytes: int) -> str:
    """Render a bounded human-readable attachment size.

    渲染易读的附件大小."""

    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MiB"
    return f"{max(1, size_bytes // 1024)} KiB"


class TurnControllerMixin(TuiAppControllerMixin):
    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        prompt = event.value.strip()
        event.input.value = ""
        if not prompt and not self._pending_attachment_paths:
            return
        if self._pending_interaction_request_id is not None and self._user_interaction is not None:
            request_id = self._pending_interaction_request_id
            self._pending_interaction_request_id = None
            self._user_interaction.resolve(request_id, prompt)
            self._write_ui_entry("status", "interaction.submitted")
            return
        if prompt.startswith("/"):
            await self._dispatch_slash_command(prompt)
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            if not self._first_token_seen and self._pending_assistant is not None:
                if not self._queue_interjection(prompt):
                    event.input.value = prompt
                    event.input.cursor_position = len(prompt)
            else:
                self._write_ui_entry("error", "turn.running")
            return

        self._submit_prompt(prompt)

    def _submit_prompt(self, prompt: str) -> None:
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        attachments: tuple[Attachment, ...] = ()
        if self._pending_attachment_paths:
            try:
                attachments = build_attachments(
                    self._pending_attachment_paths,
                    workspace=self._cwd,
                )
            except AttachmentError as error:
                self._write_ui_entry("error", "attachment.invalid", reason=str(error))
                self._restore_prompt_draft(prompt)
                return
        composed_prompt, content_parts = compose_turn_input(prompt, attachments)
        self._active_prompt = prompt
        self._active_prompt_entry_index = len(self._entries)
        self._ultracode_decision = None
        self._turn_pristine_rewound = False
        self._write_entry("user", self._entry_text_with_attachments(prompt, attachments))
        self._context_used_tokens += 4 + estimate_text_tokens(composed_prompt)
        self._context_usage_estimated = True
        self._context_preflight_status = None
        self._context_preflight_capacity_tokens = None
        self._context_preflight_total_tokens = False
        self._context_preflight_notice = None
        self._refresh_runtime_bar()
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._terminal_execution_reason = None
        self._terminal_budget_usage = None
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        # Remember what was submitted so a pristine rewind can re-queue it.  The
        # owned clipboard temp files stay alive until the turn is durable, because
        # a rewind would otherwise lose the only copy of a pasted image.
        #
        # 记住已提交的内容,以便原始回滚可以重新排队.拥有的剪贴板临时文件会保留到回合
        # 持久化为止,否则回滚会丢失粘贴图片的唯一副本.
        self._submitted_attachment_paths = tuple(self._pending_attachment_paths)
        self._clear_attachments()
        self._turn_worker = self.run_worker(
            self._run_prompt(composed_prompt, content_parts),
            name="agent-turn",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    @staticmethod
    def _entry_text_with_attachments(prompt: str, attachments: tuple[Attachment, ...]) -> str:
        """Render the user entry with one attachment marker per file.

        在用户条目中为每个附件渲染一个标记."""

        if not attachments:
            return prompt
        markers = "\n".join(
            f"📎 {attachment.path.name} · {_human_size(attachment.size_bytes)}"
            for attachment in attachments
        )
        return f"{prompt}\n{markers}" if prompt else markers

    def _restore_prompt_draft(self, prompt: str) -> None:
        """Return a rejected submission to the composer so nothing is lost.

        被拒绝的提交返回输入框,避免内容丢失."""

        prompt_widget = self._main_screen_query_one("#prompt", PromptInput)
        prompt_widget.value = prompt
        prompt_widget.cursor_position = len(prompt_widget.value)
        prompt_widget.focus()

    async def _add_attachments(self, paths: Sequence[str]) -> bool:
        """Validate and queue attachments for the next message.

        Returns ``True`` only when every path was accepted, so a caller that owns
        a temporary resource can report failure and release it.  A rejected
        attachment never produces a success notice.

        校验并暂存附件.仅当全部路径被接受时返回 ``True``,以便拥有临时资源的调用方
        报告失败并释放资源;被拒绝的附件绝不产生成功提示.
        """

        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return False
        combined = (*self._pending_attachment_paths, *paths)
        try:
            attachments = build_attachments(combined, workspace=self._cwd)
        except AttachmentError as error:
            self._write_ui_entry("error", "attachment.invalid", reason=str(error))
            return False
        self._pending_attachment_paths = combined
        self._pending_attachments = attachments
        await self._refresh_attachment_tray()
        return True

    def _release_clipboard_temp(self, path: Path) -> None:
        """Delete one TUI-owned clipboard temp file, idempotently and quietly.

        Only resources this interface created are ever removed; user-supplied
        attachment paths are never tracked, so they are never deleted.  Cleanup
        errors are swallowed so they cannot mask the primary user-visible error.

        幂等且静默地删除一个本界面拥有的剪贴板临时文件.只有本界面创建的资源会被删除;
        用户提供的附件路径从不被跟踪,因此永不被删除.清理错误被吞掉,避免掩盖主要错误.
        """

        owned = self._clipboard_temp_paths
        if path not in owned:
            return
        owned.discard(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def _release_clipboard_resources(self) -> None:
        """Release every clipboard temp file this interface still owns.

        释放本界面仍拥有的全部剪贴板临时文件."""

        for path in tuple(self._clipboard_temp_paths):
            self._release_clipboard_temp(path)

    def _clear_attachments(self) -> None:
        self._pending_attachment_paths = ()
        self._pending_attachments = ()
        self.run_worker(
            self._refresh_attachment_tray(),
            name="attachment-tray",
            exclusive=True,
        )

    async def _refresh_attachment_tray(self) -> None:
        tray = self._main_screen_query_optional("#attachment-tray", Horizontal)
        if tray is None:
            return
        await tray.remove_children([child for child in tray.children if isinstance(child, Button)])
        if not self._pending_attachments:
            tray.display = False
            return
        tray.display = True
        await tray.mount_all(
            [
                Button(
                    f"✕ {attachment.path.name} · {_human_size(attachment.size_bytes)}",
                    id=f"attachment-remove-{index}",
                )
                for index, attachment in enumerate(self._pending_attachments)
            ]
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("attachment-remove-"):
            return
        event.stop()
        index = button_id.removeprefix("attachment-remove-")
        if not index.isdigit() or int(index) >= len(self._pending_attachment_paths):
            return
        position = int(index)
        paths = list(self._pending_attachment_paths)
        removed_path = paths[position]
        del paths[position]
        self._pending_attachment_paths = tuple(paths)
        self._release_clipboard_temp(Path(removed_path))
        self._pending_attachments = tuple(
            attachment
            for position_, attachment in enumerate(self._pending_attachments)
            if position_ != position
        )
        self.run_worker(
            self._refresh_attachment_tray(),
            name="attachment-tray",
            exclusive=True,
        )

    async def on_prompt_input_image_paste_requested(
        self,
        event: PromptInput.ImagePasteRequested,
    ) -> None:
        """Attach the system clipboard image, when one is available.

        在可用时附加系统剪贴板图片."""

        event.stop()
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        image = self._clipboard_image_reader.read_image()
        if image is None:
            # No image on the system clipboard: fall back to TextArea's text
            # paste so ordinary copying keeps working unchanged.
            #
            # 系统剪贴板没有图片:回落到 TextArea 的文本粘贴,普通复制粘贴不受影响.
            self._write_ui_entry("status", "clipboard.image_unavailable")
            await event.input.run_action("paste")
            return
        if len(image.data) > MAX_IMAGE_ATTACHMENT_BYTES:
            # Reject before creating any retained temporary resource.
            #
            # 在创建任何保留的临时资源之前先拒绝超限负载.
            self._write_ui_entry(
                "error",
                "attachment.invalid",
                reason=(
                    f"image attachment is too large: clipboard "
                    f"({len(image.data)} > {MAX_IMAGE_ATTACHMENT_BYTES} bytes)"
                ),
            )
            return
        handle, raw_path = tempfile.mkstemp(prefix="clipboard-", suffix=".png")
        path = Path(raw_path)
        self._clipboard_temp_paths.add(path)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(image.data)
        except OSError as error:
            self._release_clipboard_temp(path)
            self._write_ui_entry("error", "attachment.invalid", reason=str(error))
            return
        if not await self._add_attachments([str(path)]):
            # A rejected attachment must never be followed by a success notice.
            #
            # 被拒绝的附件之后绝不能出现成功提示.
            self._release_clipboard_temp(path)
            return
        self._write_ui_entry(
            "status",
            "clipboard.image_attached",
            name=path.name,
            size=_human_size(len(image.data)),
        )

    def _queue_interjection(self, prompt: str) -> bool:
        if len(self._queued_interjections) >= _MAX_QUEUED_INTERJECTIONS:
            self._write_ui_entry("error", "turn.interjection_limit")
            return False
        self._queued_interjections.append(prompt)
        self._write_ui_entry("status", "turn.interjection_queued")
        return True

    def _start_next_interjection(self) -> None:
        if (
            self._turn_worker is not None and self._turn_worker.is_running
        ) or not self._queued_interjections:
            return
        self._submit_prompt(self._queued_interjections.popleft())

    def _restore_queued_interjections(self) -> None:
        """Return every unsent interjection to the draft without auto-submitting it.

        将所有未发送的插话放回草稿,不自动提交."""

        if not self._queued_interjections:
            return
        queued = tuple(self._queued_interjections)
        self._queued_interjections.clear()
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        prompt.value = "\n\n".join((*queued, prompt.value)) if prompt.value else "\n\n".join(queued)
        prompt.cursor_position = len(prompt.value)
        self._write_ui_entry("status", "turn.interjections_restored", count=len(queued))

    async def _restore_submitted_attachments(self) -> None:
        """Re-queue the attachments of a rewound turn so nothing is lost.

        将被回滚回合的附件重新排队,避免丢失."""

        paths = self._submitted_attachment_paths
        self._submitted_attachment_paths = ()
        if not paths or self._pending_attachment_paths:
            return
        existing = _existing_paths(paths)
        if not existing:
            return
        try:
            attachments = build_attachments(existing, workspace=self._cwd)
        except AttachmentError:
            return
        self._pending_attachment_paths = existing
        self._pending_attachments = attachments
        await self._refresh_attachment_tray()

    def _finalize_attachment_resources(self) -> None:
        """Release owned clipboard temp files once a turn no longer needs them.

        A rewound turn deliberately keeps them so the restored draft can be
        resent with its image intact.

        回合不再需要时释放拥有的剪贴板临时文件;被回滚的回合刻意保留它们,以便恢复的
        草稿可以连同图片一起重新发送.
        """

        if self._turn_pristine_rewound:
            return
        self._submitted_attachment_paths = ()
        self._release_clipboard_resources()

    async def _restore_pristine_prompt(self) -> None:
        prompt_text = self._active_prompt
        if not prompt_text:
            return
        await self._restore_submitted_attachments()
        entry_index = self._active_prompt_entry_index
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        if not prompt.value:
            if entry_index is not None and 0 <= entry_index < len(self._entries):
                entry = self._entries[entry_index]
                if entry.category == "user" and entry.text == prompt_text:
                    await self._remove_transcript_entry(entry_index)
            prompt.value = prompt_text
            prompt.cursor_position = len(prompt_text)
            self._write_ui_entry("status", "turn.draft_restored")
            return
        self._write_ui_entry("status", "turn.draft_preserved")

    async def _run_prompt(
        self,
        prompt: str,
        content_parts: Sequence[ContentPart] = (),
    ) -> None:
        parts = tuple(content_parts)
        turn_service = self._turn_service
        if turn_service is not None:
            request = RunTurnRequest(
                prompt,
                content_parts=parts,
                cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
                expected_session_id=self._runner.session_id,
            )
            await self._run_agent_turn(
                lambda: turn_service.run_turn(request, sink=self._handle_event)
            )
            self._finalize_attachment_resources()
            return
        await self._run_agent_turn(
            lambda: self._runner.run(
                prompt,
                sink=self._handle_event,
                cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
                # Only forwarded when present so lightweight runner test
                # doubles that predate attachments keep working unchanged.
                # 仅在存在时传递,避免影响早期的轻量 runner 测试替身.
                **({"content_parts": content_parts} if content_parts else {}),
            )
        )
        self._finalize_attachment_resources()

    async def _run_background_wake(self) -> None:
        await self._run_agent_turn(
            lambda: self._runner.run_background_wake(sink=self._handle_event)
        )

    async def _run_plan_execution(self) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        service = self._plan_execution_service
        if service is not None:
            await self._run_agent_turn(
                lambda: service.execute_plan(
                    ExecutePlanRequest(),
                    sink=self._handle_event,
                )
            )
            return
        await self._run_agent_turn(lambda: controller.execute_plan(sink=self._handle_event))

    async def _run_queued_plan(self, task_id: str) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        service = self._queued_plan_execution_service
        if service is not None:
            await self._run_agent_turn(
                lambda: service.run_session_task(
                    RunSessionTaskRequest(task_id),
                    sink=self._handle_event,
                )
            )
            return
        await self._run_agent_turn(
            lambda: controller.run_session_task(task_id, sink=self._handle_event)
        )

    async def _run_agent_turn(
        self,
        run: Callable[[], Awaitable[AgentRunResult]],
    ) -> None:
        prompt_input = self._main_screen_query_one("#prompt", PromptInput)
        completed = False
        try:
            result = await run()
            if self._background_wake_active:
                await self._complete_background_wake()
            completed = True
            response = result.response or ui_text(self._language, "turn.no_response")
            if not self._turn_usage_reported:
                self._context_used_tokens = (
                    estimate_context_tokens(result.items)
                    if result.items
                    else self._context_used_tokens + 4 + estimate_text_tokens(response)
                )
                self._context_usage_estimated = True
                self._refresh_runtime_bar()
            self._finish_streamed_assistant_response(result, fallback=response)
            if self._terminal_execution_recoverable and self._terminal_execution_status is not None:
                if (
                    self._terminal_execution_status == AgentExecutionStatus.BUDGET_LIMITED.value
                    and self._terminal_execution_reason in _BUDGET_REASON_TEXT_KEYS
                ):
                    assert self._terminal_execution_reason is not None
                    reason_key = _BUDGET_REASON_TEXT_KEYS[self._terminal_execution_reason]
                    reason = ui_text(self._language, reason_key)
                    usage = (
                        self._terminal_budget_usage.for_reason(self._terminal_execution_reason)
                        if self._terminal_budget_usage is not None
                        else None
                    )
                    detail = (
                        self._terminal_budget_usage.per_tool
                        if self._terminal_budget_usage is not None
                        else None
                    )
                    if usage is None and detail is not None:
                        self._write_ui_entry(
                            "recoverable",
                            "turn.budget_limited_per_tool",
                            tool=detail.tool_name,
                            used=detail.used,
                            limit=detail.limit,
                        )
                    elif usage is None:
                        self._write_ui_entry(
                            "recoverable",
                            "turn.budget_limited_reason",
                            reason=reason,
                        )
                    else:
                        self._write_ui_entry(
                            "recoverable",
                            "turn.budget_limited_reason_usage",
                            reason=reason,
                            used=usage[0],
                            limit=usage[1],
                        )
                elif (
                    self._terminal_execution_status == AgentExecutionStatus.STUCK.value
                    and self._terminal_execution_reason in _STUCK_REASON_TEXT_KEYS
                ):
                    assert self._terminal_execution_reason is not None
                    self._write_ui_entry(
                        "recoverable",
                        _STUCK_REASON_TEXT_KEYS[self._terminal_execution_reason],
                    )
                else:
                    self._write_ui_entry(
                        "recoverable",
                        f"turn.{self._terminal_execution_status}_recoverable",
                    )
            elif self._turn_completion is not None:
                duration, steps = self._turn_completion
                self._write_ui_entry(
                    "status",
                    "turn.completed",
                    duration=duration,
                    steps=steps,
                )
            if self._agent_preferences.notify_completed is True:
                self.bell()
        except asyncio.CancelledError:
            await self._discard_pending_assistant()
            if self._turn_pristine_rewound:
                await self._restore_pristine_prompt()
            self._restore_queued_interjections()
            self._write_ui_entry("status", "turn.cancelled")
            raise
        except Exception as error:
            await self._discard_pending_assistant()
            self._restore_queued_interjections()
            self._write_turn_failure(error)
            if self._agent_preferences.notify_failed is True:
                self.bell()
        finally:
            self._pending_interaction_request_id = None
            if self._background_wake_active:
                self._background_wake_state = self._background_wake_state.abandon_wake(
                    failed_at=datetime.now(UTC)
                )
                self._background_wake_active = False
                self._background_wake_task_ids = ()
                await self._persist_background_wake_state()
            self._stop_model_loading()
            prompt_input.focus()
            if completed and self._queued_interjections:
                self.call_after_refresh(self._start_next_interjection)
            if completed or self._turn_pristine_rewound or self._active_prompt is not None:
                self._active_prompt = None
                self._active_prompt_entry_index = None

    def _write_turn_failure(self, error: Exception) -> None:
        """Render a failed turn without implying that its durable session was lost.

        将失败回合显示为可恢复状态,避免暗示其持久化会话已经丢失.
        """

        if isinstance(error, ProviderError):
            key = (
                "turn.provider_balance_recoverable"
                if self._provider_balance_is_insufficient(error)
                else "turn.provider_failure_recoverable"
            )
            self._write_ui_entry("recoverable", key)
            return
        detail = redact_sensitive_text(str(error))
        self._write_entry("error", f"{type(error).__name__}: {detail}")

    @staticmethod
    def _provider_balance_is_insufficient(error: ProviderError) -> bool:
        """Recognize the actionable payment failure without parsing provider payloads.

        识别可操作的付款失败,但不解析或暴露 Provider 原始载荷.
        """

        return error.failure.status_code == 402

    async def _handle_event(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind is AgentEventKind.USER_INPUT_REQUESTED:
            request_id = data.get("request_id")
            question = data.get("question")
            if isinstance(request_id, str) and isinstance(question, str):
                self._pending_interaction_request_id = request_id
                self._turn_activity_kind = "waiting_input"
                self._turn_activity_started_at = monotonic()
                self._refresh_turn_activity()
                options = data.get("options")
                lines = [question]
                if isinstance(options, Sequence) and not isinstance(options, str | bytes):
                    for index, option in enumerate(options, start=1):
                        if isinstance(option, Mapping) and isinstance(option.get("label"), str):
                            lines.append(f"{index}. {option['label']}")
                self._write_entry("status", "\n".join(lines))
        elif event.kind is AgentEventKind.USER_INPUT_RESOLVED:
            self._pending_interaction_request_id = None
            self._turn_activity_kind = "continuing"
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.MODEL_STEP_STARTED:
            self._seal_pending_assistant()
            self._turn_activity_kind = "model"
            self._turn_activity_tool_name = None
            self._turn_activity_tool_started_at = None
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.TEXT_DELTA:
            text = data.get("text")
            if isinstance(text, str):
                self._finalizing = False
                if text:
                    self._active_tool_activity_group = None
                    self._first_token_seen = True
                    self._turn_activity_kind = "responding"
                    self._turn_activity_tool_name = None
                    self._turn_activity_tool_started_at = None
                self._assistant_parts.append(text)
                self._update_pending_assistant("".join(self._assistant_parts))
                self._refresh_turn_activity()
        elif event.kind is AgentEventKind.FINALIZING_STARTED:
            self._finalizing = True
            self._turn_activity_kind = "finalizing"
            self._turn_activity_tool_name = None
            self._turn_activity_tool_started_at = None
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.ULTRACODE_DELEGATION_PROGRESS:
            decision = data.get("decision")
            state = data.get("state")
            if isinstance(decision, str) and isinstance(state, str):
                self._record_ultracode_decision(decision)
                self._turn_activity_kind = "orchestrating"
                self._turn_activity_tool_name = None
                self._turn_activity_tool_started_at = None
                self._refresh_turn_activity()
                self._write_entry("status", f"Ultracode {decision} · {state}")
        elif event.kind is AgentEventKind.REASONING_DELTA:
            text = data.get("text")
            if isinstance(text, str) and text:
                self._first_token_seen = True
                self._turn_activity_kind = "reasoning"
                self._refresh_turn_activity()
        elif event.kind is AgentEventKind.MODEL_THINKING_COMPLETED:
            self._turn_activity_kind = "continuing"
            self._refresh_turn_activity()
        elif event.kind is AgentEventKind.CONTEXT_USAGE_UPDATED:
            used_tokens = data.get("used_tokens")
            if isinstance(used_tokens, int) and not isinstance(used_tokens, bool):
                self._context_used_tokens = max(0, used_tokens)
                self._context_usage_estimated = data.get("estimated") is not False
                self._context_preflight_status = None
                self._context_preflight_capacity_tokens = None
                self._context_preflight_total_tokens = False
                self._context_preflight_notice = None
                self._turn_usage_reported = not self._context_usage_estimated
                self._refresh_runtime_bar()
        elif event.kind is AgentEventKind.EXECUTION_BUDGET_UPDATED:
            usage = BudgetUsageProjection.from_event_data(data)
            if usage is not None:
                self._terminal_budget_usage = usage
        elif event.kind is AgentEventKind.CONTEXT_PREFLIGHT:
            status = data.get("status")
            if isinstance(status, str):
                self._context_preflight_status = status
            capacity_tokens = data.get("capacity_tokens")
            self._context_preflight_capacity_tokens = (
                capacity_tokens
                if isinstance(capacity_tokens, int)
                and not isinstance(capacity_tokens, bool)
                and capacity_tokens > 0
                else None
            )
            estimated_input_tokens = data.get("estimated_input_tokens")
            if isinstance(estimated_input_tokens, int) and not isinstance(
                estimated_input_tokens,
                bool,
            ):
                estimated_total_tokens = data.get("estimated_total_tokens")
                if (
                    isinstance(estimated_total_tokens, int)
                    and not isinstance(
                        estimated_total_tokens,
                        bool,
                    )
                    and estimated_total_tokens >= 0
                ):
                    self._context_used_tokens = max(0, estimated_total_tokens)
                    has_request_total = True
                else:
                    self._context_used_tokens = max(0, estimated_input_tokens)
                    has_request_total = False
                self._context_usage_estimated = True
                self._context_preflight_total_tokens = has_request_total
                self._turn_usage_reported = False
                self._refresh_runtime_bar()
            if isinstance(status, str):
                notice_key = {
                    "compaction_required": "context.preflight.compaction_required",
                    "blocked": "context.preflight.blocked",
                    "unknown": "context.preflight.unknown",
                }.get(status)
                if notice_key != self._context_preflight_notice:
                    if notice_key is not None:
                        self._write_ui_entry("status", notice_key)
                    self._context_preflight_notice = notice_key
        elif event.kind is AgentEventKind.CONTEXT_COMPACTION_COMPLETED:
            self._write_ui_entry("status", "context.compaction_result", status="completed")
        elif event.kind is AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER:
            raw_task_ids = data.get("task_ids")
            if (
                self._background_wake_active
                and isinstance(raw_task_ids, Sequence)
                and not isinstance(raw_task_ids, str | bytes)
            ):
                task_ids = tuple(task_id for task_id in raw_task_ids if isinstance(task_id, str))
                self._background_wake_task_ids = task_ids
        elif event.kind is AgentEventKind.PROVIDER_ATTEMPT_FAILED:
            provider = self._field(data, "provider")
            message = self._field(data, "message")
            self._write_ui_entry(
                "error",
                "provider.failed",
                provider=provider,
                message=message,
            )
        elif event.kind is AgentEventKind.PROVIDER_SELECTED:
            provider = self._field(data, "provider")
            model = self._field(data, "model")
            self._provider_name = provider
            self._model_name = model
            context_window_tokens = data.get("context_window_tokens")
            self._context_window_tokens = (
                context_window_tokens
                if isinstance(context_window_tokens, int)
                and not isinstance(context_window_tokens, bool)
                and context_window_tokens > 0
                else None
            )
            self._context_preflight_status = None
            self._context_preflight_capacity_tokens = None
            self._context_preflight_total_tokens = False
            self._context_preflight_notice = None
            self._refresh_runtime_bar()
            key = (
                "provider.fallback_selected"
                if data.get("failover") is True
                else "provider.selected"
            )
            self._write_ui_entry("status", key, provider=provider, model=model)
        elif event.kind in {
            AgentEventKind.BACKEND_TOOL_STARTED,
            AgentEventKind.BACKEND_TOOL_COMPLETED,
            AgentEventKind.TOOL_REQUESTED,
            AgentEventKind.TOOL_PERMISSION,
            AgentEventKind.TOOL_APPROVAL_REQUESTED,
            AgentEventKind.TOOL_APPROVAL_RESOLVED,
            AgentEventKind.TOOL_STARTED,
            AgentEventKind.TOOL_COMPLETED,
            AgentEventKind.TOOL_FAILED,
        }:
            if event.kind in {
                AgentEventKind.BACKEND_TOOL_STARTED,
                AgentEventKind.TOOL_REQUESTED,
            }:
                self._seal_pending_assistant()
            self._handle_tool_feedback_event(event)
        elif event.kind is AgentEventKind.PLAN_UPDATED:
            try:
                self._plan = SessionPlan.from_dict(data)
            except ValueError:
                return
            self._plan_comments = ()
            self._upsert_plan_entry(self._plan)
        elif event.kind is AgentEventKind.PLAN_EXECUTION_REQUESTED:
            self._write_ui_entry("status", "plan.execution_requested")
        elif event.kind is AgentEventKind.TURN_COMPLETED:
            self._finalizing = False
            self._turn_activity_kind = "completed"
            self._refresh_turn_activity()
            self._turn_completion = (
                self._event_duration(data),
                self._positive_int(data.get("step"), fallback=1),
            )
            execution_status = recoverable_terminal_status(data)
            if execution_status is not None:
                self._terminal_execution_status = execution_status.value
                self._terminal_execution_recoverable = True
                self._terminal_execution_reason = recoverable_execution_reason(data)
                if self._terminal_budget_usage is None:
                    self._terminal_budget_usage = BudgetUsageProjection.from_event_data(data)
                else:
                    detail = budget_limited_detail(data)
                    if detail is not None:
                        self._terminal_budget_usage = replace(
                            self._terminal_budget_usage, per_tool=detail
                        )
            else:
                self._terminal_execution_status = None
                self._terminal_execution_recoverable = False
                self._terminal_execution_reason = None
                self._terminal_budget_usage = None
        elif event.kind is AgentEventKind.TURN_FAILED:
            self._turn_activity_kind = "failed"
            self._refresh_turn_activity()
            self._turn_pristine_rewound = data.get("pristine_rewound") is True
