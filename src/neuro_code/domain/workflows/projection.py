"""DW4a immutable typed result facts; no control-flow or verification authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from neuro_code.domain.workflows.definition import (
    FieldSchema,
    Map,
    SchemaKind,
    TaskBatch,
    workflow_output_schema,
)
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import StepIdentity, fingerprint, identifier, timestamp

MAX_PROJECTION_BYTES = 1024 * 1024


def validate_output(schema: FieldSchema, value: object) -> None:
    """Check the existing DW1 closed output contract, without coercion/defaults."""
    if schema.kind is SchemaKind.OBJECT:
        if not isinstance(value, dict) or set(value) != {name for name, _ in schema.properties}:
            raise ValueError("projection fields differ from DW1 schema")
        for name, child in schema.properties:
            validate_output(child, value[name])
    elif schema.kind is SchemaKind.ARRAY:
        if (
            not isinstance(value, list)
            or schema.items is None
            or schema.max_items is None
            or len(value) > schema.max_items
        ):
            raise ValueError("projection array differs from DW1 schema")
        for item in value:
            validate_output(schema.items, item)
    elif schema.kind is SchemaKind.INTEGER:
        if type(value) is not int:
            raise ValueError("projection requires an integer")
    elif schema.kind is SchemaKind.STRING:
        if not isinstance(value, str):
            raise ValueError("projection requires a string")
    else:
        raise ValueError("unsupported TaskBatch/Map output kind")


def validated_output_json(step: TaskBatch | Map, value: object) -> str:
    validate_output(workflow_output_schema(step), value)
    result = canonical(value)
    if len(result.encode("utf-8")) > MAX_PROJECTION_BYTES:
        raise ValueError("projection output exceeds bound")
    return result


@dataclass(frozen=True, slots=True)
class WorkflowResultProjection:
    """JSON strings retain immutable, canonical values; no mutable dict escapes."""

    projection_id: str
    run_id: str
    parent_session_id: str
    expansion_id: str
    step: StepIdentity
    dag_id: str
    source_json: str
    output_json: str
    created_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.projection_id,
            self.run_id,
            self.parent_session_id,
            self.expansion_id,
            self.dag_id,
        ):
            identifier(value)
        if not isinstance(self.step, StepIdentity):
            raise TypeError("projection step must be canonical")
        timestamp(self.created_at)
        for value in (self.source_json, self.output_json):
            if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_PROJECTION_BYTES:
                raise ValueError("projection JSON exceeds bound")
            if canonical(json.loads(value)) != value:
                raise ValueError("projection JSON is not canonical")

    @property
    def source_fingerprint(self) -> str:
        return digest(json.loads(self.source_json))

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "projection_id": self.projection_id,
                "run_id": self.run_id,
                "parent_session_id": self.parent_session_id,
                "expansion_id": self.expansion_id,
                "step_key": self.step.key,
                "dag_id": self.dag_id,
                "source_fingerprint": self.source_fingerprint,
                "output": json.loads(self.output_json),
                "created_at": self.created_at.astimezone(UTC).isoformat(),
            }
        )


def projection_identity(expansion_identity: str) -> str:
    fingerprint(expansion_identity)
    return "projection:" + expansion_identity
