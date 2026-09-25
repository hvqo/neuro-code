"""Typed metadata for model-visible request continuity.

模型可见请求连续性的类型化元数据。
"""

from __future__ import annotations

from enum import StrEnum


class ModelRequestSource(StrEnum):
    MAIN_TURN = "main_turn"
    FINALIZER = "finalizer"
    COMPACTION = "compaction"
    PROJECT_MEMORY_EXTRACTION = "project_memory_extraction"
    WEB_SEARCH_SIDECAR = "web_search_sidecar"
    SUBAGENT = "subagent"
    WORKFLOW_CHILD = "workflow_child"
    TITLE_AUXILIARY = "title_auxiliary"
    AUXILIARY = "auxiliary"


class CacheBoundaryReason(StrEnum):
    MODEL_SWITCH = "model_switch"
    PROVIDER_SWITCH = "provider_switch"
    TOOL_SCHEMA_CHANGE = "tool_schema_change"
    FULL_COMPACTION = "full_compaction"
    MICROCOMPACTION_BATCH = "microcompaction_batch"
    FRESH_CONTEXT_ROLLOVER = "fresh_context_rollover"
    CONFIG_RELOAD = "config_reload"
    PROJECT_SCOPE_CHANGE = "project_scope_change"
    INSTRUCTION_AUTHORITY_CHANGE = "instruction_authority_change"
    NEW_BINDING = "new_binding"


__all__ = ["CacheBoundaryReason", "ModelRequestSource"]
