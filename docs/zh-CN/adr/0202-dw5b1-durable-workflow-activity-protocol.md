# ADR 0202：DW5b-1 持久化 Workflow Activity 协议

**简体中文** · [English](../../en/adr/0202-dw5b1-durable-workflow-activity-protocol.md)

- 状态：已接受的内部基础
- 日期：2026-10-07
- 范围：协议、持久化和有界 Interpreter 消费；不接入真实 Activity adapter

## Interpreter 与 Activity ownership

Interpreter 在一个 tick 初始化 READY Step，下一 tick 解析 DW1 typed input，发布
`WorkflowActivityInvocation`。invocation、Step WAITING、Run WAITING 和 ACTIVITY
journal 在同一 SQLite transaction 提交，`advance_once()` 立即返回。后续非终态 tick
返回 `activity_waiting`、`progressed=False`，不写入、不预留预算、不 dispatch。

外部 owner 显式使用窄 `WorkflowActivityStore` port。Activity owner/fence/revision 与
Workflow control owner 分离。本阶段不新增 daemon、timer、scheduler、model/tool 请求、
filesystem mutation 或长期调用。Ownership 只协调未来 adapter，不授予 Permission、
Sandbox、Workspace、Branch 或 Repeat 权威。

## 身份与不可变结果事实

invocation ID 复用 Run + StepIdentity + input fingerprint 的 SHA-256。
StepIdentity 包含 step、iteration、item key。不可变 invocation fingerprint 进一步绑定
definition、parent session、ActivityKind 和 exact bounded canonical request。
DW1 reference schema 校验解析后的输入；literal/artifact 必须与声明值精确一致。
retry 时间不改变 request identity；同 ID 不同 request 拒绝。

`WorkflowActivityResult` 绑定 invocation/request/kind、terminal state、source
ID/fingerprint、UTC terminal time、actual usage，以及仅 COMPLETED 可有的 canonical
output。Source 是 provenance，不是执行或验证权威。COMPLETED output 继续通过既有
`activity_output_schema`；schema-valid JSON 不能替代 durable result，不建立新 output schema。

## 状态与副作用起点

`READY → CLAIMED → RUNNING → COMPLETED / FAILED / BLOCKED / INDETERMINATE`。
CLAIMED 也可在 dispatch 前进入 FAILED/BLOCKED/INDETERMINATE；COMPLETED 要求先跨越
RUNNING。Revision CAS、SQLite writer serialization 与不可逆状态 trigger 保证只有一个
claimant；Activity fence 独立于 Workflow fence，不提供 generic reclaim/reset/retry。

READY 只代表 invocation 存在。CLAIMED 表示 owner/reservation 存在，但尚未启动副作用。
Adapter 必须先持久化 RUNNING，再跨过底层 side-effect-start boundary。刚写 RUNNING
便 crash 时，实际可能未开始，但协议保守地认为副作用可能已开始。进程消失不能证明可
重试；未来 adapter 必须证明底层结果，或记录 INDETERMINATE。

Exact claim/start ACK replay 只返回既有事实；RUNNING replay 不允许重复执行。
Terminal replay 校验持久化 invocation/result/schema/budget/journal，不要求 execution
process 或原 Activity owner 存活；不同 result/source 仍拒绝。

## Schema 40 与事务

Schema 39→40 新增 `workflow_activity_attempts`、不可变
`workflow_activity_results` 和有界本地 `workflow_activity_events` journal（每个
invocation 最多四条生命周期事实）。既有 output CHECK 增加 `activity`，迁移保持旧
row/fingerprint 原样，包括 `fake_activity`。读取校验索引身份、snapshot digest、
publication generation linkage、local journal continuity 和 terminal evidence。
不可变 trigger 阻止终态修改/删除以及 invocation 改写。

Workflow ACTIVITY fact 明确区分发布与消费；Activity-local fact 记录 claim/running/
terminal revision 和 snapshot fingerprint，不记录 raw output、文件内容或秘密。
Run budget 继续使用 RESERVED/CONSUMED discriminator。这是 snapshot + recovery evidence，
不是第二套 event sourcing 框架，也不是 Runtime authority。

Publication 与 Step/Run/journal 原子提交。Claim 与既有 Run budget reservation 原子提交。
Result settlement、terminal attempt、immutable result 和 journal 原子提交。任意失败
全部回滚；commit 后 ACK loss 可 reopen 读取，不创建第二 invocation、不重复计费。

## Budget 与后续 tick 消费

复用 `BudgetAmounts`、`BudgetReservation` 和 Run ceiling。Owner 必须在 claim 时、
dispatch 前预留 known upper bounds；超过 ceiling 则 claim 回滚。Publication 本身不
消费 model/tool 预算。Adapter 必须显式用 `None` 表达未知 usage，不能将缺失字段补零。
Crash/restart 后 outstanding reservation 保持 outstanding。

Terminal settlement 保存真实消耗，即使超过 reservation/ceiling。Unknown committed
usage 或 ceiling overrun 使 Run 进入 NEEDS_ATTENTION；late accounting 不重新开启
CANCELLED/FAILED Run，replay 不重复计费。既有显式 ledger reconciliation 可精化
unknown 字段，不修改已知消耗，也不改写原不可变 result。

后续 tick 遇到 COMPLETED，在同一事务提交 `OutputKind.ACTIVITY`、Step COMPLETED、
Run RUNNING 和 ACTIVITY consumption fact，并重新验证 exact durable result 与预算。
FAILED 原子失败 Step/Run；BLOCKED/INDETERMINATE 进入 NEEDS_ATTENTION，不自动 repair/
retry。WAITING Step 缺少 invocation 为 integrity failure。Consumption ACK loss 根据
既有 Step/output 恢复，绝不重新执行 Activity。

## 兼容与排除项

Legacy FAKE_ACTIVITY output 保持可读。旧 deterministic fake 只作为纯测试 helper，
不再是 production Activity 默认路径；测试显式驱动外部 owner。Branch/Repeat/Map、
DW3 publication、DW4 projection/adoption、exact Profile/capability binding 保持不变。

本阶段不实现 ADOPT/VERIFY/REPAIR adapter。不修改 Result Adoption、VerificationTracker、
AgentRuntime、Task DAG scheduler/Leader/Worker、Swarm、Replan 或 UltraCode。
Activity COMPLETED 和 schema-valid output 均不等于 verification PASS 或 Workflow
COMPLETED。DW5b-2 单独实现 adapter 的 durable source checks、真实 authority boundary
和安全 reconciliation。

## 验证

真实 SQLite 测试覆盖 publication/claim/settlement/consumption 回滚、ACK loss/reopen、
无 sleep 的竞争进程、stale owner/fence、不可变终态、request/result tamper、typed schema、
unknown/overrun budget、dispatch 前 cancel、historical replay、schema-39 fake output
兼容。DW5a control 和既有 Workflow/Task DAG/Swarm/UltraCode 门禁继续保留。
