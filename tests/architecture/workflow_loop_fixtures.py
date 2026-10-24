"""Canonical DW1 Branch-chain fixtures for the bounded verification loop."""

from __future__ import annotations

from neuro_code.domain.workflows.definition import compile_workflow
from neuro_code.domain.workflows.publication import canonical
from tests.architecture.test_workflow_interpreter import source


def result_ref(step_id: str, field: str = "status") -> dict[str, object]:
    return {"kind": "result", "step_id": step_id, "field_path": [field]}


def verification_activity(step_id: str, after_repair: str | None = None) -> dict[str, object]:
    inputs: dict[str, object] = {}
    if after_repair is not None:
        inputs["after_repair"] = result_ref(after_repair)
    return {
        "kind": "Activity",
        "step_id": step_id,
        "activity": "parent.verify",
        "inputs": inputs,
    }


def repair_activity(step_id: str, failed_verify: str) -> dict[str, object]:
    return {
        "kind": "Activity",
        "step_id": step_id,
        "activity": "parent.repair",
        "inputs": {"failure": result_ref(failed_verify)},
    }


def branch(step_id: str, verify_id: str, fail_path: list[dict[str, object]]) -> dict[str, object]:
    return {
        "kind": "Branch",
        "step_id": step_id,
        "condition": {
            "op": "eq",
            "ref": result_ref(verify_id),
            "value": "PASS",
        },
        "paths": {"pass": [], "fail": fail_path},
        "then_path": "pass",
        "else_path": "fail",
    }


def bounded_loop_source(verify_count: int = 3) -> dict[str, object]:
    if not 1 <= verify_count <= 3:
        raise ValueError("fixture supports one to three VERIFY steps")

    def route(index: int) -> dict[str, object]:
        verify_id = f"verify_{index}"
        if index == verify_count:
            fail_path: list[dict[str, object]] = []
        else:
            repair_id = f"repair_{index}"
            next_verify = f"verify_{index + 1}"
            fail_path = [
                repair_activity(repair_id, verify_id),
                verification_activity(next_verify, repair_id),
                route(index + 1),
            ]
        return branch(f"route_{index}", verify_id, fail_path)

    result = source()
    result["definition_id"] = "bounded-verification-loop"
    result["steps"] = [verification_activity("verify_1"), route(1)]
    return result


def compile_bounded_loop(verify_count: int = 3):
    return compile_workflow(canonical(bounded_loop_source(verify_count)))
