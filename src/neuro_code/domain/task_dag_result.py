"""Exact bounded worker-result evidence, separate from DAG display previews."""

from __future__ import annotations

from dataclasses import dataclass

from neuro_code.domain.task_dag import MAX_TASK_DAG_ID_BYTES, _safe_identifier

MAX_EXACT_DAG_RESPONSE_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class TaskDagResultEvidence:
    """The complete redacted worker contract, or an explicitly truncated result.

    This is data, never verification or permission authority. Truncated evidence
    can be persisted for diagnosis but cannot satisfy a Workflow response field.
    """

    parent_task_id: str
    child_session_id: str
    response: str
    truncated: bool = False

    def __post_init__(self) -> None:
        _safe_identifier(
            self.parent_task_id, field_name="result parent task", limit=MAX_TASK_DAG_ID_BYTES
        )
        _safe_identifier(
            self.child_session_id, field_name="result child session", limit=MAX_TASK_DAG_ID_BYTES
        )
        if (
            not isinstance(self.response, str)
            or len(self.response.encode("utf-8")) > MAX_EXACT_DAG_RESPONSE_BYTES
        ):
            raise ValueError("exact DAG response must be bounded UTF-8 text")
        if type(self.truncated) is not bool:
            raise TypeError("truncation evidence must be boolean")
