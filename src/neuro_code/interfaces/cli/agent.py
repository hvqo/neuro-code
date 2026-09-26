"""Headless Agent command execution for the CLI.

CLI 无头 Agent 命令的执行边界.

The command owns CLI stream projection and resource cleanup for one headless
turn.  Runtime behavior and provider construction remain application and
bootstrap responsibilities.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import cast

from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.attachments import (
    AttachmentError,
    build_attachments,
    compose_turn_input,
)
from neuro_code.application.sessions.service import ResumeSessionRequest
from neuro_code.application.sessions.turns import RunTurnRequest
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.cli.contracts import CliServices
from neuro_code.interfaces.cli.interaction import CliUserInteraction
from neuro_code.interfaces.cli.serialization import serialize_execution_outcome
from neuro_code.interfaces.cli.settings import _application_settings
from neuro_code.shared.errors import ConfigurationError

_EPHEMERAL_TRACE_EVENTS = frozenset(
    {
        AgentEventKind.RUNTIME_TRACE_MODEL_REQUEST,
        AgentEventKind.RUNTIME_TRACE_CONTEXT_BUILD,
        AgentEventKind.RUNTIME_TRACE_CONTEXT_ROLLOVER,
        AgentEventKind.RUNTIME_TRACE_REPLAN,
        AgentEventKind.RUNTIME_TRACE_VERIFICATION,
        AgentEventKind.RUNTIME_TRACE_FINALIZER,
        AgentEventKind.RUNTIME_TRACE_SUBAGENT,
    }
)


def _is_cli_json_event(event: AgentEvent) -> bool:
    return (
        event.kind is not AgentEventKind.MODEL_REQUEST_SNAPSHOT
        and event.kind not in _EPHEMERAL_TRACE_EVENTS
    )


async def run_agent(args: argparse.Namespace, services: CliServices) -> int:
    """Run one bounded headless Agent turn and project its result."""
    if not args.prompt:
        raise ConfigurationError(
            "the agent subcommand requires -p/--single; run neuro without a subcommand "
            "for the interactive TUI"
        )
    application = await services.open_application(_application_settings(args))
    try:
        if args.resume is not None:
            await application.session_service.prepare_resume(ResumeSessionRequest(args.resume))
        binding = await application.create_binding(
            resume_id=args.resume,
            user_interaction=CliUserInteraction(interactive=args.output_format == "plain"),
            enable_local_attached_terminals=True,
        )
        last_context_notice: str | None = None
        context_notice_text = {
            "compaction_required": "Context pressure detected; compacting before continuing.",
            "blocked": "The context request exceeds the available budget; the turn stopped safely.",
            "unknown": "Provider context capacity is unknown; continuing without a numeric safety check.",
        }

        async def stream_event(event: AgentEvent) -> None:
            nonlocal last_context_notice
            if args.output_format == "plain" and event.kind is AgentEventKind.TEXT_DELTA:
                text = event.data.get("text")
                if isinstance(text, str):
                    print(text, end="", flush=True)
            elif args.output_format == "plain" and event.kind is AgentEventKind.CONTEXT_PREFLIGHT:
                status = event.data.get("status")
                notice = context_notice_text.get(status) if isinstance(status, str) else None
                if notice != last_context_notice:
                    if notice is not None:
                        print(notice, file=sys.stderr, flush=True)
                    last_context_notice = notice
            elif (
                args.output_format == "plain"
                and event.kind is AgentEventKind.CONTEXT_COMPACTION_COMPLETED
                and last_context_notice != "compacted"
            ):
                print("Context compacted before continuing.", file=sys.stderr, flush=True)
                last_context_notice = "compacted"
            elif args.output_format == "jsonl":
                if _is_cli_json_event(event):
                    print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)

        async def ultracode_delegate(
            request: RunTurnRequest,
            sink: EventSink | None,
        ) -> AgentRunResult:
            service = await application.create_ultracode_delegation_service(
                parent_binding=binding,
            )
            return cast(AgentRunResult, await service.run_turn(request, sink=sink))

        if getattr(binding.runner, "reasoning_effort", None) is ReasoningEffort.ULTRACODE:
            turn_service = application.session_service.bind_runner(
                binding.runner,
                ultracode_delegate=ultracode_delegate,
            )
        else:
            turn_service = application.session_service.bind_runner(binding.runner)
        try:
            attachments = build_attachments(
                args.attach or (),
                workspace=Path(args.cwd) if args.cwd else Path.cwd(),
            )
        except AttachmentError as error:
            raise ConfigurationError(f"invalid attachment: {error}") from error
        composed_prompt, content_parts = compose_turn_input(args.prompt, attachments)
        result = await turn_service.run_turn(
            RunTurnRequest(
                composed_prompt,
                content_parts=content_parts,
                expected_session_id=args.resume,
            ),
            sink=stream_event,
        )
        if args.output_format == "plain":
            print()
        elif args.output_format == "json":
            print(
                json.dumps(
                    {
                        "session_id": result.session_id,
                        "response": result.response,
                        "steps": result.steps,
                        "events": [
                            event.to_dict() for event in result.events if _is_cli_json_event(event)
                        ],
                        "outcome": serialize_execution_outcome(result.outcome),
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    finally:
        await asyncio.shield(application.close())


__all__ = ["run_agent"]
