"""Scoped trusted observations around the existing provider/tool boundaries.

Absent by default. This is composition isolation, not an OS security boundary.
No caller-supplied monetary receipt is accepted; cancellation leaves an intent
without an ACK, which is deliberately not proof of completed execution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from neuro_code.application.ports.model import ModelProvider, ModelToolPolicy
from neuro_code.application.ports.workflow_repair import RepairExecutionObserver
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    ModelBackendToolStarted,
    ModelCompleted,
    ModelEvent,
)
from neuro_code.domain.tools import ToolDefinition

_observer: ContextVar[RepairExecutionObserver | None] = ContextVar("repair_observer", default=None)


def current_observer() -> RepairExecutionObserver | None:
    return _observer.get()


@contextmanager
def observe_execution(observer: RepairExecutionObserver) -> Iterator[None]:
    if _observer.get() is not None:
        raise RuntimeError("nested REPAIR execution is forbidden")
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


async def observed_model_stream(
    provider: ModelProvider,
    context: ModelContext,
    tools: Sequence[ToolDefinition],
    *,
    tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
) -> AsyncIterator[ModelEvent]:
    observer = current_observer()
    if (
        observer is not None
        and getattr(provider, "execution_observation_passthrough", False) is True
    ):
        async for event in provider.stream(context, tools, tool_policy=tool_policy):
            yield event
        return
    if observer is None:
        # Preserve the ordinary provider call shape (some legacy test adapters
        # intentionally do not implement the optional keyword).
        stream = (
            provider.stream(context, tools)
            if tool_policy is ModelToolPolicy.ALLOWED
            else provider.stream(context, tools, tool_policy=tool_policy)
        )
        async for event in stream:
            yield event
        return
    ordinal = await observer.enter(
        "model", context.request_source.value, provider.provider_name + "/" + provider.model_name
    )
    completion: ModelCompleted | None = None
    tool_unknown = False
    try:
        async for event in provider.stream(context, tools, tool_policy=tool_policy):
            if isinstance(event, ModelBackendToolStarted):
                tool_unknown = True
            if isinstance(event, ModelCompleted):
                if completion is not None:
                    raise RuntimeError("duplicate model completion")
                completion = event
            yield event
    except Exception:
        await observer.returned(ordinal, failed=True, tool_unknown=tool_unknown)
        raise
    else:
        usage = completion.usage if completion else None
        await observer.returned(
            ordinal,
            failed=completion is None,
            input_tokens=usage.processed_input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            tool_unknown=tool_unknown,
        )
