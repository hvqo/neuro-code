# ADR 0161：结构化会话 Working Set

[English](../../en/adr/0161-structured-session-working-set.md) · **简体中文**

- 状态：已接受
- 日期：2026-09-15
- 范围：CM2 有界持久任务状态 projection

## 背景

CM1 提供对 canonical ordered durable `SessionItem` sequence 的有界只读访问。该序列是
conversation、recovery、compaction、export 和 fork 语义的 source of truth，但不是 active
task state 的紧凑表示。CM2 需要一个小型持久 projection 保存高信号状态，同时不创建第二份
transcript，也不自动注入旧的 raw history。

## 决策

应用层拥有不可变的 `WorkingSetSnapshot`，固定包含六个 section：`goal`、`constraints`、
`decisions`、`progress`、`unresolved_work` 和 `next_steps`。每个 section 最多八个 entry；每个
entry 有界，完整持久 snapshot 和渲染后的 context 使用独立的字节限制。Entry 分为 model-authored
或 history-backed 两类。History-backed entry 携带不透明的 CM1 `SessionItemReference`；应用层在
写入和读取前都针对当前 session 解析每一个 reference。格式错误、stale、tampered、cross-session
和不可公开的 reference 都失败关闭。Hidden reasoning 永远不是 Working Set 字段。

SQLite 为每个 session 保存一个 `session_working_sets` row。该 row 保存 current revision 和
canonical snapshot JSON；schema version 31 以不复制 `SessionItem` history 的方式增加这个
projection。缺少 row 的 legacy session 解析为 revision-zero 的空状态。存在但格式错误的 row
返回错误，不会隐式 reset。完整替换使用 `BEGIN IMMEDIATE` 和 expected-revision 检查，因此 stale
writer 不能覆盖更新后的 snapshot，也不能产生部分更新。删除 session 会级联删除该 row。Fork、
import 和普通 subagent 创建不会复制 Working Set：child 从空状态开始，因为当前 CM1 reference
不能证明可以安全 rebinding 到 child history。

`SessionWorkingSetApplicationService` 是 validation 和 configured explicit redaction 的唯一应用 owner。
Canonical `SessionApplicationService`、runtime composition 和 model tool 使用同一契约。Snapshot
在持久化或暴露前先进行 redaction，并在 model projection boundary 再次执行。

每次 model request 前，`AgentLoopRunner` 为 runtime-bound session 读取 current snapshot。非空
snapshot 被确定性渲染为带有 `SyntheticReason.WORKING_SET` 的有界 `Message`，由 `ContextBuilder`
插入 project instructions 和 available skills 之后。它不会加入内存中的 durable item sequence；如果
调用方带入该 synthetic message，会被移除；compaction projection 之后会重新构建。空 snapshot 不增加
message。

`session_working_set` 只暴露 `read` 和完整替换式 `update`。Schema 不包含 session selector；可信的
scope 只有 `ToolContext.session_id`。Update 必须提供上次返回的 revision 和全部六个 section。该 tool
声明 `side_effecting=False`，因为现有 flag 表示 workspace/shell mutation；对于已提交的 session-state
update，result metadata 会明确报告 `durable_write=true`。但 tool definition 仍使用 exclusive，使并发
model call 不会通过 scheduler 竞争 durable Working Set replacement。它不会重写 history、调用
compaction、选择 parent/child session，也不会从 parent runtime 接收 Working Set。

## 不变量与非目标

Working Set 不等于 canonical history。持久 `SessionItem` sequence 仍是唯一的 conversation source of
truth，CM1 list/search/read 的可见性和 reference 规则保持不变。Working Set 不等于 compaction summary：
compaction 继续拥有现有的持久 summary 和 recovery lifecycle；Working Set 只会在下一次 request 前再次
读取。Working Set 不等于 cross-session memory：每个 row、reference、application request、tool call
和 runtime projection 都绑定一个 session。

Append-only history、provider-native preserved context、verification 与 workspace/checkpoint state、
permission 与 sandbox boundary、redaction、recovery 以及 parent/child isolation 继续由已有 service
拥有。CM2 不增加 history search 的 schema/indexing、relevance eviction、rollover、automatic summarization、
tool-result eviction、artifact redesign、cross-session browsing、embedding，也不增加新的 CLI/TUI/ACP
surface。

## 兼容性与验证

新 table 是 additive 的，现有 session 仍可读取。聚焦测试覆盖 empty legacy session、重启持久化、
atomic revision-CAS update、stale 和 failed write、有界类型化 snapshot、malformed state、CM1 reference
validation、redaction、非持久 synthetic injection、current-session tool binding、fork/subagent
isolation 以及 schema migration。相关 architecture check、Ruff、format、mypy 和文档 parity 仍是变更
gate 的一部分。
