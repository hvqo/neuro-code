# ADR 0203：DW5b-2 真实 Workflow ADOPT Adapter

**简体中文** · [English](../../en/adr/0203-dw5b2-real-workflow-adopt-adapter.md)

- 状态：已接受的内部 Adapter
- 日期：2026-10-08
- 范围：仅真实 ADOPT；schema 保持 40

## 精确来源与身份

`WorkflowAdoptActivityAdapter` 是显式调用的应用服务，不是 Interpreter 回调或
scheduler。真实 ADOPT 声明保留输入名 `source`，其值必须是直接 DW1 `ResultRef`：
`TaskBatch.tasks` 或 `Map.items`，只选择整个生产批次。Literal JSON、调用方 DAG ID、
latest DAG、成员 selector 和 Repeat aggregate selector 均不授予 adoption 权威。
Repeat 内直接生产步骤使用 invocation iteration；外层支配步骤使用 iteration zero，
与 DW5a 解析保持一致。

Resolver 校验不可变声明、已完成 producer StepIdentity、已消费 PROJECTION 索引及
消费 journal。它用 invocation 冻结的已解析输入重建 DW1 output shape，仅用于验证
已消费 output fingerprint，不从业务 response 内容提取来源身份或成功判据。
索引提供 projection ID/fingerprint，producer identity 提供确定性的 DW5a expansion ID。
真实执行再通过 `WorkflowCompletedDagSourceAdapter` 校验 exact Run、Expansion、
Projection、DAG/node generation、parent session 与 preserved lease facts。

确定性的 `adopt-<sha256>` 绑定 Run、invocation、StepIdentity、request fingerprint、
精确 Workflow source reference 和 parent session。复用 `WorkflowAdoptionSourceRef`、
`ResultAdoptionRequest`，不伪造 Swarm row、不建立身份别名或第二套 adoption engine。

## 授权 dispatch 与有界 operation

Factory 使用既有 parent binding 与 mutation port 构造既有
`ResultAdoptionApplicationService`。原 capability、worktree、checkpoint、three-way、
path、protected-file、symlink、conflict 和 sandbox 检查继续拥有最终权威。
所有写入经过该 mutation port，Adapter 不直接写文件或运行 shell；Interpreter tick
只发布或消费事实。

对于新的 READY invocation，先校验 source/parent authority，然后原子提交 claim 和
预算预留。只有本次调用成功完成 claim 和 RUNNING commit 才授权首次 dispatch。
Historical claim/start ACK replay 不是 dispatch 许可。另一 owner 的 CLAIMED 返回 busy，
不借用其 token。没有通用 Activity reset、retry 或 takeover。

每次 mutation-port operation 前，有界 wrapper 检查 Run 仍 WAITING 此 invocation、
冻结 plan/session/target 一致、新的 durable target dispatch revision 存在，以及
operation/wall ceiling 尚有余量。默认预留 64 operations（现有最大 target 数）和
30,000ms；composition 可选择更小上限，wall 最多 300,000ms。Deadline 固定于原
RUNNING 时间，restart 不续期。Target revision 保守约束先前 dispatch attempts，
包括不确定 crash；不能将它转成伪造的精确用量。历史 APPLYING target 本身不能授权
再次调用 port；desired-image observation 可以无重复写入地完成它。
额度耗尽或无法证明时保留 RUNNING 与 outstanding reservation，标记 NEEDS_ATTENTION，
不自动 retry。

Wall ceiling 限制 dispatch 开始，不强制中断已运行的平台 mutation；底层
permission/workspace/sandbox port 保留其执行限制。不增加 timer、轮询或无限预算。

## Terminal proof 与 reconciliation

窄端口 `WorkflowAdoptionStore.reconcile_workflow_adoption` 在 Activity 结算的同一个
SQLite transaction 中读取真实 underlying terminal record，不接受任意 source hash
或伪造 Activity owner。绑定 ADOPT 不能通过 generic `finish_workflow_activity`
虚构 terminal fact。普通恢复读取也验证 underlying proof。

Proof 绑定 adoption ID、不可变 plan fingerprint、terminal state/version、有序的
target/evidence digest、`parent_workspace_changed`、terminal timestamp 与 usage。
COMPLETED 要求 targets 已 applied 且 evidence 是 desired image。
Activity result 另绑定 exact invocation/request。映射如下：

| Adoption | Activity | 语义 |
|---|---|---|
| COMPLETED | COMPLETED | DW1 输出 `status=completed` 和 durable changed flag，允许真实 false |
| CONFLICT | BLOCKED | Parent/overlap conflict 不是成功，不自动 repair |
| FAILED | FAILED | 保留底层失败 |
| INDETERMINATE | INDETERMINATE | 不确定副作用需关注 |
| Nonterminal / busy | 无 terminal result | 保持既有 core ownership 和 forward recovery 权威 |

创建 plan 之前的校验错误继续返回错误，不虚构 terminal adoption evidence。

成功的有界首次 dispatch 记录实际 mutation-port 调用数和 wall time；generated tasks、
model calls 和 tokens 可证明为零。Crash/restart 后丢失的 operation/wall 测量保留为
`None`（空 plan 可证明零 operations）。既有 Run ledger 仅结算一次，保留真实 overrun/
unknown，不能重新打开 CANCELLED Run。既有显式 reconciliation 只能补未知 ledger 字段；
原 immutable Activity result/proof 不变，不增加第二套账本。

Terminal reconciliation 是不依赖 Activity owner 的历史事实读取，检查 exact source/
parent binding 与持久化完整性，不要求 live Projection 查询、lease、worktree、checkpoint
或原 parent HEAD，既不 claim ownership，也不 mutation workspace。
Nonterminal recovery 继续严格检查全部 live sources 与 parent identity，并依赖既有
adoption core owner liveness/lease/CAS。另一活跃 core owner 仍返回 busy。

## Crash 边界

| 窗口 | 恢复 |
|---|---|
| RUNNING 已提交，无 adoption fact | 不根据 RUNNING replay 重新 dispatch；保留预留并 NEEDS_ATTENTION |
| Plan 已提交，mutation 未开始 | 同一 adoption ID/plan，live validation 与既有 core ownership recovery |
| Mutation 已发生，ACK 丢失 | 既有 target CAS 与 desired-image observation；不重复写已有 desired image |
| Adoption terminal，Activity result 未提交 | 读取 terminal proof 并原子结算，无 live-resource 依赖或 mutation |
| Activity result 或 Interpreter consume ACK 丢失 | 精确 durable replay，不重复结算或 adoption |
| 第三方修改 parent | 既有 three-way/conflict/indeterminate 检查，不覆盖新内容或无关脏文件 |

结算失败时 budget、attempt、result、journal 一起回滚；底层 adoption terminal fact
仍可供下一次 reconciliation。不承诺跨数据库事务或 exactly-once 外部文件操作。

## 兼容与排除项

Schema 40 不变，无 migration 或历史重写。不使用保留 source binding 的既有 DW5b-1
generic protocol fixtures 仍可读。Swarm/UltraCode 共用原样的 Result Adoption engine、
owner 与 recovery 语义。DW3 task accounting、Interpreter two-phase 和 Runtime 不变。

COMPLETED adoption 和 `parent_workspace_changed` 不代表 verification PASS。
ADOPT adapter 结算不消费 output、不推进 Step/Run、不发布 DAG、不完成 Workflow；
只有既有 ledger 可因未知账目将 Run 置 NEEDS_ATTENTION。后续 Interpreter tick 才消费
Activity fact。VERIFY、REPAIR、completion requirements、Planner、CLI/TUI wiring 和
自动 adapter dispatcher 均留到以后。

## 证据

Focused tests 使用真实 SQLite、临时 Git repository、既有共享 adoption engine 与注入的
workspace mutation ports，覆盖 TaskBatch/Map、真实 parent 文件内容、无关脏文件保留、
无变化结果、精确 source/parent 拒绝、stale resources、并发 owner、dispatch ceilings、
terminal proof tamper、rollback、cancel、ACK-loss 窗口与零重复 mutation。
既有 Activity、publication、projection、Interpreter、adoption、Swarm/Replan/UltraCode、
security 和 migration 回归仍须通过。
