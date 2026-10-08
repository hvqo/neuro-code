"""DW5a bounded typed values and durable output provenance. No execution authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from neuro_code.domain.workflows.definition import (
    Activity,
    ArtifactRef,
    FieldSchema,
    Map,
    SchemaKind,
    TaskBatch,
    activity_output_schema,
    workflow_output_schema,
)
from neuro_code.domain.workflows.publication import canonical, digest
from neuro_code.domain.workflows.state import StepIdentity, bounded_text, fingerprint, identifier

MAX_VALUE_BYTES = 1024 * 1024


def typed_json(schema: FieldSchema, value: object) -> str:
    """Validate DW1 types, without coercion, file reads, or mutable durable values."""
    if schema.kind is SchemaKind.OBJECT:
        if type(value) is not dict or set(value) != {n for n, _ in schema.properties}:
            raise ValueError("typed object fields differ from schema")
        for name, child in schema.properties:
            typed_json(child, value[name])
    elif schema.kind is SchemaKind.ARRAY:
        if (
            type(value) is not list
            or schema.items is None
            or schema.max_items is None
            or len(value) > schema.max_items
        ):
            raise ValueError("typed array exceeds schema")
        for item in value:
            typed_json(schema.items, item)
    elif schema.kind is SchemaKind.ARTIFACT:
        if type(value) is not dict:
            raise ValueError("artifact must be an integrity reference")
        artifact = ArtifactRef(**value)
        identifier(artifact.artifact_id)
        fingerprint(artifact.integrity_fingerprint)
        if artifact.workspace_identity is not None:
            bounded_text(artifact.workspace_identity)
            if len(artifact.workspace_identity.encode("utf-8")) > 128:
                raise ValueError("artifact workspace identity exceeds DW1 bound")
    elif (
        type(value)
        is not {SchemaKind.STRING: str, SchemaKind.INTEGER: int, SchemaKind.BOOLEAN: bool}[
            schema.kind
        ]
    ):
        raise ValueError("typed scalar differs from schema")
    result = canonical(value)
    if len(result.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError("typed value exceeds byte bound")
    return result


class OutputKind(StrEnum):
    PROJECTION = "projection"
    FAKE_ACTIVITY = "fake_activity"
    ACTIVITY = "activity"
    EMPTY_MAP = "empty_map"


@dataclass(frozen=True, slots=True)
class WorkflowStepOutput:
    run_id: str
    step: StepIdentity
    input_fingerprint: str
    kind: OutputKind
    source_id: str
    source_fingerprint: str
    output_json: str

    def __post_init__(self) -> None:
        identifier(self.run_id)
        identifier(self.source_id)
        fingerprint(self.input_fingerprint)
        fingerprint(self.source_fingerprint)
        if not isinstance(self.step, StepIdentity) or not isinstance(self.kind, OutputKind):
            raise TypeError("output provenance must be typed")
        if (
            len(self.output_json.encode("utf-8")) > MAX_VALUE_BYTES
            or canonical(json.loads(self.output_json)) != self.output_json
        ):
            raise ValueError("output must be bounded canonical JSON")

    @property
    def fingerprint(self) -> str:
        return digest(self.payload)

    @property
    def payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "step_key": self.step.key,
            "input_fingerprint": self.input_fingerprint,
            "kind": self.kind.value,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "output": json.loads(self.output_json),
        }


def validate_step_output(step: Activity | TaskBatch | Map, output: WorkflowStepOutput) -> None:
    schema = (
        activity_output_schema(step.activity)
        if isinstance(step, Activity)
        else workflow_output_schema(step)
    )
    typed_json(schema, json.loads(output.output_json))
    if output.kind is OutputKind.FAKE_ACTIVITY and not isinstance(step, Activity):
        raise ValueError("fake output requires Activity")
    if output.kind is OutputKind.ACTIVITY and not isinstance(step, Activity):
        raise ValueError("durable activity output requires Activity")
    if output.kind is OutputKind.EMPTY_MAP and (
        not isinstance(step, Map) or output.output_json != canonical({"count": 0, "items": []})
    ):
        raise ValueError("empty Map output requires zero members")
    if output.kind is OutputKind.PROJECTION and not isinstance(step, TaskBatch | Map):
        raise ValueError("worker output requires exact projection")


def expansion_id(run_id: str, step: StepIdentity) -> str:
    return "expansion:" + digest([run_id, step.key])


def invocation_id(run_id: str, step: StepIdentity, input_fingerprint: str) -> str:
    return "activity:" + digest([run_id, step.key, input_fingerprint])
