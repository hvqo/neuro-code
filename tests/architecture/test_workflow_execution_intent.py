"""Exact publication-to-worker intent, without new capability authority."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from neuro_code.application.agents.profiles import WRITABLE_WORKER_AGENT_PROFILE
from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.application.workflows.subagent_capabilities import (
    resolve_writable_subagent_capability,
    writable_subagent_request,
)
from neuro_code.application.workflows.task_dag import RunTaskDagRequest, TaskDagApplicationService
from neuro_code.application.workflows.writable_subagent import (
    RunWritableSubagentRequest,
    resolve_writable_execution_profile,
)
from neuro_code.bootstrap.subagent import CompositionWritableSubagentRuntimeFactory
from neuro_code.domain.agents.profile import AgentCapability
from neuro_code.domain.parent_context_relay import ParentContextRelay
from neuro_code.domain.task_dag import TaskDagNodeState
from neuro_code.domain.workflows.publication import (
    ExpansionMember,
    WorkflowNodeExecutionIntent,
    canonical,
    digest,
)
from neuro_code.domain.workflows.state import StepIdentity
from neuro_code.infrastructure.persistence import sqlite_session_workflow_publication as owner
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError
from tests.architecture.test_workflow_interpreter import source, tick
from tests.architecture.test_workflow_publication import INPUT, NOW, counts, intent, publish
from tests.architecture.test_workflow_publication import setup as publication_setup
from tests.test_agent_profiles import _resolve
from tests.test_task_dag import _binding, _FakeLeaseStore, _FakeRelayStore, _FakeWritableService
from tests.test_writable_subagent import _capability, _grant


def execution_intent(profile="writable_worker", caps=(AgentCapability.WORKSPACE_READ,)):
    return WorkflowNodeExecutionIntent(
        "run",
        "expansion",
        "dag",
        StepIdentity("implement"),
        ExpansionMember("batch", "work", "node", INPUT, profile, caps),
        "parent",
    )


@pytest.mark.parametrize("field", ["profile_ref", "required_capabilities"])
async def test_intent_participates_in_publication_identity_and_replay(tmp_path, field):
    store, run = await publication_setup(tmp_path)
    proposal = intent(run)
    first = await publish(store, run, proposal)
    changed = replace(
        proposal.members[0],
        **{field: "custom_worker" if field == "profile_ref" else (AgentCapability.WORKSPACE_READ,)},
    )
    other = replace(proposal, members=(changed,))
    assert other.identity_fingerprint != proposal.identity_fingerprint
    assert other.dag.definition_fingerprint == proposal.dag.definition_fingerprint
    before = counts(store)
    with pytest.raises(WorkflowStateError, match="conflict"):
        await publish(store, run, other)
    assert counts(store) == before
    assert (await publish(store, run, proposal)).replayed
    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    result = await reopened.get_workflow_node_execution_intent(first.dag.dag_id, "node-0")
    assert result.member == proposal.members[0]
    assert (
        result.fingerprint
        == (await store.get_workflow_node_execution_intent(first.dag.dag_id, "node-0")).fingerprint
    )


@pytest.mark.parametrize("field", ["profile_ref", "required_capabilities"])
async def test_definitions_differing_only_in_intent_produce_distinct_bindings(tmp_path, field):
    from neuro_code.application.workflows.workflow_interpreter import DurableWorkflowInterpreter
    from neuro_code.domain.workflows import compile_workflow

    _, run = await publication_setup(tmp_path)
    definitions = []
    proposals = []
    for index in range(2):
        data = source()
        if index:
            data["steps"][0]["tasks"][0][field] = (
                "custom_worker" if field == "profile_ref" else ["workspace.read"]
            )
        definition = compile_workflow(canonical(data))
        definitions.append(definition)
        # Hold Run, parent, DAG, prompts, inputs and timestamps exactly constant.
        proposals.append(
            DurableWorkflowInterpreter._intent(
                run, definition.steps[0], StepIdentity("implement"), {"batch": {"work": {}}}, INPUT
            )
        )
    assert definitions[0].fingerprint != definitions[1].fingerprint
    assert proposals[0].dag == proposals[1].dag
    assert proposals[0].identity_fingerprint != proposals[1].identity_fingerprint


async def test_new_publication_requires_exact_declared_intent(tmp_path):
    store, run = await publication_setup(tmp_path)
    proposal = intent(run)
    for member in (
        ExpansionMember("batch", "work", "node-0", INPUT),
        replace(proposal.members[0], profile_ref="custom_worker"),
        replace(proposal.members[0], required_capabilities=()),
    ):
        with pytest.raises(WorkflowStateError, match="TaskSpec"):
            await publish(store, run, replace(proposal, members=(member,)))
        assert counts(store)[0] == 0


async def test_atomic_rollback_and_commit_before_ack_keep_exact_intent(tmp_path):
    store, run = await publication_setup(tmp_path)
    proposal = intent(run)
    before = counts(store)
    with (
        patch.object(owner, "_insert_expansion", side_effect=RuntimeError("after DAG insert")),
        pytest.raises(RuntimeError),
    ):
        await publish(store, run, proposal)
    assert counts(store) == before
    await publish(store, run, proposal)  # The client loses this acknowledgement.
    reopened = SqliteSessionStore(store.database_path)
    await reopened.initialize()
    assert (await publish(reopened, run, proposal)).replayed
    assert (
        await reopened.get_workflow_node_execution_intent(proposal.dag.dag_id, "node-0")
    ).member == proposal.members[0]
    assert SCHEMA_VERSION == 40


@pytest.mark.parametrize("change", ["profile", "caps", "node", "digest"])
async def test_execution_read_rejects_payload_tamper(tmp_path, change):
    store, run = await publication_setup(tmp_path)
    proposal = intent(run)
    await publish(store, run, proposal)
    with closing(sqlite3.connect(store.database_path)) as db, db:
        db.execute("DROP TRIGGER workflow_expansions_immutable_update")
        data = proposal.payload
        if change == "profile":
            data["members"][0]["profile_ref"] = "other"
        elif change == "caps":
            data["members"][0]["required_capabilities"] = []
        elif change == "node":
            data["members"][0]["node_id"] = "other"
        db.execute(
            "UPDATE workflow_expansions SET canonical_intent = ?",
            (canonical(data) if change != "digest" else "{}",),
        )
    with pytest.raises(WorkflowStateError):
        await store.get_workflow_node_execution_intent(proposal.dag.dag_id, "node-0")


async def test_legacy_publication_remains_readable_but_not_executable(tmp_path):
    store, run = await publication_setup(tmp_path)
    proposal = intent(run)
    # Simulate exact pre-DW5a publication using the old frozen member contract.
    legacy = replace(proposal, members=(ExpansionMember("batch", "work", "node-0", INPUT),))
    with patch.object(owner, "_validate_members"):
        await publish(store, run, legacy)
    assert await store.get_workflow_expansion(legacy.expansion_id) is not None
    with pytest.raises(WorkflowStateError, match="missing"):
        await store.get_workflow_node_execution_intent(legacy.dag.dag_id, "node-0")
    assert await store.get_workflow_node_execution_intent("ordinary-dag", "node") is None


@pytest.mark.parametrize("legacy", [False, True])
async def test_worker_seam_passes_exact_intent_or_fails_before_worker(tmp_path, legacy):
    store, run = await publication_setup(tmp_path)
    proposal = intent(run)
    if legacy:
        proposal = replace(proposal, members=(ExpansionMember("batch", "work", "node-0", INPUT),))
        with patch.object(owner, "_validate_members"):
            await publish(store, run, proposal)
    else:
        await publish(store, run, proposal)
    worker = _FakeWritableService(run.parent_session_id)
    requests = []
    original = worker.run_subagent_with_execution_identity

    async def capture(request, **kwargs):
        requests.append(request)
        raise ConfigurationError("test stops before any model")

    worker.run_subagent_with_execution_identity = capture
    service = TaskDagApplicationService(
        store,
        store,
        worker,
        _FakeLeaseStore(),
        _FakeRelayStore(),
        parent_binding=_binding(run.parent_session_id),
    )
    result = await service.run_task_dag(RunTaskDagRequest(proposal.dag.dag_id))
    assert result.nodes[0].state is TaskDagNodeState.FAILED
    if legacy:
        assert not requests
    else:
        assert requests[
            0
        ].workflow_execution_intent == await store.get_workflow_node_execution_intent(
            proposal.dag.dag_id, "node-0"
        )
    assert not worker.calls
    worker.run_subagent_with_execution_identity = original


@pytest.mark.parametrize("profile", ["missing_custom", "reviewer", "explorer", "main", "leader"])
def test_unknown_and_nonwritable_profiles_never_fallback(profile):
    with pytest.raises((ValueError, ConfigurationError)):
        resolve_writable_execution_profile(
            RunWritableSubagentRequest(
                "parent", "task", workflow_execution_intent=execution_intent(profile)
            )
        )
    assert (
        resolve_writable_execution_profile(RunWritableSubagentRequest("parent", "task"))
        is WRITABLE_WORKER_AGENT_PROFILE
    )


@pytest.mark.parametrize(
    "ceiling",
    [
        None,
        "parent",
        "permission",
        "security",
        "provider",
        "platform",
        "runtime",
        "wrong_profile",
        "missing_binding",
    ],
)
async def test_factory_exact_profile_and_effective_requirements_before_model(tmp_path, ceiling):
    from neuro_code.application.agents.binding import ALL_AGENT_CAPABILITIES
    from neuro_code.application.agents.profiles import EXPLORER_AGENT_PROFILE
    from neuro_code.application.ports.configuration import AppConfig
    from neuro_code.domain.agents.profile import AgentCapability

    parent = _capability(tmp_path / "parent")
    global_policy = _capability(tmp_path / "global")
    grant = _grant(tmp_path, parent)
    capabilities = resolve_writable_subagent_capability(
        parent=parent,
        requested=writable_subagent_request(parent, global_policy=global_policy, max_steps=8),
        global_policy=global_policy,
        workspace_grant=grant,
    )
    effective = _resolve(
        WRITABLE_WORKER_AGENT_PROFILE,
        **(
            {ceiling: set(ALL_AGENT_CAPABILITIES) - {AgentCapability.WORKSPACE_READ}}
            if ceiling in {"parent", "permission", "security", "provider", "platform", "runtime"}
            else {}
        ),
    )
    if ceiling == "wrong_profile":
        effective = _resolve(EXPLORER_AGENT_PROFILE)
    binding = SimpleNamespace(
        effective_agent_binding=None if ceiling == "missing_binding" else effective,
        capabilities=capabilities.capabilities,
        close=AsyncMock(),
        runner=SimpleNamespace(run=AsyncMock()),
    )
    composition = SimpleNamespace(
        config=AppConfig(
            cwd=parent.cwd,
            state_dir=tmp_path,
            providers={},
            default_provider=None,
            selected_provider=None,
        ),
        create_binding=AsyncMock(return_value=binding),
    )
    relay = ParentContextRelay.create(
        relay_id="relay",
        parent_session_id="parent",
        parent_task_id="task",
        child_session_id="child",
        lease_id="lease",
        worktree_id=grant.managed_worktree_id,
        baseline_checkpoint_id=grant.baseline_checkpoint_id,
        base_commit_sha=grant.base_commit_sha,
        task_prompt_fingerprint="a" * 64,
        source_item_count=0,
        capability_fingerprint=capabilities.capabilities.fingerprint,
        grant_fingerprint=grant.fingerprint,
        items=(),
        truncated=False,
        created_at=NOW,
    )
    request = RunWritableSubagentRequest(
        "parent", "task", workflow_execution_intent=execution_intent()
    )
    factory = CompositionWritableSubagentRuntimeFactory(composition)
    if ceiling:
        with pytest.raises(ConfigurationError, match=r"identity|unavailable"):
            await factory.create(
                request,
                parent_task_id="task",
                child_session_id="child",
                capabilities=capabilities,
                relay=relay,
            )
        binding.close.assert_awaited_once()
    else:
        runtime = await factory.create(
            request,
            parent_task_id="task",
            child_session_id="child",
            capabilities=capabilities,
            relay=relay,
        )
        assert runtime.capability_fingerprint == capabilities.fingerprint
        assert (
            composition.create_binding.call_args.kwargs["agent_profile"]
            is WRITABLE_WORKER_AGENT_PROFILE
        )
        assert (
            composition.create_binding.call_args.kwargs["capabilities"] is capabilities.capabilities
        )
        await runtime.close()
    binding.runner.run.assert_not_awaited()
    assert grant == capabilities.workspace_grant


@pytest.mark.parametrize(
    "case", ["writable", "unknown", "required_lsp_unavailable", "missing_request", "wrong_request"]
)
async def test_production_composition_consumes_publication_before_first_model(
    tmp_path, monkeypatch, case
):
    from neuro_code.application.permissions.policy import PermissionMode
    from neuro_code.application.settings import ApplicationSettings
    from neuro_code.application.workflows.workflow_interpreter import DurableWorkflowInterpreter
    from neuro_code.bootstrap.composition import ApplicationComposition
    from neuro_code.domain.sandbox.models import SandboxProfile
    from neuro_code.domain.workflows import compile_workflow
    from neuro_code.domain.workflows.interpreter import expansion_id, typed_json
    from tests.test_writable_subagent import (
        _dag_relay_context_provider_factory,
        _make_real_repository,
    )

    repository, _ = _make_real_repository(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "config.toml").write_text(
        '[routing]\ndefault = "fixture"\n[providers.fixture]\nprotocol = "openai-chat"\n'
        'model = "fixture-model"\nbase_url = "https://provider.invalid/v1"\napi_key_env = "FIXTURE_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NEURO_CODE_HOME", str(state))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    contexts = []
    app = await ApplicationComposition.open(
        ApplicationSettings(
            cwd=repository, sandbox="off", permission_mode=PermissionMode.BYPASS, max_steps=8
        ),
        provider_factory=_dag_relay_context_provider_factory(contexts),
    )
    try:
        session = await app.store.create_session(
            str(repository), "fixture", "fixture-model", sandbox_profile=SandboxProfile.OFF
        )
        parent = await app.create_binding(
            resume_id=session, capabilities=_capability(repository, sandbox=SandboxProfile.OFF)
        )
        data = source()
        if case == "unknown":
            data["steps"][0]["tasks"][0]["profile_ref"] = "custom_no_registry"
        elif case == "required_lsp_unavailable":
            data["steps"][0]["tasks"][0]["required_capabilities"].append("lsp")
        definition = compile_workflow(canonical(data))
        value = {"targets": [], "objective": "exact profile intent"}
        await app.store.insert_workflow_definition(definition)
        await app.store.create_workflow_run(
            "run",
            definition_fingerprint=definition.fingerprint,
            parent_session_id=session,
            input_fingerprint=digest(value),
            request_id="create",
            created_at=NOW,
        )
        await app.store.put_workflow_input("run", typed_json(definition.input_schema, value))
        await app.store.claim_workflow_run(
            "run",
            expected_generation=0,
            expected_owner_fence=0,
            owner_id="owner",
            request_id="claim",
            updated_at=NOW,
        )
        interpreter = DurableWorkflowInterpreter(
            state=app.store,
            facts=app.store,
            publication=app.store,
            projections=app.store,
            dags=app.store,
            activities=app.store,
        )
        await tick(app.store, interpreter)
        await tick(app.store, interpreter)
        expansion = await app.store.get_workflow_expansion(
            expansion_id("run", StepIdentity("implement"))
        )
        if case in {"missing_request", "wrong_request"}:
            from neuro_code.application.workflows.writable_subagent import (
                WritableSubagentExecutionIdentity,
            )

            dag = await app.store.get_task_dag(expansion.dag_id)
            durable = await app.store.get_workflow_node_execution_intent(
                dag.dag_id, dag.nodes[0].node_id
            )
            supplied = (
                None
                if case == "missing_request"
                else replace(durable, member=replace(durable.member, required_capabilities=()))
            )
            worker = app.create_writable_subagent_service(parent_binding=parent)
            await worker.initialize()
            with pytest.raises(ConfigurationError, match="differs from durable"):
                await worker.run_subagent_with_execution_identity(
                    RunWritableSubagentRequest(session, "task", workflow_execution_intent=supplied),
                    execution_identity=WritableSubagentExecutionIdentity(
                        dag.dag_id, dag.nodes[0].node_id, "not-started"
                    ),
                )
            assert not contexts
            assert not await app.store.list_session_tasks(session)
            return
        original_binding = app.create_binding
        with patch.object(app, "create_binding", wraps=original_binding) as create:
            result = await app.create_task_dag_service(parent_binding=parent).run_task_dag(
                RunTaskDagRequest(expansion.dag_id)
            )
        if case == "writable":
            assert len(contexts) == 1
            assert result.nodes[0].state is TaskDagNodeState.COMPLETED
            assert create.call_args.kwargs["agent_profile"] is WRITABLE_WORKER_AGENT_PROFILE
        else:
            assert not contexts
            assert result.nodes[0].state is TaskDagNodeState.FAILED
            if case == "unknown":
                assert not create.called
            else:
                assert create.call_count == 1
                assert "required capabilities" in result.nodes[0].error_reason
        # Worker execution never advances Workflow control or interprets success as PASS.
        run = await app.store.get_workflow_run("run")
        assert run.generation == 3
        assert run.status.value == "WAITING"
    finally:
        await app.close()
