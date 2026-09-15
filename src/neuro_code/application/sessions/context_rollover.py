"""Application owner for the durable current-session context generation.

The service persists only the monotonic generation and its canonical history
boundary.  It does not copy history, summarize content, or select another
session.

当前会话持久化 context generation 的应用层 owner.

该 service 有意只持久化单调递增的 generation marker 及其 canonical history boundary; 不会
复制 history、生成 summary 或选择其他 session.
"""

from __future__ import annotations

from neuro_code.application.ports.context_rollover import (
    AdvanceContextRolloverRequest,
    ContextRolloverState,
    ReadContextRolloverRequest,
)
from neuro_code.application.ports.storage import SessionStore


class SessionContextRolloverApplicationService:
    """Read and advance one session's durable active-context generation."""

    __slots__ = ("_store",)

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def read_context_rollover(
        self,
        request: ReadContextRolloverRequest,
    ) -> ContextRolloverState:
        if not isinstance(request, ReadContextRolloverRequest):
            raise ValueError("context rollover read request must be canonical")
        generation, boundary = await self._store.load_context_generation_state(request.session_id)
        return ContextRolloverState(request.session_id, generation, boundary)

    async def advance_context_rollover(
        self,
        request: AdvanceContextRolloverRequest,
    ) -> ContextRolloverState:
        if not isinstance(request, AdvanceContextRolloverRequest):
            raise ValueError("context rollover advance request must be canonical")
        generation = await self._store.advance_context_generation(
            request.session_id,
            item_boundary=request.history_item_boundary,
            turn_id=request.turn_id,
        )
        _, boundary = await self._store.load_context_generation_state(request.session_id)
        return ContextRolloverState(request.session_id, generation, boundary)


__all__ = ["SessionContextRolloverApplicationService"]
