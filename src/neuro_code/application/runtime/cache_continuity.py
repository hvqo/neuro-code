"""Binding-scoped cache epoch state for monotonic provider projections.

按会话绑定管理缓存 epoch 状态, 用于保证 Provider 投影单调.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from neuro_code.domain.conversation.prompt_continuity import CacheBoundaryReason


class CacheContinuityState:
    """Track explicit structural boundaries without claiming provider cache hits."""

    __slots__ = (
        "_binding",
        "_cache_epoch",
        "_configuration_fingerprint",
        "_context_generation",
        "_pending_boundary",
        "_provider_identity",
        "_tool_schema_fingerprint",
        "_trajectory_id",
    )

    def __init__(self) -> None:
        self._binding: tuple[str | None, str | None] | None = None
        self._cache_epoch = 0
        self._context_generation = 0
        self._pending_boundary: CacheBoundaryReason | None = None
        self._provider_identity: tuple[str, str] | None = None
        self._tool_schema_fingerprint: str | None = None
        self._configuration_fingerprint: str | None = None
        self._trajectory_id = uuid.uuid4().hex

    @property
    def cache_epoch(self) -> int:
        return self._cache_epoch

    @property
    def context_generation(self) -> int:
        return self._context_generation

    @property
    def pending_boundary(self) -> CacheBoundaryReason | None:
        return self._pending_boundary

    @property
    def trajectory_id(self) -> str:
        return self._trajectory_id

    def bind(
        self,
        session_id: str | None,
        project_id: str | None,
        generation: int,
    ) -> CacheBoundaryReason | None:
        binding = (session_id, project_id)
        if self._binding is None:
            self._binding = binding
            self._context_generation = generation
            self._pending_boundary = CacheBoundaryReason.NEW_BINDING
            return CacheBoundaryReason.NEW_BINDING
        previous_session, previous_project = self._binding
        if previous_session != session_id:
            self._binding = binding
            self._trajectory_id = uuid.uuid4().hex
            self._cache_epoch = 0
            self._context_generation = generation
            self._provider_identity = None
            self._tool_schema_fingerprint = None
            self._configuration_fingerprint = None
            self._pending_boundary = CacheBoundaryReason.NEW_BINDING
            return CacheBoundaryReason.NEW_BINDING
        reason: CacheBoundaryReason | None = None
        if previous_project != project_id:
            self._binding = binding
            self.advance(CacheBoundaryReason.PROJECT_SCOPE_CHANGE)
            reason = CacheBoundaryReason.PROJECT_SCOPE_CHANGE
        if self._context_generation != generation:
            self._context_generation = generation
            self.advance(CacheBoundaryReason.FRESH_CONTEXT_ROLLOVER)
            reason = CacheBoundaryReason.FRESH_CONTEXT_ROLLOVER
        return reason

    def advance(self, reason: CacheBoundaryReason, *, generation: int | None = None) -> None:
        if not isinstance(reason, CacheBoundaryReason):
            raise TypeError("cache boundary reason must be a CacheBoundaryReason")
        self._cache_epoch += 1
        self._pending_boundary = reason
        if generation is not None:
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError("context generation must be non-negative")
            self._context_generation = generation

    def observe_provider(self, provider: str, model: str) -> CacheBoundaryReason | None:
        identity = (provider, model)
        if self._provider_identity is None:
            self._provider_identity = identity
            return None
        if identity == self._provider_identity:
            return None
        reason = (
            CacheBoundaryReason.PROVIDER_SWITCH
            if identity[0] != self._provider_identity[0]
            else CacheBoundaryReason.MODEL_SWITCH
        )
        self._provider_identity = identity
        self.advance(reason)
        return reason

    def observe_tool_schema(self, definitions: object) -> CacheBoundaryReason | None:
        payload = json.dumps(
            definitions,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(payload).hexdigest()
        if self._tool_schema_fingerprint is None:
            self._tool_schema_fingerprint = fingerprint
            return None
        if fingerprint == self._tool_schema_fingerprint:
            return None
        self._tool_schema_fingerprint = fingerprint
        self.advance(CacheBoundaryReason.TOOL_SCHEMA_CHANGE)
        return CacheBoundaryReason.TOOL_SCHEMA_CHANGE

    def observe_configuration(self, configuration: tuple[str, ...]) -> CacheBoundaryReason | None:
        fingerprint = hashlib.sha256("\0".join(configuration).encode("utf-8")).hexdigest()
        if self._configuration_fingerprint is None:
            self._configuration_fingerprint = fingerprint
            return None
        if fingerprint == self._configuration_fingerprint:
            return None
        self._configuration_fingerprint = fingerprint
        self.advance(CacheBoundaryReason.CONFIG_RELOAD)
        return CacheBoundaryReason.CONFIG_RELOAD

    def consume_boundary(self) -> None:
        self._pending_boundary = None


__all__ = ["CacheContinuityState"]
