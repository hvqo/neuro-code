"""Immutable DW1 data contracts. None of these values can execute a workflow."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from typing import TypeVar, cast

from neuro_code.domain.agents.profile import AgentCapability, AgentProfile, AgentRole
from neuro_code.domain.task_dag import (
    MAX_TASK_DAG_EDGES,
    MAX_TASK_DAG_NODE_DEPENDENCIES,
    MAX_TASK_DAG_NODES,
    MAX_TASK_DAG_PARALLELISM,
    MAX_TASK_DAG_PROMPT_BYTES,
    TaskDagNodeKind,
)

WORKFLOW_VERSION = 1
MAX_WORKFLOW_ITERATIONS = 3
MAX_WORKFLOW_GENERATED_TASKS = 32
MAX_WORKFLOW_STEPS = 64
MAX_WORKFLOW_DEPTH = 8
MAX_WORKFLOW_SOURCE_BYTES = 128 * 1024
MAX_WORKFLOW_DECLARED_CEILING = 2**31 - 1


class SchemaKind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class FieldSchema:
    """Closed required-field schemas, not arbitrary JSON Schema expressions."""

    kind: SchemaKind
    properties: tuple[tuple[str, FieldSchema], ...] = ()
    items: FieldSchema | None = None
    max_items: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SchemaKind) or not isinstance(self.properties, tuple):
            raise TypeError("schema must use canonical immutable values")
        if any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], FieldSchema)
            for pair in self.properties
        ):
            raise TypeError("schema properties must contain immutable name/schema pairs")
        if tuple(sorted(self.properties, key=lambda pair: pair[0])) != self.properties:
            raise ValueError("schema properties must use unique sorted names")
        if len({name for name, _ in self.properties}) != len(self.properties):
            raise ValueError("schema properties must be unique")
        if self.kind is SchemaKind.OBJECT:
            if self.items is not None or self.max_items is not None:
                raise ValueError("object schema cannot carry array fields")
        elif self.kind is SchemaKind.ARRAY:
            if (
                self.properties
                or not isinstance(self.items, FieldSchema)
                or type(self.max_items) is not int
                or not 1 <= self.max_items <= MAX_WORKFLOW_GENERATED_TASKS
            ):
                raise ValueError("array schema requires bounded items")
        elif self.properties or self.items is not None or self.max_items is not None:
            raise ValueError("scalar schema cannot carry container fields")


@dataclass(frozen=True, slots=True)
class ResultRef:
    step_id: str
    field_path: tuple[str, ...]
    iteration: int | None = None
    item_key: str | None = None


@dataclass(frozen=True, slots=True)
class InputRef:
    field_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ItemRef:
    field_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Integrity assertion only; existence/freshness are future runtime checks."""

    artifact_id: str
    integrity_fingerprint: str
    workspace_identity: str | None = None


type Scalar = str | int | bool


@dataclass(frozen=True, slots=True)
class Literal:
    value: Scalar


type Reference = ResultRef | InputRef | ItemRef
type InputValue = Reference | ArtifactRef | Literal
type Bindings = tuple[tuple[str, InputValue], ...]


class ConditionOp(StrEnum):
    EQ = "eq"
    EXISTS = "exists"


@dataclass(frozen=True, slots=True)
class Condition:
    op: ConditionOp
    ref: Reference
    value: Scalar | None = None


@dataclass(frozen=True, slots=True)
class WorkflowBudget:
    max_generated_tasks: int
    max_model_calls: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_wall_seconds: int


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    prompt_template: str
    profile_ref: str
    required_capabilities: tuple[AgentCapability, ...]
    inputs: Bindings
    depends_on: tuple[str, ...]
    route: TaskDagNodeKind = TaskDagNodeKind.WRITABLE_SUBAGENT


class ActivityKind(StrEnum):
    """Future parent-owned intents. No handler or execution entry exists in DW1."""

    ADOPT = "parent.adopt"
    VERIFY = "parent.verify"
    REPAIR = "parent.repair"


@dataclass(frozen=True, slots=True)
class TaskBatch:
    step_id: str
    tasks: tuple[TaskSpec, ...]
    max_parallel: int


@dataclass(frozen=True, slots=True)
class Activity:
    step_id: str
    activity: ActivityKind
    inputs: Bindings


@dataclass(frozen=True, slots=True)
class Branch:
    step_id: str
    condition: Condition
    paths: tuple[tuple[str, tuple[Step, ...]], ...]
    then_path: str
    else_path: str


@dataclass(frozen=True, slots=True)
class Repeat:
    step_id: str
    max_iterations: int
    steps: tuple[Step, ...]
    until: Condition


@dataclass(frozen=True, slots=True)
class Map:
    step_id: str
    collection: Reference
    max_items: int
    batch: TaskBatch


type Step = TaskBatch | Activity | Branch | Repeat | Map


@dataclass(frozen=True, slots=True)
class WorkflowDiagnostic:
    code: str
    path: str
    expected: str
    actual: str | int | bool | None


class WorkflowValidationError(ValueError):
    def __init__(self, diagnostics: tuple[WorkflowDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(f"{item.code} at {item.path}" for item in diagnostics))


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    version: int
    definition_id: str
    input_schema: FieldSchema
    budget: WorkflowBudget
    steps: tuple[Step, ...]
    completion_requirements: tuple[Condition, ...]

    def __post_init__(self) -> None:
        # Internal construction has the same validation boundary as JSON source.
        diagnostics = validate_workflow(self)
        if diagnostics:
            raise WorkflowValidationError(diagnostics)

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            workflow_payload(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @property
    def canonical_bytes(self) -> bytes:
        return self.canonical_json.encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_FIELD = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_E = TypeVar("_E", AgentCapability, ActivityKind, ConditionOp, SchemaKind, TaskDagNodeKind)
DefinitionFields = tuple[
    int, str, FieldSchema, WorkflowBudget, tuple[Step, ...], tuple[Condition, ...]
]


def _schema_payload(schema: FieldSchema) -> dict[str, object]:
    result: dict[str, object] = {"type": schema.kind.value}
    if schema.kind is SchemaKind.OBJECT:
        result["properties"] = {name: _schema_payload(child) for name, child in schema.properties}
    if schema.kind is SchemaKind.ARRAY:
        assert schema.items is not None
        result.update(items=_schema_payload(schema.items), max_items=schema.max_items)
    return result


def _value_payload(value: InputValue) -> dict[str, object]:
    if isinstance(value, Literal):
        return {"kind": "literal", "value": value.value}
    if isinstance(value, ArtifactRef):
        result: dict[str, object] = {
            "kind": "artifact",
            "artifact_id": value.artifact_id,
            "integrity_fingerprint": value.integrity_fingerprint,
        }
        if value.workspace_identity is not None:
            result["workspace_identity"] = value.workspace_identity
        return result
    if isinstance(value, ResultRef):
        result = {"kind": "result", "step_id": value.step_id, "field_path": list(value.field_path)}
        if value.iteration is not None:
            result["iteration"] = value.iteration
        if value.item_key is not None:
            result["item_key"] = value.item_key
        return result
    if isinstance(value, (InputRef, ItemRef)):
        return {
            "kind": "input" if isinstance(value, InputRef) else "item",
            "field_path": list(value.field_path),
        }
    raise TypeError("input value must be canonical")


def _condition_payload(condition: Condition) -> dict[str, object]:
    result: dict[str, object] = {"op": condition.op.value, "ref": _value_payload(condition.ref)}
    if condition.op is ConditionOp.EQ:
        result["value"] = condition.value
    return result


def _bindings_payload(bindings: Bindings) -> dict[str, object]:
    return {name: _value_payload(value) for name, value in bindings}


def _step_payload(step: Step) -> dict[str, object]:
    result: dict[str, object] = {"step_id": step.step_id}
    if isinstance(step, TaskBatch):
        result.update(
            kind="TaskBatch",
            max_parallel=step.max_parallel,
            tasks=[
                {
                    "task_id": task.task_id,
                    "prompt_template": task.prompt_template,
                    "profile_ref": task.profile_ref,
                    "route": task.route.value,
                    "required_capabilities": [cap.value for cap in task.required_capabilities],
                    "inputs": _bindings_payload(task.inputs),
                    "depends_on": list(task.depends_on),
                }
                for task in step.tasks
            ],
        )
    elif isinstance(step, Activity):
        result.update(
            kind="Activity", activity=step.activity.value, inputs=_bindings_payload(step.inputs)
        )
    elif isinstance(step, Branch):
        result.update(
            kind="Branch",
            condition=_condition_payload(step.condition),
            paths={
                name: [_step_payload(child) for child in children] for name, children in step.paths
            },
            then_path=step.then_path,
            else_path=step.else_path,
        )
    elif isinstance(step, Repeat):
        result.update(
            kind="Repeat",
            max_iterations=step.max_iterations,
            steps=[_step_payload(child) for child in step.steps],
            until=_condition_payload(step.until),
        )
    elif isinstance(step, Map):
        result.update(
            kind="Map",
            collection=_value_payload(step.collection),
            max_items=step.max_items,
            batch=_step_payload(step.batch),
        )
    else:
        raise TypeError("unknown workflow IR node")
    return result


def workflow_payload(definition: WorkflowDefinition) -> dict[str, object]:
    """Fresh JSON-compatible projection; mutating it cannot mutate the IR."""
    return {
        "version": definition.version,
        "definition_id": definition.definition_id,
        "input_schema": _schema_payload(definition.input_schema),
        "budget": {
            field.name: getattr(definition.budget, field.name) for field in fields(WorkflowBudget)
        },
        "steps": [_step_payload(step) for step in definition.steps],
        "completion_requirements": [
            _condition_payload(item) for item in definition.completion_requirements
        ],
    }


def _property_path(path: str, name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return f"{path}.{name}"
    return f"{path}[{json.dumps(name, ensure_ascii=True)}]"


class _Decoder:
    def __init__(self) -> None:
        self.errors: list[WorkflowDiagnostic] = []

    def error(self, code: str, path: str, expected: str, actual: object) -> None:
        # Bounded values; no repr, traceback, raw source or prompt in diagnostics.
        if isinstance(actual, str):
            summary: Scalar | None = actual[:64].encode("utf-8", errors="replace").decode("utf-8")
        elif actual is None or type(actual) in (int, bool):
            summary = cast(Scalar | None, actual)
        else:
            summary = type(actual).__name__
        self.errors.append(WorkflowDiagnostic(code, path, expected, summary))

    def obj(
        self, raw: object, path: str, required: set[str], optional: set[str] | None = None
    ) -> dict[str, object]:
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            self.error("INVALID_TYPE", path, "JSON object", raw)
            return {}
        value = cast(dict[str, object], raw)
        for key in sorted(value.keys() - required - (optional or set())):
            self.error("UNKNOWN_FIELD", _property_path(path, key), "declared field", key)
        for key in sorted(required - value.keys()):
            self.error("MISSING_FIELD", _property_path(path, key), "required field", None)
        return value

    def integer(self, raw: object, path: str, maximum: int, code: str = "INVALID_BOUND") -> int:
        if type(raw) is not int or not 1 <= raw <= maximum:
            self.error(code, path, f"integer between 1 and {maximum}", raw)
            return 1
        return raw

    def text(
        self, raw: object, path: str, *, identifier: bool = False, prompt: bool = False
    ) -> str:
        limit = MAX_TASK_DAG_PROMPT_BYTES if prompt else 128
        valid = isinstance(raw, str) and bool(raw.strip())
        if valid:
            try:
                valid = len(cast(str, raw).encode("utf-8")) <= limit and not any(
                    (ord(char) < 32 and (not prompt or char not in "\n\r\t")) or ord(char) == 127
                    for char in cast(str, raw)
                )
            except UnicodeEncodeError:
                valid = False
        if valid and identifier:
            valid = _ID.fullmatch(cast(str, raw)) is not None
        if not valid:
            self.error(
                "INVALID_IDENTIFIER" if identifier else "INVALID_TEXT",
                path,
                "bounded identifier" if identifier else "bounded non-empty UTF-8 text",
                "redacted text" if prompt else raw,
            )
            return "invalid"
        return cast(str, raw)

    def enum(self, raw: object, path: str, enum: type[_E], code: str) -> _E:
        if isinstance(raw, str):
            try:
                return enum(raw)
            except ValueError:
                pass
        self.error(code, path, " / ".join(item.value for item in enum), raw)
        return next(iter(enum))

    def array(
        self, raw: object, path: str, maximum: int, *, nonempty: bool = False
    ) -> list[object]:
        if not isinstance(raw, list):
            self.error("INVALID_TYPE", path, "JSON array", raw)
            return []
        if len(raw) > maximum or (nonempty and not raw):
            self.error("INVALID_BOUND", path, f"array size {int(nonempty)}..{maximum}", len(raw))
        return cast(list[object], raw[:maximum])

    def schema(self, raw: object, path: str, depth: int = 0) -> FieldSchema:
        if depth > MAX_WORKFLOW_DEPTH:
            self.error("STRUCTURE_TOO_DEEP", path, "bounded schema depth", depth)
            return FieldSchema(SchemaKind.STRING)
        value = self.obj(raw, path, {"type"}, {"properties", "items", "max_items"})
        kind = self.enum(value.get("type"), f"{path}.type", SchemaKind, "INVALID_SCHEMA")
        if kind is SchemaKind.OBJECT:
            value = self.obj(raw, path, {"type", "properties"})
            properties = self.obj(
                value.get("properties"),
                f"{path}.properties",
                set(),
                set(cast(dict[str, object], value.get("properties", {})))
                if isinstance(value.get("properties"), dict)
                else set(),
            )
            if len(properties) > MAX_WORKFLOW_STEPS:
                self.error(
                    "INVALID_BOUND", f"{path}.properties", "at most 64 fields", len(properties)
                )
                properties = dict(list(properties.items())[:MAX_WORKFLOW_STEPS])
            pairs = []
            for name, child in sorted(properties.items()):
                if _FIELD.fullmatch(name) is None:
                    self.error(
                        "INVALID_FIELD_PATH",
                        _property_path(f"{path}.properties", name),
                        "field name",
                        name,
                    )
                pairs.append(
                    (
                        name,
                        self.schema(child, _property_path(f"{path}.properties", name), depth + 1),
                    )
                )
            return FieldSchema(kind, tuple(pairs))
        if kind is SchemaKind.ARRAY:
            value = self.obj(raw, path, {"type", "items", "max_items"})
            return FieldSchema(
                kind,
                items=self.schema(value.get("items"), f"{path}.items", depth + 1),
                max_items=self.integer(
                    value.get("max_items"), f"{path}.max_items", MAX_WORKFLOW_GENERATED_TASKS
                ),
            )
        self.obj(raw, path, {"type"})
        return FieldSchema(kind)

    def field_path(self, raw: object, path: str) -> tuple[str, ...]:
        parts = self.array(raw, path, MAX_WORKFLOW_DEPTH)
        result = []
        for i, part in enumerate(parts):
            if not isinstance(part, str) or _FIELD.fullmatch(part) is None:
                self.error(
                    "INVALID_FIELD_PATH", f"{path}[{i}]", "field name (no expression/index)", part
                )
                result.append("invalid")
            else:
                result.append(part)
        return tuple(result)

    def value(self, raw: object, path: str) -> InputValue:
        if not isinstance(raw, dict):
            self.error("INVALID_TYPE", path, "tagged input object", raw)
            return Literal("")
        kind = raw.get("kind")
        if kind in ("input", "item"):
            value = self.obj(raw, path, {"kind", "field_path"})
            cls = InputRef if kind == "input" else ItemRef
            return cls(self.field_path(value.get("field_path"), f"{path}.field_path"))
        if kind == "result":
            value = self.obj(
                raw, path, {"kind", "step_id", "field_path"}, {"iteration", "item_key"}
            )
            iteration = (
                self.integer(value["iteration"], f"{path}.iteration", MAX_WORKFLOW_ITERATIONS)
                if "iteration" in value
                else None
            )
            item_key = (
                self.text(value["item_key"], f"{path}.item_key", identifier=True)
                if "item_key" in value
                else None
            )
            return ResultRef(
                self.text(value.get("step_id"), f"{path}.step_id", identifier=True),
                self.field_path(value.get("field_path"), f"{path}.field_path"),
                iteration,
                item_key,
            )
        if kind == "artifact":
            value = self.obj(
                raw, path, {"kind", "artifact_id", "integrity_fingerprint"}, {"workspace_identity"}
            )
            digest = value.get("integrity_fingerprint")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                self.error(
                    "INVALID_ARTIFACT_FINGERPRINT",
                    f"{path}.integrity_fingerprint",
                    "lowercase SHA-256",
                    digest,
                )
                digest = "0" * 64
            workspace = (
                self.text(value["workspace_identity"], f"{path}.workspace_identity")
                if "workspace_identity" in value
                else None
            )
            return ArtifactRef(
                self.text(value.get("artifact_id"), f"{path}.artifact_id", identifier=True),
                digest,
                workspace,
            )
        if kind == "literal":
            value = self.obj(raw, path, {"kind", "value"})
            scalar = value.get("value")
            if type(scalar) not in (str, int, bool):
                self.error("INVALID_LITERAL", f"{path}.value", "string, integer or boolean", scalar)
                scalar = ""
            if isinstance(scalar, str):
                try:
                    if len(scalar.encode("utf-8")) > MAX_TASK_DAG_PROMPT_BYTES or "\x00" in scalar:
                        raise ValueError
                except (ValueError, UnicodeEncodeError):
                    self.error("INVALID_LITERAL", f"{path}.value", "bounded UTF-8 string", scalar)
            if type(scalar) is int and abs(scalar) > MAX_WORKFLOW_DECLARED_CEILING:
                self.error("INVALID_LITERAL", f"{path}.value", "bounded integer", scalar)
            return Literal(cast(Scalar, scalar))
        self.error(
            "INVALID_REFERENCE", f"{path}.kind", "input / item / result / artifact / literal", kind
        )
        return Literal("")

    def reference(self, raw: object, path: str) -> Reference:
        value = self.value(raw, path)
        if not isinstance(value, (ResultRef, InputRef, ItemRef)):
            self.error("INVALID_REFERENCE", path, "typed data reference", raw)
            return InputRef(())
        return value

    def bindings(self, raw: object, path: str) -> Bindings:
        value = self.obj(raw, path, set(), set(raw) if isinstance(raw, dict) else set())
        if len(value) > MAX_WORKFLOW_STEPS:
            self.error("INVALID_BOUND", path, "at most 64 inputs", len(value))
        result = []
        for name, item in sorted(value.items())[:MAX_WORKFLOW_STEPS]:
            if _FIELD.fullmatch(name) is None:
                self.error("INVALID_IDENTIFIER", _property_path(path, name), "input name", name)
            result.append((name, self.value(item, _property_path(path, name))))
        return tuple(result)

    def condition(self, raw: object, path: str) -> Condition:
        value = self.obj(raw, path, {"op", "ref"}, {"value"})
        op = self.enum(value.get("op"), f"{path}.op", ConditionOp, "INVALID_CONDITION")
        scalar: Scalar | None = None
        if op is ConditionOp.EQ:
            self.obj(raw, path, {"op", "ref", "value"})
            literal = self.value({"kind": "literal", "value": value.get("value")}, path)
            assert isinstance(literal, Literal)
            scalar = literal.value
        elif "value" in value:
            self.error(
                "UNKNOWN_FIELD", f"{path}.value", "exists has no comparison value", value["value"]
            )
        return Condition(op, self.reference(value.get("ref"), f"{path}.ref"), scalar)

    def task(self, raw: object, path: str) -> TaskSpec:
        value = self.obj(
            raw,
            path,
            {
                "task_id",
                "prompt_template",
                "profile_ref",
                "required_capabilities",
                "inputs",
                "depends_on",
            },
            {"route"},
        )
        capabilities = tuple(
            self.enum(
                item, f"{path}.required_capabilities[{i}]", AgentCapability, "INVALID_CAPABILITY"
            )
            for i, item in enumerate(
                self.array(
                    value.get("required_capabilities"),
                    f"{path}.required_capabilities",
                    len(AgentCapability),
                )
            )
        )
        if len(set(capabilities)) != len(capabilities):
            self.error(
                "DUPLICATE_CAPABILITY",
                f"{path}.required_capabilities",
                "unique capability intent",
                None,
            )
        dependencies = tuple(
            self.text(item, f"{path}.depends_on[{i}]", identifier=True)
            for i, item in enumerate(
                self.array(
                    value.get("depends_on"), f"{path}.depends_on", MAX_TASK_DAG_NODE_DEPENDENCIES
                )
            )
        )
        return TaskSpec(
            self.text(value.get("task_id"), f"{path}.task_id", identifier=True),
            self.text(value.get("prompt_template"), f"{path}.prompt_template", prompt=True),
            self.text(value.get("profile_ref"), f"{path}.profile_ref", identifier=True),
            tuple(sorted(capabilities, key=lambda cap: cap.value)),
            self.bindings(value.get("inputs"), f"{path}.inputs"),
            dependencies,
            self.enum(
                value.get("route", TaskDagNodeKind.WRITABLE_SUBAGENT.value),
                f"{path}.route",
                TaskDagNodeKind,
                "UNSUPPORTED_TASK_ROUTE",
            ),
        )

    def steps(
        self, raw: object, path: str, depth: int = 0, *, nonempty: bool = True
    ) -> tuple[Step, ...]:
        if depth > MAX_WORKFLOW_DEPTH:
            self.error("STRUCTURE_TOO_DEEP", path, "bounded control depth", depth)
            return ()
        return tuple(
            step
            for i, value in enumerate(self.array(raw, path, MAX_WORKFLOW_STEPS, nonempty=nonempty))
            if (step := self.step(value, f"{path}[{i}]", depth)) is not None
        )

    def step(self, raw: object, path: str, depth: int) -> Step | None:
        if not isinstance(raw, dict):
            self.error("INVALID_TYPE", path, "step object", raw)
            return None
        kind = raw.get("kind")
        keys = {
            "TaskBatch": {"tasks", "max_parallel"},
            "Activity": {"activity", "inputs"},
            "Branch": {"condition", "paths", "then_path", "else_path"},
            "Repeat": {"max_iterations", "steps", "until"},
            "Map": {"collection", "max_items", "batch"},
        }
        if not isinstance(kind, str) or kind not in keys:
            self.error(
                "UNKNOWN_NODE_KIND",
                f"{path}.kind",
                "TaskBatch / Activity / Branch / Repeat / Map",
                kind,
            )
            return None
        value = self.obj(raw, path, keys[kind] | {"kind", "step_id"})
        step_id = self.text(value.get("step_id"), f"{path}.step_id", identifier=True)
        if kind == "TaskBatch":
            tasks = tuple(
                self.task(item, f"{path}.tasks[{i}]")
                for i, item in enumerate(
                    self.array(
                        value.get("tasks"), f"{path}.tasks", MAX_TASK_DAG_NODES, nonempty=True
                    )
                )
            )
            order = {task.task_id: i for i, task in enumerate(tasks)}
            # Diagnose source indices before normalizing the dependency set.
            for i, task in enumerate(tasks):
                for j, dependency in enumerate(task.depends_on):
                    if dependency not in order:
                        self.error(
                            "INVALID_DEPENDENCY",
                            f"{path}.tasks[{i}].depends_on[{j}]",
                            "task id in the same batch",
                            dependency,
                        )
            tasks = tuple(
                TaskSpec(
                    task.task_id,
                    task.prompt_template,
                    task.profile_ref,
                    task.required_capabilities,
                    task.inputs,
                    tuple(sorted(task.depends_on, key=lambda dep: (order.get(dep, -1), dep))),
                    task.route,
                )
                for task in tasks
            )
            return TaskBatch(
                step_id,
                tasks,
                self.integer(
                    value.get("max_parallel"),
                    f"{path}.max_parallel",
                    MAX_TASK_DAG_PARALLELISM,
                    "INVALID_PARALLEL_BOUND",
                ),
            )
        if kind == "Activity":
            return Activity(
                step_id,
                self.enum(
                    value.get("activity"), f"{path}.activity", ActivityKind, "UNSUPPORTED_ACTIVITY"
                ),
                self.bindings(value.get("inputs"), f"{path}.inputs"),
            )
        if kind == "Branch":
            paths = self.obj(
                value.get("paths"),
                f"{path}.paths",
                set(),
                set(cast(dict[str, object], value["paths"]))
                if isinstance(value.get("paths"), dict)
                else set(),
            )
            if len(paths) != 2:
                self.error(
                    "ILLEGAL_BRANCH_TARGET", f"{path}.paths", "exactly two named paths", len(paths)
                )
            pairs = tuple(
                (
                    self.text(name, _property_path(f"{path}.paths", name), identifier=True),
                    self.steps(
                        items, _property_path(f"{path}.paths", name), depth + 1, nonempty=False
                    ),
                )
                for name, items in sorted(paths.items())[:2]
            )
            return Branch(
                step_id,
                self.condition(value.get("condition"), f"{path}.condition"),
                pairs,
                self.text(value.get("then_path"), f"{path}.then_path", identifier=True),
                self.text(value.get("else_path"), f"{path}.else_path", identifier=True),
            )
        if kind == "Repeat":
            if "max_iterations" not in value:
                self.error(
                    "UNBOUNDED_REPEAT",
                    f"{path}.max_iterations",
                    "explicit integer between 1 and 3",
                    None,
                )
            return Repeat(
                step_id,
                self.integer(
                    value.get("max_iterations"),
                    f"{path}.max_iterations",
                    MAX_WORKFLOW_ITERATIONS,
                    "INVALID_REPEAT_BOUND",
                ),
                self.steps(value.get("steps"), f"{path}.steps", depth + 1),
                self.condition(value.get("until"), f"{path}.until"),
            )
        batch = (
            self.step(value.get("batch"), f"{path}.batch", depth + 1)
            if depth < MAX_WORKFLOW_DEPTH
            else None
        )
        if not isinstance(batch, TaskBatch):
            nested_kind = (
                cast(dict[str, object], value["batch"]).get("kind")
                if isinstance(value.get("batch"), dict)
                else None
            )
            self.error(
                "NESTED_MAP" if nested_kind == "Map" else "ILLEGAL_MAP_BODY",
                f"{path}.batch",
                "one TaskBatch template; no nested expansion",
                nested_kind,
            )
            return None
        return Map(
            step_id,
            self.reference(value.get("collection"), f"{path}.collection"),
            self.integer(
                value.get("max_items"), f"{path}.max_items", MAX_TASK_DAG_NODES, "MAP_OVER_BOUND"
            ),
            batch,
        )

    def definition(self, raw: object) -> DefinitionFields:
        value = self.obj(
            raw,
            "$",
            {
                "version",
                "definition_id",
                "input_schema",
                "budget",
                "steps",
                "completion_requirements",
            },
        )
        if type(value.get("version")) is not int or value["version"] != WORKFLOW_VERSION:
            self.error(
                "UNSUPPORTED_VERSION", "$.version", "integer version 1", value.get("version")
            )
        budget_keys = {field.name for field in fields(WorkflowBudget)}
        budget = self.obj(value.get("budget"), "$.budget", budget_keys)
        ceilings = {
            name: self.integer(
                budget.get(name),
                f"$.budget.{name}",
                MAX_WORKFLOW_GENERATED_TASKS
                if name == "max_generated_tasks"
                else MAX_WORKFLOW_DECLARED_CEILING,
                "INVALID_BUDGET",
            )
            for name in sorted(budget_keys)
        }
        schema = self.schema(value.get("input_schema"), "$.input_schema")
        if schema.kind is not SchemaKind.OBJECT:
            self.error("INVALID_SCHEMA", "$.input_schema", "object schema", schema.kind.value)
        return (
            WORKFLOW_VERSION,
            self.text(value.get("definition_id"), "$.definition_id", identifier=True),
            schema,
            WorkflowBudget(**ceilings),
            self.steps(value.get("steps"), "$.steps"),
            tuple(
                self.condition(item, f"$.completion_requirements[{i}]")
                for i, item in enumerate(
                    self.array(
                        value.get("completion_requirements"),
                        "$.completion_requirements",
                        MAX_WORKFLOW_STEPS,
                    )
                )
            ),
        )


def _object(**properties: FieldSchema) -> FieldSchema:
    return FieldSchema(SchemaKind.OBJECT, tuple(sorted(properties.items())))


_STRING = FieldSchema(SchemaKind.STRING)
_INTEGER = FieldSchema(SchemaKind.INTEGER)
_BOOLEAN = FieldSchema(SchemaKind.BOOLEAN)


def _batch_output(batch: TaskBatch) -> FieldSchema:
    return _object(
        tasks=_object(
            **{task.task_id: _object(status=_STRING, response=_STRING) for task in batch.tasks}
        )
    )


def workflow_output_schema(step: TaskBatch | Map) -> FieldSchema:
    """Expose the same DW1 output contract used by ResultRef validation."""
    if isinstance(step, TaskBatch):
        return _batch_output(step)
    if isinstance(step, Map):
        return _object(
            count=_INTEGER,
            items=FieldSchema(
                SchemaKind.ARRAY, items=_batch_output(step.batch), max_items=step.max_items
            ),
        )
    raise TypeError("result projection requires TaskBatch or Map")


def activity_output_schema(activity: ActivityKind) -> FieldSchema:
    match activity:
        case ActivityKind.ADOPT:
            return _object(status=_STRING, parent_workspace_changed=_BOOLEAN)
        case ActivityKind.VERIFY:
            return _object(status=_STRING, workspace_generation=_INTEGER)
        case ActivityKind.REPAIR:
            return _object(status=_STRING, response=_STRING)


class _Validator(_Decoder):
    def __init__(self, definition: DefinitionFields, profiles: tuple[AgentProfile, ...]) -> None:
        super().__init__()
        self.input_schema = definition[2]
        self.profiles = {profile.profile_id: profile for profile in profiles}
        self.all_ids: set[str] = set()
        self.step_count = 0
        self.repeats: dict[str, tuple[Repeat, FieldSchema]] = {}
        self.budget = definition[3]
        self.definition_steps = definition[4]
        self.requirements = definition[5]
        self.activity_inputs: dict[str, dict[str, FieldSchema]] = {}

    def index(self, steps: tuple[Step, ...], path: str) -> None:
        for i, step in enumerate(steps):
            location = f"{path}[{i}]"
            self.step_count += 1
            if step.step_id in self.all_ids:
                self.error(
                    "DUPLICATE_STEP_ID",
                    f"{location}.step_id",
                    "globally unique step id",
                    step.step_id,
                )
            self.all_ids.add(step.step_id)
            if isinstance(step, Branch):
                for name, children in step.paths:
                    self.index(children, _property_path(f"{location}.paths", name))
            elif isinstance(step, Repeat):
                self.index(step.steps, f"{location}.steps")
            elif isinstance(step, Map):
                self.index((step.batch,), f"{location}.template")

    def resolve(
        self,
        ref: Reference,
        available: dict[str, FieldSchema],
        path: str,
        item_schema: FieldSchema | None,
    ) -> FieldSchema | None:
        if isinstance(ref, InputRef):
            schema: FieldSchema | None = self.input_schema
        elif isinstance(ref, ItemRef):
            schema = item_schema
            if schema is None:
                self.error(
                    "INVALID_REFERENCE",
                    path,
                    "item reference only inside Map TaskSpec inputs",
                    None,
                )
                return None
        else:
            schema = available.get(ref.step_id)
            if schema is None:
                code = (
                    "NON_DOMINATING_RESULT" if ref.step_id in self.all_ids else "INVALID_REFERENCE"
                )
                self.error(
                    code, f"{path}.step_id", "result produced on every incoming path", ref.step_id
                )
                return None
            if ref.item_key is not None:
                # DW1 has no declared finite key-set proof. Never assume a Map item exists.
                self.error(
                    "UNPROVEN_RESULT_SELECTOR",
                    f"{path}.item_key",
                    "guaranteed whole Map result; item keys need future proof",
                    ref.item_key,
                )
                return None
            if ref.iteration is not None:
                repeated = self.repeats.get(ref.step_id)
                if repeated is None or ref.iteration != 1:
                    self.error(
                        "UNPROVEN_RESULT_SELECTOR",
                        f"{path}.iteration",
                        "iteration 1 of a completed Repeat (at least one body execution)",
                        ref.iteration,
                    )
                    return None
                schema = repeated[1]
        assert schema is not None
        for i, part in enumerate(ref.field_path):
            schema = dict(schema.properties).get(part) if schema.kind is SchemaKind.OBJECT else None
            if schema is None:
                self.error(
                    "INVALID_RESULT_PATH",
                    f"{path}.field_path[{i}]",
                    "declared required field",
                    part,
                )
                return None
        return schema

    def check_condition(
        self,
        condition: Condition,
        available: dict[str, FieldSchema],
        path: str,
        item: FieldSchema | None = None,
    ) -> None:
        schema = self.resolve(condition.ref, available, f"{path}.ref", item)
        if condition.op is ConditionOp.EQ and schema is not None:
            expected = {
                SchemaKind.STRING: str,
                SchemaKind.INTEGER: int,
                SchemaKind.BOOLEAN: bool,
            }.get(schema.kind)
            if expected is None or type(condition.value) is not expected:
                self.error(
                    "CONDITION_TYPE_MISMATCH",
                    f"{path}.value",
                    f"exact {schema.kind.value} scalar",
                    condition.value,
                )

    def check_bindings(
        self,
        bindings: Bindings,
        available: dict[str, FieldSchema],
        path: str,
        item: FieldSchema | None,
    ) -> None:
        for name, value in bindings:
            if isinstance(value, (InputRef, ItemRef, ResultRef)):
                self.resolve(value, available, _property_path(path, name), item)

    def batch(
        self,
        batch: TaskBatch,
        available: dict[str, FieldSchema],
        path: str,
        item: FieldSchema | None = None,
    ) -> None:
        ids = [task.task_id for task in batch.tasks]
        if len(set(ids)) != len(ids):
            self.error("DUPLICATE_TASK_ID", f"{path}.tasks", "unique batch-local task ids", None)
        edges = sum(len(task.depends_on) for task in batch.tasks)
        if edges > MAX_TASK_DAG_EDGES:
            self.error(
                "TOO_MANY_DEPENDENCIES",
                f"{path}.tasks",
                f"at most {MAX_TASK_DAG_EDGES} edges",
                edges,
            )
        pending: dict[str, set[str]] = {}
        for i, task in enumerate(batch.tasks):
            location = f"{path}.tasks[{i}]"
            if len(set(task.depends_on)) != len(task.depends_on):
                self.error(
                    "DUPLICATE_DEPENDENCY", f"{location}.depends_on", "unique dependencies", None
                )
            for j, dependency in enumerate(task.depends_on):
                if dependency not in ids:
                    self.error(
                        "INVALID_DEPENDENCY",
                        f"{location}.depends_on[{j}]",
                        "task id in the same batch",
                        dependency,
                    )
            pending[task.task_id] = set(task.depends_on) & set(ids)
            self.check_bindings(task.inputs, available, f"{location}.inputs", item)
            # Known built-in roles have one supported worker route. Custom IDs remain intent.
            if (
                task.profile_ref in {role.value for role in AgentRole}
                and task.profile_ref != AgentRole.WRITABLE_WORKER.value
            ):
                self.error(
                    "PROFILE_ROUTE_CONTRADICTION",
                    f"{location}.profile_ref",
                    "writable_worker role for writable_subagent",
                    task.profile_ref,
                )
            profile = self.profiles.get(task.profile_ref)
            if profile is not None:
                if profile.role is not AgentRole.WRITABLE_WORKER:
                    self.error(
                        "PROFILE_ROUTE_CONTRADICTION",
                        f"{location}.profile_ref",
                        "known writable worker profile",
                        task.profile_ref,
                    )
                for cap in task.required_capabilities:
                    if cap not in profile.capability_policy:
                        self.error(
                            "PROFILE_CAPABILITY_CONTRADICTION",
                            f"{location}.required_capabilities",
                            "intent contained in known profile policy; never a runtime grant",
                            cap.value,
                        )
        while pending:
            ready = {task_id for task_id, dependencies in pending.items() if not dependencies}
            if not ready:
                self.error("DEPENDENCY_CYCLE", f"{path}.tasks", "acyclic dependency graph", None)
                break
            pending = {
                task_id: dependencies - ready
                for task_id, dependencies in pending.items()
                if task_id not in ready
            }

    def walk(
        self,
        steps: tuple[Step, ...],
        available: dict[str, FieldSchema],
        path: str,
        *,
        in_repeat: bool = False,
    ) -> tuple[dict[str, FieldSchema], int]:
        scope = dict(available)
        generated = 0
        for i, step in enumerate(steps):
            location = f"{path}[{i}]"
            if isinstance(step, TaskBatch):
                self.batch(step, scope, location)
                scope[step.step_id] = workflow_output_schema(step)
                generated += len(step.tasks)
            elif isinstance(step, Activity):
                self.activity_inputs[step.step_id] = {
                    name: schema
                    for name, value in step.inputs
                    if isinstance(value, InputRef | ResultRef | ItemRef)
                    and (
                        schema := self.resolve(
                            value, scope, _property_path(f"{location}.inputs", name), None
                        )
                    )
                    is not None
                }
                scope[step.step_id] = activity_output_schema(step.activity)
            elif isinstance(step, Branch):
                names = {name for name, _ in step.paths}
                if {step.then_path, step.else_path} != names or step.then_path == step.else_path:
                    self.error(
                        "ILLEGAL_BRANCH_TARGET",
                        location,
                        "distinct targets covering exactly the declared paths",
                        None,
                    )
                self.check_condition(step.condition, scope, f"{location}.condition")
                costs = []
                for name, children in step.paths:
                    _, cost = self.walk(
                        children,
                        scope,
                        _property_path(f"{location}.paths", name),
                        in_repeat=in_repeat,
                    )
                    costs.append(cost)
                generated += max(costs, default=0)
                # Branch-local output is not promoted, even for an exists condition.
            elif isinstance(step, Repeat):
                if in_repeat:
                    self.error("NESTED_REPEAT", location, "no Repeat inside Repeat", None)
                inner, cost = self.walk(step.steps, scope, f"{location}.steps", in_repeat=True)
                self.check_condition(step.until, inner, f"{location}.until")
                body = _object(
                    **{name: schema for name, schema in inner.items() if name not in scope}
                )
                scope[step.step_id] = _object(iterations=_INTEGER, last=body)
                self.repeats[step.step_id] = (step, body)
                generated += cost * step.max_iterations
            elif isinstance(step, Map):
                collection = self.resolve(step.collection, scope, f"{location}.collection", None)
                item = None
                if collection is not None:
                    if collection.kind is not SchemaKind.ARRAY:
                        self.error(
                            "INVALID_MAP_COLLECTION",
                            f"{location}.collection",
                            "bounded array schema",
                            collection.kind.value,
                        )
                    else:
                        item = collection.items
                        if cast(int, collection.max_items) > step.max_items:
                            self.error(
                                "MAP_OVER_BOUND",
                                f"{location}.max_items",
                                "ceiling covers the declared collection maximum; never truncate",
                                step.max_items,
                            )
                self.batch(step.batch, scope, f"{location}.batch", item)
                expanded = step.max_items * len(step.batch.tasks)
                if expanded > MAX_TASK_DAG_NODES:
                    self.error(
                        "MAP_OVER_BOUND",
                        location,
                        f"single expanded batch at most {MAX_TASK_DAG_NODES} tasks",
                        expanded,
                    )
                scope[step.step_id] = workflow_output_schema(step)
                generated += expanded
        return scope, generated

    def validate(self) -> tuple[WorkflowDiagnostic, ...]:
        self.index(self.definition_steps, "$.steps")
        if self.step_count > MAX_WORKFLOW_STEPS:
            self.error(
                "TOO_MANY_STEPS",
                "$.steps",
                f"at most {MAX_WORKFLOW_STEPS} total declarations",
                self.step_count,
            )
        available, generated = self.walk(self.definition_steps, {}, "$.steps")
        if generated > self.budget.max_generated_tasks:
            self.error(
                "STRUCTURAL_EXPANSION_OVERFLOW",
                "$.budget.max_generated_tasks",
                f"at least {generated} worst-case tasks (policy max {MAX_WORKFLOW_GENERATED_TASKS})",
                self.budget.max_generated_tasks,
            )
        for i, condition in enumerate(self.requirements):
            self.check_condition(condition, available, f"$.completion_requirements[{i}]")
        return tuple(self.errors)


def activity_reference_schemas(
    definition: WorkflowDefinition, step_id: str
) -> dict[str, FieldSchema]:
    """Reuse DW1 dominance/schema resolution for already validated Activity inputs."""
    validator = _Validator(
        (
            definition.version,
            definition.definition_id,
            definition.input_schema,
            definition.budget,
            definition.steps,
            definition.completion_requirements,
        ),
        (),
    )
    validator.index(definition.steps, "$.steps")
    validator.walk(definition.steps, {}, "$.steps")
    return validator.activity_inputs[step_id]


def _immutable(value: object) -> bool:
    if (
        value is None
        or type(value) in (str, int, bool)
        or isinstance(
            value, (AgentCapability, ActivityKind, ConditionOp, SchemaKind, TaskDagNodeKind)
        )
    ):
        return True
    if isinstance(value, tuple):
        return all(_immutable(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return all(_immutable(getattr(value, field.name)) for field in fields(value))
    return False


def validate_workflow(
    definition: WorkflowDefinition, *, known_profiles: tuple[AgentProfile, ...] = ()
) -> tuple[WorkflowDiagnostic, ...]:
    """Validate direct typed construction as strictly as external source."""
    if not _immutable(definition):
        return (
            WorkflowDiagnostic("INVALID_IR", "$", "deeply immutable canonical typed values", None),
        )
    catalog_errors = _profile_catalog_errors(known_profiles)
    if catalog_errors:
        return catalog_errors
    decoder = _Decoder()
    try:
        payload = workflow_payload(definition)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_WORKFLOW_SOURCE_BYTES:
            return (
                WorkflowDiagnostic(
                    "DEFINITION_TOO_LARGE",
                    "$",
                    "canonical representation within source byte ceiling",
                    len(encoded),
                ),
            )
        decoded = decoder.definition(payload)
    except (AttributeError, TypeError, ValueError, RecursionError):
        return (WorkflowDiagnostic("INVALID_IR", "$", "canonical typed values", None),)
    if decoder.errors:
        return tuple(decoder.errors)
    original = (
        definition.version,
        definition.definition_id,
        definition.input_schema,
        definition.budget,
        definition.steps,
        definition.completion_requirements,
    )
    if decoded != original:
        return (
            WorkflowDiagnostic(
                "NONCANONICAL_IR", "$", "canonical tuple/set ordering and typed values", None
            ),
        )
    return _Validator(decoded, known_profiles).validate()


def _profile_catalog_errors(profiles: tuple[AgentProfile, ...]) -> tuple[WorkflowDiagnostic, ...]:
    if (
        not isinstance(profiles, tuple)
        or not all(isinstance(profile, AgentProfile) for profile in profiles)
        or len({profile.profile_id for profile in profiles}) != len(profiles)
    ):
        return (
            WorkflowDiagnostic(
                "INVALID_PROFILE_CATALOG", "$", "unique immutable profile intent catalog", None
            ),
        )
    return ()


class _DuplicateKey(ValueError):
    pass


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(value)


def compile_workflow(
    source: str, *, known_profiles: tuple[AgentProfile, ...] = ()
) -> WorkflowDefinition:
    """Strict JSON → validated immutable IR. Known profiles are injected intent only."""
    catalog_errors = _profile_catalog_errors(known_profiles)
    if catalog_errors:
        raise WorkflowValidationError(catalog_errors)
    try:
        bounded = (
            isinstance(source, str) and len(source.encode("utf-8")) <= MAX_WORKFLOW_SOURCE_BYTES
        )
    except UnicodeEncodeError:
        bounded = False
    if not bounded:
        raise WorkflowValidationError(
            (
                WorkflowDiagnostic(
                    "INVALID_SOURCE",
                    "$",
                    f"UTF-8 JSON text <= {MAX_WORKFLOW_SOURCE_BYTES} bytes",
                    None,
                ),
            )
        )
    try:
        raw: object = json.loads(source, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except _DuplicateKey as error:
        raise WorkflowValidationError(
            (
                WorkflowDiagnostic(
                    "DUPLICATE_JSON_KEY",
                    "$",
                    "unique object keys",
                    str(error)[:64].encode("utf-8", errors="replace").decode("utf-8"),
                ),
            )
        ) from None
    except (ValueError, RecursionError):
        raise WorkflowValidationError(
            (WorkflowDiagnostic("INVALID_JSON", "$", "strict JSON object; no NaN/Infinity", None),)
        ) from None
    decoder = _Decoder()
    decoded = decoder.definition(raw)
    if decoder.errors:
        raise WorkflowValidationError(tuple(decoder.errors))
    errors = _Validator(decoded, known_profiles).validate()
    if errors:
        raise WorkflowValidationError(errors)
    return WorkflowDefinition(*decoded)
