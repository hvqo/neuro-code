from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from tests.fakes import EmptyWorkspaceChangeObserver

from neuro_code.application.permissions.policy import PermissionManager
from neuro_code.application.ports.model import ModelToolPolicy
from neuro_code.application.ports.tools import Tool, ToolCollection, ToolContext
from neuro_code.application.runtime.agent import AgentRuntime
from neuro_code.application.trace.collector import TraceCollector, TraceSnapshot
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import (
    AgentEvent,
    ModelCompleted,
    ModelEvent,
    ModelTextDelta,
    ModelToolCall,
    ModelUsage,
)
from neuro_code.domain.conversation.messages import Role, SessionItem, SyntheticReason, ToolCall
from neuro_code.domain.tools import ToolDefinition, ToolResult

REPOSITORY_REVIEW_PROMPT = "Review retry behavior for idempotency and missing regression coverage."
REPOSITORY_REVIEW_FINDING = (
    "The retry path can repeat a non-idempotent request; add an idempotency key and a "
    "regression for a timeout after server-side success."
)
REPOSITORY_REVIEW_FILES = (
    "src/api.py",
    "src/retry.py",
    "src/request.py",
    "tests/test_retry.py",
    "docs/retry-contract.md",
)
_PROVIDER_DELAY_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class RepositoryReviewBenchmarkResult:
    snapshot: TraceSnapshot
    response: str
    provider_request_count: int
    tool_paths_read: frozenset[str]
    request_contexts: tuple[ModelContext, ...]
    durable_items: tuple[SessionItem, ...]
    elapsed_ms: float


class _ReadFixtureTool(Tool):
    definition = ToolDefinition(
        name="read_file",
        description="Read one file from the isolated repository review fixture.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    side_effecting = False

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self.paths_read: list[str] = []

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del context
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str):
            return ToolResult("fixture path is invalid", is_error=True)
        target = (self._root / raw_path).resolve()
        if not target.is_relative_to(self._root) or not target.is_file():
            return ToolResult("fixture path is unavailable", is_error=True)
        content = target.read_text(encoding="utf-8")
        self.paths_read.append(raw_path)
        return ToolResult(f"{raw_path}\n{content}")


class _ReadOnlyTools(ToolCollection):
    def __init__(self, tool: _ReadFixtureTool) -> None:
        self._tool = tool

    def get(self, name: str) -> Tool | None:
        return self._tool if name == self._tool.definition.name else None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return (self._tool.definition,)

    def has_synthetic_intent(self, name: str) -> bool:
        return name == self._tool.definition.name


class _RepositoryReviewProvider:
    provider_name = "benchmark"
    model_name = "repository-review-fixture"
    context_affinity = "fixture:repository-review-v1"

    def __init__(self, *, adaptive: bool, dependent: bool) -> None:
        self.adaptive = adaptive
        self.dependent = dependent
        self.calls: list[ModelContext] = []
        self.tool_definitions: list[tuple[ToolDefinition, ...]] = []

    async def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        self.calls.append(context)
        self.tool_definitions.append(tuple(tools))
        await asyncio.sleep(_PROVIDER_DELAY_SECONDS)
        seen = {
            path
            for path in REPOSITORY_REVIEW_FILES
            if any(
                message.role is Role.TOOL and path in message.content
                for message in context.messages
            )
        }
        remaining = tuple(path for path in REPOSITORY_REVIEW_FILES if path not in seen)
        if not remaining:
            yield ModelTextDelta(REPOSITORY_REVIEW_FINDING)
            yield ModelCompleted(
                "stop",
                usage=ModelUsage(input_tokens=1_000, output_tokens=20, cache_read_tokens=800),
            )
            return

        efficiency_guidance = any(
            message.synthetic_reason is SyntheticReason.RUNTIME_EFFICIENCY
            for message in context.messages
            if hasattr(message, "synthetic_reason")
        )
        selected = (
            remaining[:1]
            if self.dependent
            else remaining[:3]
            if self.adaptive and efficiency_guidance and len(remaining) >= 3
            else remaining[:1]
        )
        for path in selected:
            yield ModelToolCall(
                ToolCall(f"read-{len(self.calls)}-{path}", "read_file", {"path": path})
            )
        yield ModelCompleted(
            "tool_calls",
            usage=ModelUsage(input_tokens=1_000, output_tokens=8, cache_read_tokens=800),
        )


async def run_repository_review_benchmark(
    *,
    root: Path,
    adaptive: bool,
    dependent: bool = False,
) -> RepositoryReviewBenchmarkResult:
    provider = _RepositoryReviewProvider(adaptive=adaptive, dependent=dependent)
    tool = _ReadFixtureTool(root)
    collector = TraceCollector()
    collector.begin_turn(turn_id="execution-efficiency-benchmark")

    async def record(event: AgentEvent) -> None:
        collector.observe(event)

    runtime = AgentRuntime(
        provider=provider,
        tools=_ReadOnlyTools(tool),
        workspace_change_observer=EmptyWorkspaceChangeObserver(),
        permissions=PermissionManager(),
        tool_context=ToolContext(root),
        final_output_gate_enabled=False,
        normal_requirements_enabled=False,
    )
    started = monotonic()
    result = await runtime.run(REPOSITORY_REVIEW_PROMPT, sink=record)
    elapsed_ms = (monotonic() - started) * 1000
    collector.end_turn()
    snapshot = collector.snapshot()
    assert snapshot is not None
    return RepositoryReviewBenchmarkResult(
        snapshot=snapshot,
        response=result.response,
        provider_request_count=len(provider.calls),
        tool_paths_read=frozenset(tool.paths_read),
        request_contexts=tuple(provider.calls),
        durable_items=result.items,
        elapsed_ms=elapsed_ms,
    )


def write_repository_review_fixture(root: Path) -> None:
    files = {
        "src/api.py": "from .retry import send_with_retry\n\nasync def create(payload):\n    return await send_with_retry(payload)\n",
        "src/retry.py": "async def send_with_retry(payload):\n    for attempt in range(3):\n        try:\n            return await send(payload)\n        except TimeoutError:\n            if attempt == 2:\n                raise\n",
        "src/request.py": "async def send(payload):\n    return await client.post('/records', json=payload)\n",
        "tests/test_retry.py": "async def test_retries_timeout():\n    assert await send_with_retry({'name': 'x'}) == 'ok'\n",
        "docs/retry-contract.md": "POST /records creates a record. The client retries timeouts up to three times.\n",
    }
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


__all__ = [
    "REPOSITORY_REVIEW_FILES",
    "REPOSITORY_REVIEW_FINDING",
    "RepositoryReviewBenchmarkResult",
    "run_repository_review_benchmark",
    "write_repository_review_fixture",
]
