"""DW5a deterministic facts explicitly do not claim adoption or verification."""

from neuro_code.application.ports.workflow_interpreter import FakeActivityInvocation
from neuro_code.domain.workflows.definition import ActivityKind
from neuro_code.domain.workflows.publication import canonical


class DeterministicFakeWorkflowActivity:
    def evaluate(self, invocation: FakeActivityInvocation) -> str:
        match invocation.activity:
            case ActivityKind.ADOPT:
                return canonical({"status": "fake", "parent_workspace_changed": False})
            case ActivityKind.VERIFY:
                return canonical(
                    {"status": "fake", "workspace_generation": invocation.step.iteration}
                )
            case ActivityKind.REPAIR:
                return canonical({"status": "fake", "response": "No repair executed"})
