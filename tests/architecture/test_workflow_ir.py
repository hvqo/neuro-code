"""DW1 pure-domain contract, including the cross-platform Fast CI boundary."""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from neuro_code.application.agents.profiles import BUILTIN_AGENT_PROFILES
from neuro_code.domain.agents.profile import AgentCapability
from neuro_code.domain.task_dag import (
    MAX_TASK_DAG_EDGES,
    MAX_TASK_DAG_NODE_DEPENDENCIES,
    MAX_TASK_DAG_NODES,
    MAX_TASK_DAG_PARALLELISM,
)
from neuro_code.domain.workflows import (
    Activity,
    ArtifactRef,
    FieldSchema,
    InputRef,
    Literal,
    Map,
    Repeat,
    SchemaKind,
    TaskBatch,
    WorkflowValidationError,
    compile_workflow,
    validate_workflow,
)
from neuro_code.domain.workflows.definition import (
    MAX_WORKFLOW_GENERATED_TASKS,
    MAX_WORKFLOW_SOURCE_BYTES,
    workflow_payload,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "workflows"


def task(task_id="work", *, depends=()):
    return {
        "task_id": task_id,
        "prompt_template": "检查中文与 English 源码;只修改被分配的目标。",
        "profile_ref": "writable_worker",
        "required_capabilities": ["workspace.read", "workspace.write"],
        "inputs": {},
        "depends_on": list(depends),
    }


def batch(step_id="implement", tasks=None):
    return {"kind": "TaskBatch", "step_id": step_id, "tasks": tasks or [task()], "max_parallel": 1}


def ref(step_id, *path, **selectors):
    return {"kind": "result", "step_id": step_id, "field_path": list(path), **selectors}


def condition(step_id, *path, value="PASS"):
    return {"op": "eq", "ref": ref(step_id, *path), "value": value}


def activity(step_id="verify", activity_kind="parent.verify"):
    return {"kind": "Activity", "step_id": step_id, "activity": activity_kind, "inputs": {}}


def repeating(step_id="repair_loop", children=None):
    return {
        "kind": "Repeat",
        "step_id": step_id,
        "max_iterations": 3,
        "steps": children or [activity()],
        "until": condition("verify", "status"),
    }


def branching():
    return {
        "kind": "Branch",
        "step_id": "route",
        "condition": condition("verify", "status"),
        "paths": {
            "pass": [activity("accept", "parent.adopt")],
            "fail": [activity("repair", "parent.repair")],
        },
        "then_path": "pass",
        "else_path": "fail",
    }


def mapping():
    result = {
        "kind": "Map",
        "step_id": "expand",
        "collection": {"kind": "input", "field_path": ["targets"]},
        "max_items": 4,
        "batch": batch("item_batch"),
    }
    result["batch"]["tasks"][0]["inputs"] = {"target": {"kind": "item", "field_path": []}}
    return result


def source(steps=None):
    return {
        "version": 1,
        "definition_id": "dw1-example",
        "input_schema": {
            "type": "object",
            "properties": {
                "targets": {"type": "array", "items": {"type": "string"}, "max_items": 4},
                "objective": {"type": "string"},
            },
        },
        "budget": {
            "max_generated_tasks": 32,
            "max_model_calls": 100,
            "max_tool_calls": 1000,
            "max_input_tokens": 100000,
            "max_output_tokens": 20000,
            "max_wall_seconds": 600,
        },
        "steps": steps or [batch()],
        "completion_requirements": [],
    }


def compile_data(value, **kwargs):
    return compile_workflow(json.dumps(value, ensure_ascii=True), **kwargs)


def errors(value, **kwargs):
    with pytest.raises(WorkflowValidationError) as caught:
        compile_data(value, **kwargs)
    return caught.value.diagnostics


def codes(value, **kwargs):
    return {diagnostic.code for diagnostic in errors(value, **kwargs)}


@pytest.mark.parametrize("name", ["minimal", "fanout", "branch", "repeat", "map", "mixed", "cjk"])
def test_valid_fixtures_are_immutable_roundtrippable_and_profile_compatible(name):
    definition = compile_workflow(
        (FIXTURES / f"{name}.json").read_text(encoding="utf-8"),
        known_profiles=BUILTIN_AGENT_PROFILES,
    )
    assert compile_workflow(definition.canonical_json) == definition
    assert validate_workflow(definition, known_profiles=BUILTIN_AGENT_PROFILES) == ()
    assert len(definition.fingerprint) == 64
    assert definition.canonical_bytes == definition.canonical_json.encode("utf-8")
    with pytest.raises(FrozenInstanceError):
        definition.definition_id = "mutated"


def test_semantic_key_and_set_order_normalize_but_sequence_order_does_not():
    value = source([batch(tasks=[task("a"), task("b"), task("c", depends=("a", "b"))])])
    definition = compile_data(value)
    reversed_keys = json.loads(
        json.dumps(value), object_pairs_hook=lambda pairs: dict(reversed(pairs))
    )
    reversed_keys["steps"][0]["tasks"][2]["depends_on"] = ["b", "a"]
    reversed_keys["steps"][0]["tasks"][0]["required_capabilities"].reverse()
    assert compile_data(reversed_keys) == definition
    assert compile_data(reversed_keys).fingerprint == definition.fingerprint
    changed = copy.deepcopy(value)
    changed["steps"][0]["tasks"][0]["prompt_template"] += "新增目标"
    assert compile_data(changed).fingerprint != definition.fingerprint
    changed = copy.deepcopy(value)
    changed["steps"][0]["tasks"][:2] = reversed(changed["steps"][0]["tasks"][:2])
    assert compile_data(changed).fingerprint != definition.fingerprint


@pytest.mark.parametrize("seed", ["1", "42", "random"])
def test_fingerprint_golden_is_independent_of_hash_seed_and_host_paths(seed):
    recording = (FIXTURES / "mixed.json").read_text(encoding="utf-8")
    expected = (FIXTURES / "mixed.sha256").read_text(encoding="ascii").strip()
    definition = compile_workflow(recording)
    assert definition.fingerprint == expected
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from neuro_code.domain.workflows import compile_workflow; import sys; print(compile_workflow(sys.stdin.read()).fingerprint)",
        ],
        input=recording,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"},
    )
    assert completed.stdout.strip() == expected
    assert "\\u" not in definition.canonical_json


@pytest.mark.parametrize("version", [None, 0, 2, True, "1", 1.0])
def test_unsupported_versions_are_not_coerced(version):
    value = source()
    value["version"] = version
    assert "UNSUPPORTED_VERSION" in codes(value)


@pytest.mark.parametrize(
    ("wire", "code"),
    [
        ('{"version":1,"version":1}', "DUPLICATE_JSON_KEY"),
        ('{"x":NaN}', "INVALID_JSON"),
        ('{"x":Infinity}', "INVALID_JSON"),
        ('{"x":-Infinity}', "INVALID_JSON"),
        ("```json\n{}\n```", "INVALID_JSON"),
        ('{"x":1,}', "INVALID_JSON"),
        ("[1]", "INVALID_TYPE"),
        ("null", "INVALID_TYPE"),
        ("{} trailing", "INVALID_JSON"),
        ("[" * 2000, "INVALID_JSON"),
    ],
)
def test_strict_json(wire, code):
    with pytest.raises(WorkflowValidationError) as caught:
        compile_workflow(wire)
    assert code in {item.code for item in caught.value.diagnostics}


@pytest.mark.parametrize("wire", [None, b"{}", " " * (MAX_WORKFLOW_SOURCE_BYTES + 1), "\ud800"])
def test_invalid_source_is_bounded(wire):
    with pytest.raises(WorkflowValidationError) as caught:
        compile_workflow(wire)
    assert caught.value.diagnostics[0].code == "INVALID_SOURCE"


@pytest.mark.parametrize(
    "location", ["root", "budget", "node", "task", "schema", "condition", "ref", "artifact"]
)
def test_unknown_fields_at_all_boundaries(location):
    value = source([activity(), branching(), batch()])
    objects = {
        "root": value,
        "budget": value["budget"],
        "node": value["steps"][2],
        "task": value["steps"][2]["tasks"][0],
        "schema": value["input_schema"],
        "condition": value["steps"][1]["condition"],
        "ref": value["steps"][1]["condition"]["ref"],
    }
    value["steps"][0]["inputs"] = {
        "artifact": {"kind": "artifact", "artifact_id": "a", "integrity_fingerprint": "f" * 64}
    }
    objects["artifact"] = value["steps"][0]["inputs"]["artifact"]
    objects[location]["grant_permission"] = True
    assert "UNKNOWN_FIELD" in codes(value)


@pytest.mark.parametrize("kind", ["Join", "Sequence", "Parallel", "Interpreter", None, [], 5])
def test_only_five_node_kinds(kind):
    value = source()
    value["steps"][0]["kind"] = kind
    assert "UNKNOWN_NODE_KIND" in codes(value)


def test_diagnostics_collect_independent_errors_with_stable_paths():
    value = source()
    value["version"] = 99
    value["budget"]["max_model_calls"] = "10"
    value["steps"][0]["max_parallel"] = True
    actual = errors(value)
    assert {(item.code, item.path) for item in actual} >= {
        ("UNSUPPORTED_VERSION", "$.version"),
        ("INVALID_BUDGET", "$.budget.max_model_calls"),
        ("INVALID_PARALLEL_BOUND", "$.steps[0].max_parallel"),
    }
    assert errors(value) == actual
    assert all(item.expected for item in actual)


@pytest.mark.parametrize("bound", [None, "3", True, 0, -1, 4])
def test_repeat_requires_positive_explicit_policy_bound(bound):
    value = source([repeating()])
    if bound is None:
        del value["steps"][0]["max_iterations"]
        assert "UNBOUNDED_REPEAT" in codes(value)
    else:
        value["steps"][0]["max_iterations"] = bound
        assert "INVALID_REPEAT_BOUND" in codes(value)


def test_nested_repeat_is_rejected_even_inside_branch():
    branch = branching()
    branch["condition"] = condition("verify", "status")
    branch["paths"]["fail"] = [repeating("inner", [activity("inner_verify")])]
    branch["paths"]["fail"][0]["until"] = condition("inner_verify", "status")
    value = source([repeating(children=[activity(), branch])])
    assert "NESTED_REPEAT" in codes(value)


def test_global_duplicate_step_ids_and_local_duplicate_task_ids():
    assert "DUPLICATE_STEP_ID" in codes(source([activity(), activity()]))
    assert "DUPLICATE_TASK_ID" in codes(source([batch(tasks=[task(), task()])]))
    value = source([activity(), branching()])
    value["steps"][1]["paths"]["fail"][0]["step_id"] = "accept"
    assert "DUPLICATE_STEP_ID" in codes(value)


@pytest.mark.parametrize(
    ("dependencies", "code"),
    [
        (("missing",), "INVALID_DEPENDENCY"),
        (("work",), "DEPENDENCY_CYCLE"),
        (("work", "work"), "DUPLICATE_DEPENDENCY"),
        (tuple("abcde"), "INVALID_BOUND"),
    ],
)
def test_dependency_failures(dependencies, code):
    assert code in codes(source([batch(tasks=[task(depends=dependencies)])]))


def test_two_node_cycle_and_forward_dependency():
    assert "DEPENDENCY_CYCLE" in codes(
        source([batch(tasks=[task("a", depends=("b",)), task("b", depends=("a",))])])
    )
    assert compile_data(source([batch(tasks=[task("a", depends=("b",)), task("b")])]))


def test_domain_dag_limits_are_reused_at_boundaries():
    assert MAX_TASK_DAG_NODES == 8
    assert MAX_TASK_DAG_PARALLELISM == 4
    value = source([batch(tasks=[task(f"t{i}") for i in range(MAX_TASK_DAG_NODES)])])
    value["steps"][0]["max_parallel"] = MAX_TASK_DAG_PARALLELISM
    assert compile_data(value)
    value["steps"][0]["tasks"].append(task("extra"))
    assert "INVALID_BOUND" in codes(value)
    value = source()
    value["steps"][0]["max_parallel"] = MAX_TASK_DAG_PARALLELISM + 1
    assert "INVALID_PARALLEL_BOUND" in codes(value)
    tasks = [
        task(f"t{i}", depends=tuple(f"t{j}" for j in range(min(i, MAX_TASK_DAG_NODE_DEPENDENCIES))))
        for i in range(8)
    ]
    assert sum(len(t["depends_on"]) for t in tasks) > MAX_TASK_DAG_EDGES
    assert "TOO_MANY_DEPENDENCIES" in codes(source([batch(tasks=tasks)]))


@pytest.mark.parametrize("route", ["read_only", "bash", "verification", None, True])
def test_unsupported_task_route(route):
    value = source()
    value["steps"][0]["tasks"][0]["route"] = route
    assert "UNSUPPORTED_TASK_ROUTE" in codes(value)


@pytest.mark.parametrize("profile", ["", "../profile", "MAIN", "has space", "x" * 65])
def test_invalid_profile_reference(profile):
    value = source()
    value["steps"][0]["tasks"][0]["profile_ref"] = profile
    assert "INVALID_IDENTIFIER" in codes(value)


def test_profile_intent_contradictions_and_custom_profile_not_authority():
    value = source()
    value["steps"][0]["tasks"][0]["profile_ref"] = "explorer"
    assert "PROFILE_ROUTE_CONTRADICTION" in codes(value)
    value["steps"][0]["tasks"][0]["profile_ref"] = "writable_worker"
    value["steps"][0]["tasks"][0]["required_capabilities"].append("shell.execute")
    assert "PROFILE_CAPABILITY_CONTRADICTION" in codes(value, known_profiles=BUILTIN_AGENT_PROFILES)
    value["steps"][0]["tasks"][0]["profile_ref"] = "custom-worker"
    assert compile_data(value)  # Pure intent accepted, no claim of grant/availability.
    value["steps"][0]["tasks"][0]["required_capabilities"].append("root.permission")
    assert "INVALID_CAPABILITY" in codes(value)


def test_duplicate_capability_and_unsupported_activity():
    value = source()
    value["steps"][0]["tasks"][0]["required_capabilities"] = ["workspace.read"] * 2
    assert "DUPLICATE_CAPABILITY" in codes(value)
    assert "UNSUPPORTED_ACTIVITY" in codes(source([activity(activity_kind="exec")]))


@pytest.mark.parametrize("target", ["missing", "pass"])
def test_branch_targets(target):
    value = source([activity(), branching()])
    value["steps"][1]["else_path"] = target
    assert "ILLEGAL_BRANCH_TARGET" in codes(value)


def test_branch_result_dominance_and_no_none_masking_even_for_exists():
    value = source([activity(), branching(), activity("later")])
    value["steps"][2]["inputs"] = {"unconditional": ref("accept", "status")}
    assert "NON_DOMINATING_RESULT" in codes(value)
    value["steps"][2]["inputs"] = {}
    value["completion_requirements"] = [{"op": "exists", "ref": ref("accept", "status")}]
    assert "NON_DOMINATING_RESULT" in codes(value)
    value["completion_requirements"] = [condition("verify", "status")]
    assert compile_data(value)


def test_future_and_nonexistent_outputs_and_invalid_paths():
    value = source([activity("early"), activity("late")])
    value["steps"][0]["inputs"] = {"x": ref("late", "status")}
    assert "NON_DOMINATING_RESULT" in codes(value)
    value["steps"][0]["inputs"] = {"x": ref("absent", "status")}
    assert "INVALID_REFERENCE" in codes(value)
    value["steps"][0]["inputs"] = {}
    value["steps"][1]["inputs"] = {"x": ref("early", "not_an_output")}
    assert "INVALID_RESULT_PATH" in codes(value)
    value["steps"][1]["inputs"] = {"x": ref("early", "status", "nested")}
    assert "INVALID_RESULT_PATH" in codes(value)


def test_repeat_scope_and_selectors_only_guaranteed_first_iteration():
    value = source([repeating(), activity("after")])
    value["steps"][1]["inputs"] = {"x": ref("verify", "status")}
    assert "NON_DOMINATING_RESULT" in codes(value)
    value["steps"][1]["inputs"] = {"x": ref("repair_loop", "last", "verify", "status")}
    assert compile_data(value)
    value["steps"][1]["inputs"] = {"x": ref("repair_loop", "verify", "status", iteration=1)}
    assert compile_data(value)
    value["steps"][1]["inputs"]["x"]["iteration"] = 2
    assert "UNPROVEN_RESULT_SELECTOR" in codes(value)
    value["steps"][1]["inputs"] = {"x": ref("repair_loop", "last", item_key="a")}
    assert "UNPROVEN_RESULT_SELECTOR" in codes(value)


@pytest.mark.parametrize("op", ["regex", "eval", "and", True, "__import__"])
def test_no_condition_language(op):
    value = source([activity(), branching()])
    value["steps"][1]["condition"]["op"] = op
    assert "INVALID_CONDITION" in codes(value)


def test_condition_scalar_types_not_coerced_and_exists_no_value():
    value = source([activity()])
    value["completion_requirements"] = [condition("verify", "workspace_generation", value=True)]
    assert "CONDITION_TYPE_MISMATCH" in codes(value)
    value["completion_requirements"] = [condition("verify", value="x")]
    assert "CONDITION_TYPE_MISMATCH" in codes(value)
    value["completion_requirements"] = [
        {"op": "exists", "ref": ref("verify", "status"), "value": True}
    ]
    assert "UNKNOWN_FIELD" in codes(value)


def test_map_bounds_item_scope_and_nested_expansion():
    value = source([mapping()])
    assert isinstance(compile_data(value).steps[0], Map)
    value["steps"][0]["max_items"] = 3
    assert "MAP_OVER_BOUND" in codes(value)
    value["steps"][0]["max_items"] = 4
    value["steps"][0]["batch"]["tasks"] = [task("a"), task("b"), task("c")]
    assert "MAP_OVER_BOUND" in codes(value)
    value = source([mapping()])
    value["steps"][0]["batch"] = mapping()
    assert "NESTED_MAP" in codes(value)
    value["steps"][0]["batch"] = repeating()
    assert "ILLEGAL_MAP_BODY" in codes(value)
    value = source()
    value["steps"][0]["tasks"][0]["inputs"] = {"x": {"kind": "item", "field_path": []}}
    assert "INVALID_REFERENCE" in codes(value)


def test_map_nonarray_collection_and_template_result_not_published():
    value = source([mapping(), activity("after")])
    value["steps"][0]["collection"]["field_path"] = ["objective"]
    assert "INVALID_MAP_COLLECTION" in codes(value)
    value["steps"][0]["collection"]["field_path"] = ["targets"]
    value["steps"][1]["inputs"] = {"x": ref("item_batch", "tasks")}
    assert "NON_DOMINATING_RESULT" in codes(value)
    value["steps"][1]["inputs"] = {"x": ref("expand", "items")}
    assert compile_data(value)


@pytest.mark.parametrize(
    "budget_name",
    [
        "max_generated_tasks",
        "max_model_calls",
        "max_tool_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_wall_seconds",
    ],
)
@pytest.mark.parametrize("invalid", [0, -1, "100", True, None, 2**31])
def test_all_budget_declarations_positive_bounded_and_strict(budget_name, invalid):
    value = source()
    value["budget"][budget_name] = invalid
    assert "INVALID_BUDGET" in codes(value)


def test_worst_case_branch_max_repeat_product_and_total_sum():
    fanout = batch("many", [task(f"t{i}") for i in range(8)])
    loop = repeating(children=[fanout, activity()])
    value = source([loop, batch("after", [task(f"a{i}") for i in range(8)])])
    assert compile_data(value)
    value["steps"].append(batch("overflow"))
    assert "STRUCTURAL_EXPANSION_OVERFLOW" in codes(value)
    branch = branching()
    branch["paths"]["pass"] = [fanout]
    branch["paths"]["fail"] = [batch("other", [task(f"z{i}") for i in range(8)])]
    value = source([activity(), branch])
    value["budget"]["max_generated_tasks"] = 8
    assert compile_data(value)  # max(path costs), not sum of mutually exclusive paths.
    value["budget"]["max_generated_tasks"] = 7
    assert "STRUCTURAL_EXPANSION_OVERFLOW" in codes(value)
    assert MAX_WORKFLOW_GENERATED_TASKS == 32


def test_artifact_identity_is_integrity_data_only_and_payload_detached():
    value = source([activity()])
    value["steps"][0]["inputs"] = {
        "artifact": {
            "kind": "artifact",
            "artifact_id": "patch-a",
            "integrity_fingerprint": "a" * 64,
            "workspace_identity": "opaque-workspace-identity",
        },
        "literal": {"kind": "literal", "value": "你好"},
    }
    definition = compile_data(value)
    artifact = dict(definition.steps[0].inputs)["artifact"]
    assert isinstance(artifact, ArtifactRef)
    projected = workflow_payload(definition)
    projected["steps"].clear()
    assert definition.steps
    value["steps"][0]["inputs"]["artifact"]["integrity_fingerprint"] = "abc"
    assert "INVALID_ARTIFACT_FINGERPRINT" in codes(value)


def test_direct_typed_construction_validates_and_cannot_hide_mutable_data():
    definition = compile_data(source())
    with pytest.raises(WorkflowValidationError, match="INVALID_IR"):
        replace(definition, steps=list(definition.steps))
    with pytest.raises(WorkflowValidationError, match="NONCANONICAL_IR"):
        replace(
            definition,
            steps=(
                replace(
                    definition.steps[0],
                    tasks=(
                        replace(
                            definition.steps[0].tasks[0],
                            required_capabilities=(
                                AgentCapability.WORKSPACE_WRITE,
                                AgentCapability.WORKSPACE_READ,
                            ),
                        ),
                    ),
                ),
            ),
        )
    with pytest.raises(WorkflowValidationError, match="UNSUPPORTED_VERSION"):
        replace(definition, version=True)
    with pytest.raises(ValueError, match="array schema"):
        FieldSchema(SchemaKind.ARRAY, items=FieldSchema(SchemaKind.STRING), max_items=0)
    assert isinstance(definition.steps[0], TaskBatch)
    assert isinstance(compile_data(source([repeating()])).steps[0], Repeat)
    assert isinstance(compile_data(source([activity()])).steps[0], Activity)


def test_domain_module_imports_no_application_runtime_or_io_and_has_no_execution_entry():
    import neuro_code.domain.workflows as workflows

    package = Path(workflows.__file__).parent
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(
                    ("neuro_code.application", "neuro_code.infrastructure", "neuro_code.interfaces")
                )
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name in {"os", "pathlib", "socket", "subprocess", "asyncio"}
                    for alias in node.names
                )
    assert not any(
        "run" in name.lower() or "execute" in name.lower() or "interpreter" in name.lower()
        for name in workflows.__all__
    )
    assert Literal(1) != InputRef(("x",))


@pytest.mark.parametrize("literal", [None, 1.5, [], {}, "\ud800", "\x00", "x" * 8193, 2**31])
def test_literal_bounds_and_scalar_wire_types(literal):
    value = source()
    value["steps"][0]["tasks"][0]["inputs"] = {"x": {"kind": "literal", "value": literal}}
    assert "INVALID_LITERAL" in codes(value)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "float"},
        {"type": "string", "items": {"type": "string"}},
        {"type": "array", "max_items": 0, "items": {"type": "integer"}},
        {"type": "object", "properties": {"bad[]": {"type": "boolean"}}},
        {"type": "object", "properties": []},
    ],
)
def test_schema_is_closed_and_bounded(schema):
    value = source()
    value["input_schema"]["properties"]["x"] = schema
    assert errors(value)


def test_schema_and_control_depth_and_total_declarations_are_bounded():
    value = source()
    schema = {"type": "string"}
    for _ in range(10):
        schema = {"type": "object", "properties": {"nested": schema}}
    value["input_schema"] = schema
    assert "STRUCTURE_TOO_DEEP" in codes(value)
    value = source([activity()])
    value["input_schema"]["properties"] = {f"field{i}": {"type": "string"} for i in range(65)}
    assert "INVALID_BOUND" in codes(value)
    value = source([activity()])
    nested = activity("leaf")
    for i in range(10):
        nested = {
            "kind": "Branch",
            "step_id": f"branch{i}",
            "condition": condition("verify", "status"),
            "paths": {"yes": [nested], "no": []},
            "then_path": "yes",
            "else_path": "no",
        }
    value["steps"].append(nested)
    assert "STRUCTURE_TOO_DEEP" in codes(value)
    value = source([activity("verify"), branching()])
    value["steps"][1]["paths"]["pass"] = [activity(f"a{i}") for i in range(33)]
    value["steps"][1]["paths"]["fail"] = [activity(f"b{i}") for i in range(33)]
    assert "TOO_MANY_STEPS" in codes(value)


@pytest.mark.parametrize("name", ["bad[]", "invalid name", "变量"])
def test_binding_and_reference_path_names_are_not_expressions(name):
    value = source()
    value["steps"][0]["tasks"][0]["inputs"] = {name: {"kind": "input", "field_path": [name]}}
    assert "INVALID_IDENTIFIER" in codes(value)
    assert "INVALID_FIELD_PATH" in codes(value)


def test_repeat_unproven_selectors_and_map_reference_constraints():
    value = source([activity(), activity("after")])
    value["steps"][1]["inputs"] = {"x": ref("verify", "status", iteration=1)}
    assert "UNPROVEN_RESULT_SELECTOR" in codes(value)
    value = source([mapping()])
    value["steps"][0]["collection"] = {"kind": "literal", "value": "targets"}
    assert "INVALID_REFERENCE" in codes(value)
    value["steps"][0]["collection"] = {"kind": "input", "field_path": ["absent"]}
    assert "INVALID_RESULT_PATH" in codes(value)


def test_duplicate_profile_catalog_is_not_last_writer_wins():
    profiles = (BUILTIN_AGENT_PROFILES[0], BUILTIN_AGENT_PROFILES[0])
    assert "INVALID_PROFILE_CATALOG" in codes(source(), known_profiles=profiles)
    definition = compile_data(source())
    assert (
        validate_workflow(definition, known_profiles=profiles)[0].code == "INVALID_PROFILE_CATALOG"
    )


def test_budget_or_unknown_fields_cannot_elevate_authority():
    value = source()
    value["budget"]["override_global"] = True
    value["steps"][0]["tasks"][0]["permission"] = "bypass"
    value["steps"][0]["tasks"][0]["sandbox"] = "disable"
    diagnostics = errors(value)
    assert sum(item.code == "UNKNOWN_FIELD" for item in diagnostics) == 3


def test_template_is_opaque_bounded_text_not_evaluated():
    value = source()
    value["steps"][0]["tasks"][0]["prompt_template"] = (
        "__import__('os').system('bad') ${unbound} {not_an_expression}"
    )
    definition = compile_data(value)
    assert (
        definition.steps[0].tasks[0].prompt_template
        == value["steps"][0]["tasks"][0]["prompt_template"]
    )
    for text in ("\ud800", "secret-token\x00", "a" * 8193, "\x1b[31m", "\x7f"):
        value["steps"][0]["tasks"][0]["prompt_template"] = text
        diagnostics = errors(value)
        assert "INVALID_TEXT" in {item.code for item in diagnostics}
        assert not any("secret-token" in str(item.actual) for item in diagnostics)


def test_valid_hyphenated_task_output_and_map_object_item():
    value = source([batch(tasks=[task("patch-one")]), activity()])
    value["steps"][1]["inputs"] = {"result": ref("implement", "tasks", "patch-one", "status")}
    assert compile_data(value)
    value = source([mapping()])
    value["input_schema"]["properties"]["targets"]["items"] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }
    value["steps"][0]["batch"]["tasks"][0]["inputs"]["target"]["field_path"] = ["path"]
    assert compile_data(value)


def test_decoder_mutation_invariant_has_no_unstructured_failures():
    # Deterministic schema-boundary mutation, not a new property-test framework.
    original = json.loads((FIXTURES / "mixed.json").read_text(encoding="utf-8"))
    original["steps"].append(mapping())

    def walk(value, path=()):
        yield path
        if isinstance(value, dict):
            for key, child in value.items():
                yield from walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, (*path, index))

    for path in walk(original):
        for replacement in (None, True, 1, "unknown", [], {}):
            value = copy.deepcopy(original)
            if path:
                parent = value
                for part in path[:-1]:
                    parent = parent[part]
                parent[path[-1]] = replacement
            else:
                value = replacement
            failure = None
            try:
                definition = compile_data(value)
            except WorkflowValidationError as error:
                failure = error
            if failure is not None:
                assert failure.diagnostics
            else:
                assert compile_workflow(definition.canonical_json) == definition


def test_diagnostic_paths_escape_untrusted_property_names():
    value = source()
    value["bad.key[]"] = True
    diagnostic = next(item for item in errors(value) if item.code == "UNKNOWN_FIELD")
    assert diagnostic.path == '$["bad.key[]"]'
    value = source()
    value["\ud800"] = True
    diagnostics = errors(value)
    json.dumps([item.path for item in diagnostics], ensure_ascii=False).encode("utf-8")


def test_normalization_never_accepts_an_ir_that_cannot_roundtrip_its_size_bound():
    value = source()
    # Many literal bindings approach the source ceiling. Normalizing omitted route
    # must not produce accepted canonical bytes beyond the decoder's limit.
    value["steps"][0]["tasks"][0]["inputs"] = {
        f"p{i}": {"kind": "literal", "value": "x" * 8000} for i in range(16)
    }
    definition = compile_data(value)
    assert compile_workflow(definition.canonical_json) == definition


def test_invalid_dependency_diagnostic_preserves_original_source_array_index():
    value = source([batch(tasks=[task("a"), task("b", depends=("a", "missing"))])])
    diagnostic = next(item for item in errors(value) if item.code == "INVALID_DEPENDENCY")
    assert diagnostic.path == "$.steps[0].tasks[1].depends_on[1]"
