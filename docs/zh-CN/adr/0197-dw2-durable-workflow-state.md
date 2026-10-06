# ADR 0197：DW2 Workflow durable state 与持久化

**简体中文** · [English](../../en/adr/0197-dw2-durable-workflow-state.md)

- 状态：已接受持久化基础层
- 日期：2026-10-06
- 范围：持久化 control facts；尚无 Workflow 执行路径

## 身份与存储

DW1 definition 保持不可变。同一个 session SQLite 数据库在串行 migration 事务中
从 schema 35 升级到 36。五张表分别保存 `workflow_definitions`、`workflow_runs`、
`workflow_step_instances`、`workflow_budget_reservations` 和
`workflow_transition_journal`。现有 `SqliteSessionStore` 组合一个 `WorkflowsMixin`，
实现 canonical `application.ports.workflow_state.WorkflowStateStore` 端口。
没有 bootstrap、Runtime、scheduler、CLI 或 TUI consumer 接入。

Definition 使用 canonical IR 的 SHA-256 作为 key，保存精确 canonical text、
IR schema version 与 compiler version（当前为 1）。重复 insert 只有在全部字段一致时
才接受。SQL update trigger 额外保护不可变 definition 与 journal。Definition id
只是声明名称，可以存在多个不可变 fingerprint。`run_id` 独立标识一次使用，不能等于
其 definition fingerprint；parent session 与 input fingerprint 不可修改。输入和结果
不进行求值。

Step identity 是 `(step_id, iteration, item_key)`，从确定性的 JSON tuple 派生 hash。
普通 step 使用 iteration 0；声明的 Repeat 子 step 使用 1–3；Map template instance
必须有 opaque item key。Repository 根据已保存的 definition 检查这些 scope，不展开
或执行流程。每个 run 最多 256 step instances 和 256 budget reservations。Journal 单条事实
上限为 32 KiB，读取分页；不使用总事件数上限阻断迟到记账或取消。Artifact result 是有界完整性引用，不是可执行路径、权限或 adoption
成功证明。

## Snapshot、journal 与原子性

Run status 包含 `READY / RUNNING / WAITING / COMPLETED / FAILED / CANCELLED /
NEEDS_ATTENTION`。Snapshot 拥有 generation、owner/fence、control position、UTC
accounting timestamps、状态与有界诊断事实；只更新 status 会保留 control position，
显式 position 才会替换。step/reservation 子行属于同一 snapshot。
多表读取采用同一个 WAL transaction，避免旧 run 与新子行混合。

每次成功写入均使用 `BEGIN IMMEDIATE`、generation CAS，并在同一事务写入 journal。
失败会回滚全部表。Journal 保存有界 typed facts：create、claim/resume、run/step
transition、branch choice、iteration、reservation、consumption、reconciliation。
Branch decision 不可改写；Repeat iteration fact 每次只推进一轮。这些检查保存声明的
控制事实，不求值 condition，也不发布 Task DAG。

采用 snapshot + audit evidence，而非完整 event sourcing：恢复直接读取 snapshot，
不会增加第二个从任意 event 重建权限的 interpreter。Journal 每页最多 100 条。
Request id 在 run 内唯一，fingerprint 覆盖精确 canonical payload，包括 CAS expectation
和 timestamp。完全相同的重试返回 `replayed=True`、原 committed generation 与当前
snapshot，不重复应用命令或增加 generation。同 id 不同 payload 拒绝。调用方必须
跨重试保留原 request identity/payload，不能在不确定是否提交后随意生成新 id。

## Owner fence 与 crash recovery

Claim 是显式 CAS，同时检查 expected generation 与 previous owner fence。
首次 claim 将 READY 转为 RUNNING。后续每次 claim 增加 fence，并记录 NEEDS_ATTENTION，
包括同 owner 的重启 claim。旧 owner 不能使用过时 fence 推进。没有基于时钟过期的
lease 推测旧进程或副作用已经停止。接管保留 steps、running/waiting facts、结果及未决
预算，不重置或重放工作。

Fence 只保护 durable control write，不承诺 exactly-once shell/tool，不停止外部进程，
不授予 Permission/Sandbox authority。未来恢复必须先核对证据，再决定副作用能否重试。
Terminal run 不再推进，但相同 request 重试仍可读取确认。
迟到的 consumption/reconciliation 可更新其 ledger，但不会重开 run。Cancel/failure 可以保留未完成
step/reservation 作为恢复证据；completion 拒绝未完成 step、未决 reservation 与未知消耗。

## Durable budget ledger

Ledger 覆盖 generated tasks、model/tool calls、input/output tokens 和整数 wall
milliseconds。Run ceiling 默认来自 DW1 声明，只能收窄；它不分配或扩大 global/Profile
授权。与真实 Runtime ceiling 的交集以及实际消费接入留到后续。

稳定 reservation id 保存已知预留上界与 UTC 创建时间。Settlement 保存实际 usage
和结算时间。未结算 reservation 的 consumed aggregate 同样保持未知，预留容量不代表实际消耗为零。显式 zero 只能作为调用方提供的 accounting fact；缺失/未知 consumption
使用 `None` 并传播到 aggregate。未知消耗持久化并阻止继续预留/正常推进，直到独立的
typed reconciliation 填补未知维度；已经确认的值不能静默修改。真实 overrun 会持久化，
而非拒绝记录后丢失事实；它使 run 进入 NEEDS_ATTENTION，并阻止正常继续推进。

Reservation/settlement timestamp 与 durable wall-millisecond amount 支持未来跨进程
重启的时间 accounting。DW2 不启动时钟，不测量真实 Model/Tool，不使用新进程 monotonic
clock 猜测消费。Crash 后未决 reservation 保留，绝不自动释放为 zero。

## 验证与留项

数据库回归覆盖 migration rollback/旧数据库升级、真实竞争 OS processes、旧 generation /
fence、重复与冲突 request、snapshot+journal rollback、immutable records、step scope、
branch/iteration facts、有界 ledger、unknown/overrun accounting、SQLite reopen。
测试同时确认不发布 DAG/session task。删除 session 会 cascade run-owned facts，immutable
definition 保留。Workflow state 不进入 session export/fork copy，也不进入 FTS。

DW3 仍负责 Workflow expansion 与 Task DAG atomic publication、effect adapters、output
projection、Runtime accounting 和 recovery proof；DW2 没有这些执行入口。
DW1 极深 typed IR hardening 继续独立记录在 Issue #165。
