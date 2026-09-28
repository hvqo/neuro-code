from __future__ import annotations

import ast
import unittest
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from neuro_code.application.agents.binding import (
    ALL_AGENT_CAPABILITIES,
    apply_reasoning_policy,
    provider_available_capabilities,
    resolve_agent_binding,
)
from neuro_code.application.agents.composition import bind_agent_profile
from neuro_code.application.agents.profiles import (
    BUILTIN_AGENT_PROFILES,
    EXPLORER_AGENT_PROFILE,
    LEADER_AGENT_PROFILE,
    MAIN_AGENT_PROFILE,
    PLANNER_AGENT_PROFILE,
    REVIEWER_AGENT_PROFILE,
    WRITABLE_WORKER_AGENT_PROFILE,
    builtin_agent_profile,
)
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionManager,
    PermissionRule,
)
from neuro_code.application.ports.agent_profiles import EffectiveAgentBinding
from neuro_code.application.ports.model import (
    ModelCapability,
    ModelCapabilitySet,
    ModelEvent,
    ModelToolPolicy,
)
from neuro_code.application.ports.tools import Tool, ToolContext
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.runtime.profile_tools import ProfileBoundToolCollection
from neuro_code.domain.agents.profile import (
    AgentBehaviorPolicy,
    AgentCapability,
    AgentContextPolicy,
    AgentMemoryPolicy,
    AgentModelPolicy,
    AgentProfile,
    AgentProfileOverride,
    AgentRole,
    AgentWorkspacePolicy,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelCompleted
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import ExecutionBudget
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import ConfigurationError
from tests.fakes import EmptyWorkspaceChangeObserver


def _budget(calls: int = 48) -> ExecutionBudget:
    return ExecutionBudget(
        max_model_calls=calls,
        max_tool_rounds=calls,
        max_tool_calls=calls * 4,
        max_calls_per_tool=calls,
        max_wall_seconds=None,
        max_input_tokens=None,
        max_output_tokens=None,
        max_total_tokens=None,
    )


def _single_capability_profile(capability: AgentCapability) -> AgentProfile:
    return AgentProfile(
        profile_id="test.search",
        name="Test profile",
        description="Bounded fixture profile.",
        role=AgentRole.EXPLORER,
        behavior=AgentBehaviorPolicy.READ_ONLY_EXPLORATION,
        capability_policy=frozenset({capability}),
        model_policy=AgentModelPolicy(),
        memory_policy=AgentMemoryPolicy(),
        context_policy=AgentContextPolicy(),
        workspace_policy=AgentWorkspacePolicy(read=False),
    )


class _Provider:
    provider_name = "fixture-provider"
    model_name = "fixture-model"
    context_affinity = "fixture:profile"

    def __init__(self, capabilities: ModelCapabilitySet | None = None) -> None:
        self.capabilities = capabilities or ModelCapabilitySet.all_unknown()

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy
        if False:
            yield ModelCompleted("stop")


class _ToolFixture:
    side_effecting = False

    def __init__(self, name: str) -> None:
        self.definition = ToolDefinition(name, "fixture", {"type": "object"})
        self.calls = 0

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        del arguments, context
        self.calls += 1
        return ToolResult("ok")


class _ToolsFixture:
    def __init__(self, *names: str) -> None:
        self.tools = {name: _ToolFixture(name) for name in names}

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self.tools.values())

    def has_synthetic_intent(self, name: str) -> bool:
        return False

    def replace_external(self, tools: Collection[Tool], previous_names: Collection[str]) -> None:
        for name in previous_names:
            self.tools.pop(name, None)
        for tool in tools:
            self.tools[tool.definition.name] = tool


def _resolve(
    profile: AgentProfile,
    *,
    runtime: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    provider: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    platform: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    permission: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    security: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    parent: Collection[AgentCapability] = ALL_AGENT_CAPABILITIES,
    override: AgentProfileOverride | None = None,
    tool_names: Collection[str] = ("web_search",),
    denied_tool_names: Collection[str] = (),
) -> EffectiveAgentBinding:
    return resolve_agent_binding(
        profile=profile,
        provider_name="fixture-provider",
        model_name="fixture-model",
        reasoning_effort=ReasoningEffort.MAX,
        tool_names=tool_names,
        runtime_capabilities=runtime,
        provider_capabilities=provider,
        platform_capabilities=platform,
        permission_capabilities=permission,
        security_capabilities=security,
        parent_capabilities=parent,
        permission_denied_tool_names=denied_tool_names,
        execution_budget=_budget(),
        sandbox_profile=SandboxProfile.WORKSPACE,
        override=override,
    )


class AgentProfileDomainTests(unittest.TestCase):
    def test_builtin_profiles_are_typed_bounded_and_resolvable_by_exact_id(self) -> None:
        self.assertEqual(
            {profile.profile_id for profile in BUILTIN_AGENT_PROFILES},
            {"main", "explorer", "planner", "reviewer", "writable_worker", "leader"},
        )
        self.assertIs(builtin_agent_profile("main"), MAIN_AGENT_PROFILE)
        self.assertIs(builtin_agent_profile("explorer"), EXPLORER_AGENT_PROFILE)
        self.assertIs(builtin_agent_profile("planner"), PLANNER_AGENT_PROFILE)
        self.assertIs(builtin_agent_profile("reviewer"), REVIEWER_AGENT_PROFILE)
        self.assertIs(builtin_agent_profile("writable_worker"), WRITABLE_WORKER_AGENT_PROFILE)
        self.assertIs(builtin_agent_profile("leader"), LEADER_AGENT_PROFILE)
        with self.assertRaisesRegex(ValueError, "unknown built-in"):
            builtin_agent_profile("missing")

    def test_invalid_profile_and_unstable_context_contract_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            AgentProfile(
                profile_id="../escape",
                name="Invalid",
                description="Invalid profile.",
                role=AgentRole.EXPLORER,
                behavior=AgentBehaviorPolicy.STANDARD,
                capability_policy=frozenset(),
            )
        with self.assertRaisesRegex(ValueError, "stable-prefix"):
            AgentContextPolicy(preserve_stable_prefix=False)
        with self.assertRaises(ConfigurationError):
            _resolve(None)  # type: ignore[arg-type]

    def test_profile_scope_and_write_intent_are_consistent(self) -> None:
        with self.assertRaisesRegex(ValueError, "write capability and write policy"):
            AgentProfile(
                profile_id="invalid.write",
                name="Invalid write profile",
                description="No write policy.",
                role=AgentRole.EXPLORER,
                behavior=AgentBehaviorPolicy.STANDARD,
                capability_policy=frozenset({AgentCapability.WORKSPACE_WRITE}),
            )


class CapabilityResolutionTests(unittest.TestCase):
    def test_effective_capability_is_exact_intersection_with_typed_reason(self) -> None:
        profile = _single_capability_profile(AgentCapability.WEB_SEARCH)
        all_caps = set(ALL_AGENT_CAPABILITIES)
        axes = (
            ("runtime", {AgentCapability.WEB_SEARCH}, "runtime_unavailable"),
            ("provider", {AgentCapability.WEB_SEARCH}, "provider_unavailable"),
            ("platform", {AgentCapability.WEB_SEARCH}, "platform_unavailable"),
            ("permission", {AgentCapability.WEB_SEARCH}, "permission_policy"),
            ("security", {AgentCapability.WEB_SEARCH}, "sandbox_policy"),
            ("parent", {AgentCapability.WEB_SEARCH}, "parent_policy"),
        )
        for axis, values, expected_reason in axes:
            with self.subTest(axis=axis):
                kwargs = dict.fromkeys(
                    ("runtime", "provider", "platform", "permission", "security", "parent"),
                    all_caps,
                )
                kwargs[axis] = all_caps - values
                binding = _resolve(profile, **kwargs)
                self.assertNotIn(AgentCapability.WEB_SEARCH, binding.effective_capabilities)
                unavailable = {
                    item.capability: item.reason.value
                    for item in binding.capability_resolution.unavailable
                }
                self.assertEqual(unavailable[AgentCapability.WEB_SEARCH], expected_reason)

        override = AgentProfileOverride(
            disabled_capabilities=frozenset({AgentCapability.WEB_SEARCH})
        )
        binding = _resolve(profile, override=override)
        unavailable = {
            item.capability: item.reason.value for item in binding.capability_resolution.unavailable
        }
        self.assertEqual(unavailable[AgentCapability.WEB_SEARCH], "profile_override")

    def test_read_only_child_cannot_escalate_write_from_parent(self) -> None:
        binding = _resolve(
            WRITABLE_WORKER_AGENT_PROFILE,
            tool_names=("read_file", "apply_patch", "search_replace"),
            parent=ALL_AGENT_CAPABILITIES - {AgentCapability.WORKSPACE_WRITE},
        )
        self.assertNotIn(AgentCapability.WORKSPACE_WRITE, binding.effective_capabilities)
        self.assertEqual(binding.bound_tool_names, ("read_file",))
        reasons = {
            item.capability: item.reason.value for item in binding.capability_resolution.unavailable
        }
        self.assertEqual(reasons[AgentCapability.WORKSPACE_WRITE], "parent_policy")

    def test_sandbox_and_managed_workspace_bound_write_capability(self) -> None:
        profile = WRITABLE_WORKER_AGENT_PROFILE
        all_caps = set(ALL_AGENT_CAPABILITIES)
        profile_context = resolve_agent_binding(
            profile=profile,
            provider_name="fixture-provider",
            model_name="fixture-model",
            reasoning_effort=ReasoningEffort.HIGH,
            tool_names=("read_file", "apply_patch"),
            runtime_capabilities=all_caps,
            provider_capabilities=all_caps,
            platform_capabilities=all_caps - {AgentCapability.WORKSPACE_WRITE},
            execution_budget=_budget(),
            sandbox_profile=SandboxProfile.READ_ONLY,
        )
        self.assertNotIn(AgentCapability.WORKSPACE_WRITE, profile_context.effective_capabilities)
        self.assertEqual(profile_context.bound_tool_names, ("read_file",))

    def test_provider_search_is_fail_closed_and_native_requires_explicit_capability(self) -> None:
        local_tool_names = ("web_search",)
        self.assertNotIn(
            AgentCapability.WEB_SEARCH,
            provider_available_capabilities(
                tool_names=local_tool_names,
                provider_capabilities=ModelCapabilitySet.all_unknown(),
            ),
        )
        function_tool_provider = ModelCapabilitySet.from_supported(ModelCapability.FUNCTION_TOOLS)
        self.assertIn(
            AgentCapability.WEB_SEARCH,
            provider_available_capabilities(
                tool_names=local_tool_names,
                provider_capabilities=function_tool_provider,
            ),
        )
        self.assertIn(
            AgentCapability.WEB_SEARCH,
            provider_available_capabilities(
                tool_names=(),
                provider_tool_names=("web_search",),
                provider_capabilities=ModelCapabilitySet.from_supported(
                    ModelCapability.HOSTED_WEB_SEARCH
                ),
            ),
        )
        self.assertNotIn(
            AgentCapability.WEB_SEARCH,
            provider_available_capabilities(
                tool_names=(),
                provider_tool_names=("web_search",),
                provider_capabilities=ModelCapabilitySet.all_unknown(),
            ),
        )

    def test_profile_reasoning_policy_preserves_ultracode_application_strategy(self) -> None:
        self.assertIs(
            apply_reasoning_policy(ReasoningEffort.ULTRACODE, MAIN_AGENT_PROFILE),
            ReasoningEffort.ULTRACODE,
        )

    def test_override_only_narrows_budget_and_binding_fingerprint_is_deterministic(self) -> None:
        profile = MAIN_AGENT_PROFILE
        override = AgentProfileOverride(
            disabled_capabilities=frozenset({AgentCapability.WORKSPACE_WRITE}),
            execution_budget_ceiling=_budget(9),
        )
        first = _resolve(
            profile,
            override=override,
            tool_names=("read_file", "apply_patch"),
        )
        second = _resolve(
            profile,
            override=override,
            tool_names=("read_file", "apply_patch"),
        )
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertLessEqual(first.execution_budget.max_model_calls, 9)
        self.assertNotIn("apply_patch", first.bound_tool_names)
        altered_guidance = AgentProfile(
            profile_id=profile.profile_id,
            name=profile.name,
            description=profile.description,
            role=profile.role,
            behavior=profile.behavior,
            capability_policy=profile.capability_policy,
            system_guidance="Different stable guidance.",
            model_policy=profile.model_policy,
            reasoning_policy=profile.reasoning_policy,
            memory_policy=profile.memory_policy,
            context_policy=profile.context_policy,
            execution_budget_ceiling=profile.execution_budget_ceiling,
            subagent_policy=profile.subagent_policy,
            workspace_policy=profile.workspace_policy,
            verification_policy=profile.verification_policy,
        )
        changed_binding = _resolve(
            altered_guidance,
            override=override,
            tool_names=("read_file", "apply_patch"),
        )
        self.assertNotEqual(first.fingerprint, changed_binding.fingerprint)

    def test_trace_projection_contains_only_safe_profile_metadata(self) -> None:
        profile = AgentProfile(
            profile_id="test.trace",
            name="Trace profile",
            description="Fixture",
            role=AgentRole.EXPLORER,
            behavior=AgentBehaviorPolicy.READ_ONLY_EXPLORATION,
            capability_policy=frozenset({AgentCapability.WEB_SEARCH}),
            system_guidance="PRIVATE_GUIDANCE_SENTINEL",
        )
        binding = _resolve(profile)
        metadata = str(binding.to_trace_mapping())
        self.assertIn("profile_id", metadata)
        self.assertIn("web.search", metadata)
        self.assertNotIn("PRIVATE_GUIDANCE_SENTINEL", metadata)
        self.assertNotIn("api_key", metadata)


class RuntimeCapabilityBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_bound_tool_schema_and_execution_fail_closed_without_scope(self) -> None:
        base_tools = _ToolsFixture("read_project_memory")
        memory_tool = base_tools.tools["read_project_memory"]
        blocked = ProfileBoundToolCollection(
            base_tools,
            (),
            effective_capabilities=(),
        )
        self.assertEqual(
            tuple(definition.name for definition in blocked.definitions()),
            ("read_project_memory",),
        )
        exposed = blocked.get("read_project_memory")
        self.assertIsNotNone(exposed)
        result = await exposed.execute({}, ToolContext(Path("/workspace")))
        self.assertTrue(result.is_error)
        self.assertEqual(memory_tool.calls, 0)

    async def test_extension_refresh_requires_the_bound_extension_capability(self) -> None:
        original = _ToolFixture("read_file")
        external = _ToolFixture("mcp_external")
        read_only = ProfileBoundToolCollection(
            _ToolsFixture("read_file"),
            ("read_file",),
            effective_capabilities=(AgentCapability.WORKSPACE_READ,),
        )
        with self.assertRaisesRegex(TypeError, "does not allow external"):
            read_only.replace_external((external,), ())

        extensible = ProfileBoundToolCollection(
            _ToolsFixture("read_file"),
            ("read_file",),
            effective_capabilities=(
                AgentCapability.WORKSPACE_READ,
                AgentCapability.EXTENSION_INVOKE,
            ),
        )
        extensible.replace_external((external,), ())
        self.assertEqual(
            {definition.name for definition in extensible.definitions()},
            {"read_file", "mcp_external"},
        )
        self.assertIsNotNone(original)

    async def test_extension_refresh_cannot_reintroduce_filtered_builtin_tools(self) -> None:
        base_tools = _ToolsFixture("read_file", "bash", "apply_patch")
        profile_bound = ProfileBoundToolCollection(
            base_tools,
            ("read_file",),
            effective_capabilities=(
                AgentCapability.WORKSPACE_READ,
                AgentCapability.EXTENSION_INVOKE,
            ),
        )
        profile_bound.replace_external((_ToolFixture("mcp_external"),), ())
        exposed_names = {definition.name for definition in profile_bound.definitions()}
        self.assertEqual(exposed_names, {"read_file", "mcp_external"})
        self.assertIsNone(profile_bound.get("bash"))
        self.assertIsNone(profile_bound.get("apply_patch"))

    async def test_extension_refresh_can_remove_existing_tools_without_extension_authority(
        self,
    ) -> None:
        base_tools = _ToolsFixture("read_file", "mcp_external")
        profile_bound = ProfileBoundToolCollection(
            base_tools,
            ("read_file", "mcp_external"),
            effective_capabilities=(
                AgentCapability.WORKSPACE_READ,
                AgentCapability.EXTENSION_INVOKE,
            ),
        )
        profile_bound.replace_external((), ("mcp_external",))
        self.assertEqual(
            {definition.name for definition in profile_bound.definitions()},
            {"read_file"},
        )

    async def test_agent_runtime_refresh_uses_the_profile_bound_tool_catalog(self) -> None:
        tools = _ToolsFixture("read_file", "bash", "apply_patch")
        override = AgentProfileOverride(
            disabled_capabilities=frozenset(
                {AgentCapability.WORKSPACE_WRITE, AgentCapability.SHELL_EXECUTE}
            )
        )
        binding = _resolve(
            MAIN_AGENT_PROFILE,
            tool_names=("read_file", "bash", "apply_patch"),
            override=override,
        )
        runtime = AgentRuntime(
            provider=_Provider(),
            tools=tools,
            workspace_change_observer=EmptyWorkspaceChangeObserver(),
            permissions=PermissionManager(),
            tool_context=ToolContext(Path("/workspace")),
            execution_budget=_budget(),
            reasoning_effort=ReasoningEffort.HIGH,
            effective_agent_binding=binding,
        )
        runtime.replace_external_tools((_ToolFixture("mcp_external"),), ())
        self.assertEqual(
            {definition.name for definition in runtime._tools.definitions()},
            {"read_file", "mcp_external"},
        )
        self.assertIsNone(runtime._tools.get("bash"))
        self.assertIsNone(runtime._tools.get("apply_patch"))

    async def test_read_only_profile_hides_write_tools_and_writable_worker_binds_managed_write(
        self,
    ) -> None:
        names = ("read_file", "apply_patch", "search_replace")
        read_only = _resolve(EXPLORER_AGENT_PROFILE, tool_names=names)
        self.assertEqual(read_only.bound_tool_names, ("read_file",))

        tools = _ToolsFixture(*names)
        provider = _Provider()
        denied_without_managed_workspace = bind_agent_profile(
            profile=WRITABLE_WORKER_AGENT_PROFILE,
            provider=provider,
            tools=tools,
            sandbox_profile=SandboxProfile.WORKSPACE,
            execution_budget=_budget(),
            reasoning_effort=ReasoningEffort.HIGH,
            managed_workspace=False,
        )
        self.assertNotIn(
            AgentCapability.WORKSPACE_WRITE,
            denied_without_managed_workspace.effective_capabilities,
        )
        self.assertNotIn("apply_patch", denied_without_managed_workspace.bound_tool_names)

        managed = bind_agent_profile(
            profile=WRITABLE_WORKER_AGENT_PROFILE,
            provider=provider,
            tools=tools,
            sandbox_profile=SandboxProfile.WORKSPACE,
            execution_budget=_budget(),
            reasoning_effort=ReasoningEffort.HIGH,
            managed_workspace=True,
        )
        self.assertIn(AgentCapability.WORKSPACE_WRITE, managed.effective_capabilities)
        self.assertIn("apply_patch", managed.bound_tool_names)

    async def test_provider_unknown_search_is_removed_from_executable_binding(self) -> None:
        binding = bind_agent_profile(
            profile=_single_capability_profile(AgentCapability.WEB_SEARCH),
            provider=_Provider(ModelCapabilitySet.all_unknown()),
            tools=_ToolsFixture("web_search"),
            sandbox_profile=SandboxProfile.OFF,
            execution_budget=_budget(),
            reasoning_effort=ReasoningEffort.HIGH,
        )
        self.assertNotIn(AgentCapability.WEB_SEARCH, binding.effective_capabilities)
        self.assertNotIn("web_search", binding.bound_tool_names)
        unavailable = {
            item.capability: item.reason.value for item in binding.capability_resolution.unavailable
        }
        self.assertEqual(unavailable[AgentCapability.WEB_SEARCH], "provider_unavailable")

    async def test_unbound_project_memory_keeps_schema_but_not_effective_capability(self) -> None:
        binding = bind_agent_profile(
            profile=MAIN_AGENT_PROFILE,
            provider=_Provider(ModelCapabilitySet.from_supported(ModelCapability.FUNCTION_TOOLS)),
            tools=_ToolsFixture("read_project_memory", "read_file"),
            sandbox_profile=SandboxProfile.WORKSPACE,
            execution_budget=_budget(),
            reasoning_effort=ReasoningEffort.HIGH,
            active_project_memory=False,
        )
        self.assertNotIn(
            AgentCapability.PROJECT_MEMORY_READ,
            binding.effective_capabilities,
        )
        self.assertNotIn("read_project_memory", binding.bound_tool_names)
        unavailable = {
            item.capability: item.reason.value for item in binding.capability_resolution.unavailable
        }
        self.assertEqual(
            unavailable[AgentCapability.PROJECT_MEMORY_READ],
            "runtime_unavailable",
        )

    async def test_permission_deny_removes_tool_but_path_scoped_policy_stays_authoritative(
        self,
    ) -> None:
        writable_tools = _ToolsFixture("read_file", "apply_patch", "search_replace")
        broad_deny = PermissionManager(
            rules=(PermissionRule(PermissionEffect.DENY, "apply_patch"),)
        )
        broad_binding = bind_agent_profile(
            profile=MAIN_AGENT_PROFILE,
            provider=_Provider(),
            tools=writable_tools,
            sandbox_profile=SandboxProfile.WORKSPACE,
            execution_budget=_budget(),
            reasoning_effort=ReasoningEffort.HIGH,
            permissions=broad_deny,
        )
        self.assertNotIn("apply_patch", broad_binding.bound_tool_names)

        path_deny = PermissionManager(
            rules=(
                PermissionRule(
                    PermissionEffect.DENY,
                    "apply_patch",
                    path_pattern="/workspace/protected/*",
                ),
            )
        )
        scoped_binding = bind_agent_profile(
            profile=MAIN_AGENT_PROFILE,
            provider=_Provider(),
            tools=writable_tools,
            sandbox_profile=SandboxProfile.WORKSPACE,
            execution_budget=_budget(),
            reasoning_effort=ReasoningEffort.HIGH,
            permissions=path_deny,
        )
        self.assertIn("apply_patch", scoped_binding.bound_tool_names)

    async def test_architecture_binds_every_production_agent_runtime_through_profile_resolution(
        self,
    ) -> None:
        source_root = Path(__file__).parents[1] / "src" / "neuro_code"
        runtime_calls: list[tuple[Path, ast.Call]] = []
        for path in source_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "AgentRuntime"
                ):
                    runtime_calls.append((path, node))
        self.assertTrue(runtime_calls)
        self.assertEqual(
            {path.relative_to(source_root).as_posix() for path, _ in runtime_calls},
            {"bootstrap/composition_bindings.py"},
        )
        for _path, call in runtime_calls:
            self.assertIn(
                "effective_agent_binding",
                {keyword.arg for keyword in call.keywords},
            )


if __name__ == "__main__":
    unittest.main()
