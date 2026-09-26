# ruff: noqa: RUF001
"""Bounded Agent runtime trace ledger and metadata inspector."""

from __future__ import annotations

import json
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from neuro_code.application.trace.collector import (
    TRACE_LEDGER_PAGE_SIZE,
    TraceCollector,
    TraceKind,
    TraceRecord,
    TraceSnapshot,
)
from neuro_code.shared.ui_language import UiLanguage


class TraceScreen(ModalScreen[None]):
    """Metadata-only trace ledger with bounded rendering and lazy filtering."""

    CSS = """
    TraceScreen { align: center middle; background: $modal-overlay 25%; }
    #trace-dialog { width: 96%; max-width: 150; height: 92%; padding: $space-1 $space-2; background: $surface; border: round $border; }
    #trace-title, #trace-help, #trace-summary { height: auto; max-height: 3; color: $text-primary; }
    #trace-filter { height: 3; margin: $space-1 $space-0; }
    #trace-main { height: 1fr; }
    #trace-ledger, #trace-inspector { width: 1fr; height: 1fr; padding: $space-1; border: round $border; overflow: hidden; }
    #trace-ledger { width: 58%; }
    #trace-inspector { width: 42%; }
    #trace-timeline { height: 4; margin-top: $space-1; padding: $space-0 $space-1; color: $text-secondary; border: round $border; }
    #trace-help { color: $text-secondary; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", priority=True, show=False),
        Binding("up", "previous", "Previous", priority=True, show=False),
        Binding("down", "next", "Next", priority=True, show=False),
        Binding("pageup", "page_previous", "Page up", priority=True, show=False),
        Binding("pagedown", "page_next", "Page down", priority=True, show=False),
        Binding("home", "first", "First", priority=True, show=False),
        Binding("end", "last", "Last", priority=True, show=False),
        Binding("ctrl+f", "toggle_follow", "Follow", priority=True, show=False),
        Binding("/", "focus_search", "Search", priority=True, show=False),
        Binding("enter", "inspect", "Inspect", priority=True, show=False),
        Binding("1", "overview", "Overview", priority=True, show=False),
        Binding("2", "timing", "Timing", priority=True, show=False),
        Binding("3", "usage", "Usage", priority=True, show=False),
        Binding("4", "context", "Context", priority=True, show=False),
        Binding("5", "diagnostics", "Diagnostics", priority=True, show=False),
        Binding("[", "older_trace", "Older trace", priority=True, show=False),
        Binding("]", "newer_trace", "Newer trace", priority=True, show=False),
    ]

    def __init__(self, collector: TraceCollector, *, language: UiLanguage) -> None:
        super().__init__()
        self._collector = collector
        self._language = language
        self._snapshots: tuple[TraceSnapshot, ...] = ()
        self._trace_index = 0
        self._filtered: tuple[TraceRecord, ...] = ()
        self._selected = 0
        self._follow = True
        self._inspect = False
        self._tab = "overview"
        self._query = ""

    @property
    def selected_record(self) -> TraceRecord | None:
        if not self._filtered:
            return None
        return self._filtered[min(max(self._selected, 0), len(self._filtered) - 1)]

    def compose(self) -> ComposeResult:
        with Vertical(id="trace-dialog", classes="modal-dialog modal-l"):
            yield Label("Agent Trace", id="trace-title")
            yield Label("", id="trace-summary")
            yield Input(
                placeholder=self._text("Filter/search metadata…", "筛选/搜索元数据…"),
                id="trace-filter",
            )
            with Horizontal(id="trace-main"):
                yield Static("", id="trace-ledger")
                yield Static("", id="trace-inspector")
            yield Static("", id="trace-timeline")
            yield Label(self._help(), id="trace-help")

    def on_mount(self) -> None:
        self.refresh_trace()
        self.query_one("#trace-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "trace-filter":
            self._query = event.value.casefold().strip()
            self._rebuild_filtered()
            self._refresh_view()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "trace-filter":
            self._inspect = True
            self._refresh_view()

    def refresh_trace(self) -> None:
        old_trace_id = self._current_snapshot().trace_id if self._snapshots else None
        self._snapshots = self._collector.snapshots()
        if not self._snapshots:
            self._trace_index = 0
            self._filtered = ()
            self._refresh_view()
            return
        if old_trace_id is not None:
            self._trace_index = next(
                (
                    index
                    for index, item in enumerate(self._snapshots)
                    if item.trace_id == old_trace_id
                ),
                len(self._snapshots) - 1,
            )
        else:
            self._trace_index = len(self._snapshots) - 1
        self._rebuild_filtered()
        if self._follow and self._filtered:
            self._selected = len(self._filtered) - 1
        else:
            self._selected = min(self._selected, max(0, len(self._filtered) - 1))
        self._refresh_view()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_previous(self) -> None:
        if self._filtered:
            self._selected = max(0, self._selected - 1)
            self._follow = False
            self._refresh_view()

    def action_next(self) -> None:
        if self._filtered:
            self._selected = min(len(self._filtered) - 1, self._selected + 1)
            self._refresh_view()

    def action_page_previous(self) -> None:
        if self._filtered:
            self._selected = max(0, self._selected - TRACE_LEDGER_PAGE_SIZE)
            self._follow = False
            self._refresh_view()

    def action_page_next(self) -> None:
        if self._filtered:
            self._selected = min(len(self._filtered) - 1, self._selected + TRACE_LEDGER_PAGE_SIZE)
            self._refresh_view()

    def action_first(self) -> None:
        self._selected = 0
        self._follow = False
        self._refresh_view()

    def action_last(self) -> None:
        self._selected = max(0, len(self._filtered) - 1)
        self._follow = True
        self._refresh_view()

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        if self._follow:
            self._selected = max(0, len(self._filtered) - 1)
        self._refresh_view()

    def action_focus_search(self) -> None:
        self.query_one("#trace-filter", Input).focus()

    def action_inspect(self) -> None:
        self._inspect = not self._inspect
        self._refresh_view()

    def action_overview(self) -> None:
        self._select_tab("overview")

    def action_timing(self) -> None:
        self._select_tab("timing")

    def action_usage(self) -> None:
        self._select_tab("usage")

    def action_context(self) -> None:
        self._select_tab("context")

    def action_diagnostics(self) -> None:
        self._select_tab("diagnostics")

    def action_older_trace(self) -> None:
        self._select_trace(max(0, self._trace_index - 1))

    def action_newer_trace(self) -> None:
        self._select_trace(min(len(self._snapshots) - 1, self._trace_index + 1))

    def _select_tab(self, tab: str) -> None:
        self._tab = tab
        self._inspect = True
        self._refresh_view()

    def _select_trace(self, index: int) -> None:
        if not self._snapshots:
            return
        self._trace_index = index
        self._rebuild_filtered()
        self._selected = max(0, len(self._filtered) - 1)
        self._follow = True
        self._refresh_view()

    def _current_snapshot(self) -> TraceSnapshot:
        return self._snapshots[self._trace_index]

    def _rebuild_filtered(self) -> None:
        if not self._snapshots:
            self._filtered = ()
            return
        snapshot = self._current_snapshot()
        if not self._query:
            self._filtered = snapshot.records
        else:
            self._filtered = tuple(
                record
                for record in snapshot.records
                if self._query
                in " ".join(
                    (
                        record.kind.value,
                        record.name,
                        record.status.value,
                        record.provider or "",
                        record.model or "",
                        str(record.step or ""),
                        json.dumps(dict(record.metadata), ensure_ascii=False, sort_keys=True),
                    )
                ).casefold()
            )
        if self._filtered:
            self._selected = min(self._selected, len(self._filtered) - 1)
        else:
            self._selected = 0

    def _refresh_view(self) -> None:
        if not self.is_mounted:
            return
        title = self.query_one("#trace-title", Label)
        summary_widget = self.query_one("#trace-summary", Label)
        ledger = self.query_one("#trace-ledger", Static)
        inspector = self.query_one("#trace-inspector", Static)
        timeline = self.query_one("#trace-timeline", Static)
        if not self._snapshots:
            title.update("Agent Trace")
            summary_widget.update(
                self._text(
                    "No runtime trace yet. Submit a turn, then run /trace.",
                    "暂无运行时追踪。先提交一个回合，再运行 /trace。",
                )
            )
            ledger.update(self._text("No records captured.", "尚未捕获记录。"))
            inspector.update("")
            timeline.update("")
            return
        snapshot = self._current_snapshot()
        summary = snapshot.summary
        cache_label = (
            f"{summary.weighted_cache_reuse:.1%}"
            if summary.weighted_cache_reuse is not None
            else "unknown"
        )
        title.update(
            f"Agent Trace · {self._trace_index + 1}/{len(self._snapshots)} · {snapshot.status.value} · {snapshot.source}"
        )
        parallel_ratio = (
            f"{summary.parallel_tool_ratio:.0%}" if summary.parallel_tool_ratio is not None else "—"
        )
        longest_model = (
            f"{summary.longest_model_requests[0][0]} {_seconds(summary.longest_model_requests[0][1])}"
            if summary.longest_model_requests
            else "—"
        )
        longest_tool = (
            f"{summary.longest_tools[0][0]} {_seconds(summary.longest_tools[0][1])}"
            if summary.longest_tools
            else "—"
        )
        cache_miss = (
            f"{summary.cache_miss_per_request:.0f}"
            if summary.cache_miss_per_request is not None
            else "—"
        )
        summary_widget.update(
            f"{self._text('Turn', '回合')} {_seconds(summary.duration_ms)} · "
            f"{summary.model_steps} steps · {summary.model_requests} requests · "
            f"{summary.tool_batches} batches / {summary.tool_calls} tools · "
            f"Weighted cache reuse {cache_label} · Provider {_seconds(summary.provider_time_ms)} · "
            f"TTFT {_milliseconds(summary.average_ttft_ms)} / user {_milliseconds(summary.user_visible_ttft_ms)} · "
            f"Cache miss/request {cache_miss}\n"
            f"Permission wait {_seconds(summary.permission_wait_ms)} · "
            f"Tool execution {_seconds(summary.tool_time_ms)} · Context {_seconds(summary.context_time_ms)} · "
            f"Runtime/Other {_seconds(summary.runtime_other_ms)}\n"
            f"Parallel {parallel_ratio} · retries {summary.retries} · failovers {summary.failovers} · "
            f"replans {summary.replans} · compactions {summary.compactions} · finalizers {summary.finalizer_calls}\n"
            f"Longest model {longest_model} · tool {longest_tool}"
        )
        records = self._filtered
        page_start = (self._selected // TRACE_LEDGER_PAGE_SIZE) * TRACE_LEDGER_PAGE_SIZE
        page = records[page_start : page_start + TRACE_LEDGER_PAGE_SIZE]
        lines = [
            "# TYPE          NAME                    STATE       TIME    TTFT    IN    CACHE OUT",
            f"records {len(records)} · page {page_start + 1 if records else 0}-{page_start + len(page)} · dropped {snapshot.dropped_records}",
        ]
        for offset, record in enumerate(page, start=page_start):
            marker = ">" if offset == self._selected else " "
            lines.append(_ledger_line(offset + 1, record, marker=marker))
        ledger.update("\n".join(lines))
        selected = self.selected_record
        inspector.update(
            self._render_inspector(selected)
            if selected is not None
            else self._text("No matching record.", "没有匹配记录。")
        )
        timeline.update(_timeline(snapshot))

    def _render_inspector(self, record: TraceRecord) -> str:
        data = record.to_dict()
        metadata = dict(record.metadata)
        common = [
            f"{self._text('Inspector', '检查器')} · {record.kind.value} · {record.name}",
            f"Status: {record.status.value}",
            f"Step: {record.step if record.step is not None else '—'}",
            f"Span: {record.span_id}",
            f"Parent: {record.parent_span_id or '—'}",
        ]
        if record.request_id:
            common.append(f"Request: {record.request_id}")
        if record.tool_call_id:
            common.append(f"Tool call: {record.tool_call_id}")
        tabs = {
            "overview": ("provider", "model", "status", "step", "source", "output_kind"),
            "timing": (
                "duration_ms",
                "ttft_ms",
                "stream_duration_ms",
                "permission_wait_ms",
                "execution_ms",
                "batch_wall_ms",
                "sum_child_execution_ms",
            ),
            "usage": (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cache_miss_tokens",
                "cache_reuse_ratio",
                "estimated_context_tokens",
                "capacity_tokens",
            ),
            "context": (
                "context_generation",
                "cache_epoch",
                "cache_boundary_reason",
                "append_only",
                "common_prefix_messages",
                "microcompaction",
                "estimated_tokens",
                "item_count",
            ),
            "diagnostics": (
                "error_type",
                "failure_kind",
                "status_code",
                "retry_count",
                "failover_count",
                "parallel",
                "exclusive",
                "child_tool_count",
                "result_bytes",
                "original_bytes",
                "projected_bytes",
                "truncated",
                "artifact_available",
                "reason_code",
                "replan_count",
                "cycle_period",
                "progress_since_replan",
                "attempts",
                "cancelled",
                "not_started",
            ),
        }
        values: list[str] = []
        for key in tabs[self._tab]:
            value = data.get(key, metadata.get(key))
            if value is None or value == "":
                continue
            values.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        if not self._inspect:
            values = values[:8]
        return "\n".join(
            (*common, f"Tab: {self._tab} · Enter {self._text('to expand', '展开')}", "", *values)
        )

    def _text(self, english: str, chinese: str) -> str:
        return chinese if self._language is UiLanguage.SIMPLIFIED_CHINESE else english

    def _help(self) -> str:
        return self._text(
            "↑/↓ select · PgUp/PgDn page · Enter inspect · / search · Ctrl+F follow · [ and ] history · 1-5 tabs · Esc close",
            "↑/↓ 选择 · PgUp/PgDn 翻页 · Enter 检查 · / 搜索 · Ctrl+F 跟随 · [ 和 ] 历史 · 1-5 标签 · Esc 关闭",
        )


def _seconds(value: float | None) -> str:
    return "—" if value is None else f"{value / 1000:.2f}s"


def _milliseconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}ms"


def _ledger_line(index: int, record: TraceRecord, *, marker: str) -> str:
    name = record.name[:22]
    kind = record.kind.value.upper()[:12]
    state = record.status.value[:9]
    duration = _seconds(record.duration_ms).rjust(7)
    ttft = _milliseconds(record.ttft_ms).rjust(7)
    input_tokens = str(record.input_tokens) if record.input_tokens is not None else "—"
    cache_tokens = str(record.cache_read_tokens) if record.cache_read_tokens is not None else "—"
    output_tokens = str(record.output_tokens) if record.output_tokens is not None else "—"
    return f"{marker}{index:4d} {kind:12} {name:22} {state:9} {duration} {ttft} {input_tokens:>5} {cache_tokens:>6} {output_tokens:>4}"


def _timeline(snapshot: TraceSnapshot) -> str:
    spans = tuple(
        record
        for record in snapshot.records
        if record.kind
        in {
            TraceKind.MODEL,
            TraceKind.TOOL,
            TraceKind.CONTEXT,
            TraceKind.FINALIZER,
            TraceKind.REPLAN,
            TraceKind.SUBAGENT,
        }
        and record.duration_ms is not None
        and record.duration_ms > 0
    )
    duration = snapshot.duration_ms or max(
        (record.start_offset_ms + (record.duration_ms or 0) for record in spans),
        default=0,
    )
    if duration <= 0:
        return "Timeline · no measured spans"
    width = 72
    cells = ["·"] * width
    legend: dict[str, str] = {}
    for record in spans:
        start = min(width - 1, int(record.start_offset_ms / duration * width))
        span_duration = record.duration_ms or 0.0
        end = min(
            width, max(start + 1, int((record.start_offset_ms + span_duration) / duration * width))
        )
        char = (
            "M"
            if record.kind is TraceKind.MODEL
            else "T"
            if record.kind is TraceKind.TOOL
            else "S"
            if record.kind is TraceKind.SUBAGENT
            else "R"
        )
        legend[char] = {"M": "MODEL", "T": "TOOL", "S": "SUBAGENT", "R": "RUNTIME"}[char]
        for index in range(start, end):
            cells[index] = char if cells[index] == "·" or cells[index] == char else "*"
    labels = " · ".join(f"{char}={name}" for char, name in legend.items())
    return f"Timeline · 0s {' '.join(cells)} {duration / 1000:.1f}s\n{labels} · *=overlap"


__all__ = ["TraceScreen"]
