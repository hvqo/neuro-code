# ADR 0203：DW5b-2 真实 Workflow ADOPT Adapter

**简体中文** · [English](../../en/adr/0203-dw5b2-real-workflow-adopt-adapter.md)

- 状态：已接受的内部 Adapter
- 日期：2026-10-08
- 范围：仅真实 ADOPT；schema 41（执行计量证据）

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

## 可信执行计量

Schema 41 新增三个不可变证据表：`workflow_adoption_executions`、
`workflow_adoption_dispatch_events`、`workflow_adoption_measurements`。
它们保存执行证据，**不是第二套预算账本**；不重写旧 adoption 或 schema-40 Activity
result，也不会把历史 unknown 自动改成精确用量。

`execute_workflow_adoption()` 是窄的首次 dispatch 执行边界，在同一事务中提交
CLAIMED → RUNNING 和 exact execution identity，绑定 invocation/request、确定性
adoption ID、reservation、owner/fence 与 running revision。历史 RUNNING 即使持有
原 owner token，也不能再次进入。入口只接收 composition 注入的真实 mutation 依赖和
controller callback，不接收 amounts、用量时间戳或 receipt。Callback 使用原 Result
Adoption engine 与受控计量 port。Live scope 还要求首次 CAS 后签发的进程内、不可
序列化授权；用数据库 ID 构造 scope/receipt 或重启进程不能创建新的计量 writer。退出
scope 即撤销授权；授权本身不证明调用，仍必须测量真实 entry/ACK 并校验 durable
identity。依赖注入属于可信 composition，不是未受信任 plugin
入口；能够替换 mutation 实现或改写全部数据库事实的任意进程不在此契约内。
普通 hash 仅用于完整性，不是执行权威。

Repository-owned scope 在 dispatch 前保存 intent，直接调用
`WorkspaceMutationPort.apply()`，再单独保存返回/异常 ACK。实际进入 port 后的普通
异常也计一次调用，无论是否改变文件；prepare/validation 不伪造 tool calls。
Intent 本身不证明已经进入 port。Target revision 只用于发现遗漏的计量边界及保守限制
上限，绝不转换为 usage。Callback 绕过计量 port 时，不能为其 APPLYING transition
提交已知零用量。取消或不完整的 scope 不会被重新启动、重新封存。

只有仍在运行的 scope 可在观察到 exact underlying terminal adoption 后完成计量。
Wall usage 来自受控 `perf_counter_ns()`，按毫秒向上取整，覆盖已提交 RUNNING scope
进入后至底层终态观察之间的准备、port 调用及其计量写入开销；不包含 claim 前检查和
后续 Activity settlement。它不依赖调用方的 wall-clock timestamp。Pre-dispatch ceiling
限制新调用，不截断已经进入的调用；实际 overrun 保留真实用量，由既有 ledger 安全地
进入 NEEDS_ATTENTION。Generated tasks/model calls/tokens 保持 0。

历史 reconciliation **不接受** `usage` 或 receipt。Known usage 只能在首次执行的 live
scope 中结算，须校验不可变 execution identity、连续 intent/ACK 对、exact plan
mutation 和 terminal adoption digest。完整 measurement、immutable Activity result、
budget settlement、journal **在同一 SQLite 事务提交**。公共 reconciliation 可 replay
已经提交的 terminal result，但不能用序列化 receipt 新建 known usage；孤立 measurement
没有对应 Activity result 即非法，普通 hash 即使正确也 fail closed。

执行或 terminal settlement 在该事务 commit 前崩溃时，tool/wall 保持 `None`；空 plan
可独立证明零 port calls，但不能证明 wall usage。即使已有所有调用 ACK，没有完整原子
结算也不能建立精确 wall 消耗。Commit 后 ACK 丢失则 replay 同一 known terminal fact，
不执行、不重复扣费。Intent 条数、target revision、desired image 均不是执行 receipt。

Measurement、Activity result、budget settlement、journal 在既有单事务提交，exact replay 不重复
结算。正常两次或零次调用完成后，known-accounted Run 保持 WAITING；后续独立
Interpreter tick 直接消费 output，无需人工 accounting/resume。Unknown 或 exceeded
进入 NEEDS_ATTENTION；reconciliation 只补未知 ledger 字段，immutable Activity usage
保留原始执行事实。Late accounting 不重新打开 CANCELLED/terminal Run。

Terminal reconciliation 是不依赖 Activity owner 的历史事实读取。它检查 exact source/
parent binding，不要求 live Projection 校验、lease、worktree、checkpoint 或原 parent
HEAD。通过 immutable SQLite Expansion/publication journal、consumed output、
Projection fingerprint/source snapshot，独立验证 exact DAG generation/definition、
frozen members 与 worker identities。重算修改后的 adoption plan fingerprint 不能替代
这些独立锚点；历史锚点缺失则 fail closed。不 claim ownership，不 mutation workspace。
Terminal recovery read 同样执行 provenance 与保守 usage 校验。Nonterminal recovery
继续严格检查 live source 和 parent identity，复用既有 core owner liveness/lease/CAS；
另一活跃 core owner 仍返回 busy。

## 活跃执行与恢复仲裁

Adoption terminal 不能证明 Activity execution scope 已结束。以 canonical 本地
SQLite 路径和稳定 invocation identity 为 key 的非阻塞 OS 排他锁，覆盖首次
RUNNING/execution identity 提交、prepare/adopt、port 调用、terminal observation、
measurement/result/ledger/journal 原子结算，直到 scope exit。POSIX 使用 `flock`，
Windows 使用 `msvcrt` 单字节锁。外部执行期间不长期持有 SQLite 写事务。

公共恢复先获取同一锁，再进入 `BEGIN IMMEDIATE` 并重新读取全部状态和 proof。
锁仍被执行者持有时返回 `concurrent_execution`，Adapter 转为有界 `busy`，不修改
文件、计费或 Run。Deadline、PID 猜测和 lease 都不能覆盖此锁。底层事实尚不存在的
RUNNING 恢复标记也受锁保护。首次执行 task 可以在持锁期间标记自己的不确定 ceiling；
此 task-local 记账不能替代 OS 仲裁，child task 不能借用它。

进程死亡或明确退出 scope 后，OS 释放锁。若没有已原子提交的完整 result，恢复才能
保守结算 unknown；不签发新的 execution scope 或再次 dispatch 权限。孤立的序列化
measurement 仍 fail closed。Immutable terminal Activity replay 仅验证并读取 durable
facts，不等待执行资源存活、不改写 usage、不要求 preserved resources。完整 known
结算必须先于锁释放；外层 ACK 丢失只读取同一个 known result。

锁文件释放后保持未锁定，数据库使用期间不 unlink；删除重建 inode 会破坏排他性。
文件不保存 receipt、owner authority 或 usage，不是第二账本，也不是永久持有的锁。
该机制用于共享同一本地数据库的协作进程，不是 Python dependency 的 OS 安全隔离，
也不是分布式文件系统协议。Schema 保持 41，不承诺跨 SQLite 与文件系统的 exactly-once
原子事务。

## Crash 边界

| 窗口 | 恢复 |
|---|---|
| RUNNING 已提交，无 adoption fact | Scope 活跃：busy；scope 已遗弃：不 redispatch，保留 reservation 并进入 NEEDS_ATTENTION |
| Plan 已提交，mutation 未开始 | 同一 adoption ID/plan，live validation 与既有 core ownership recovery |
| Intent 已保存／mutation 已发生但 ACK 丢失／scope 不完整 | Unknown accounting；既有 target CAS 与 desired-image observation 防止重复写 desired image |
| Adoption terminal，Activity result 缺失 | 执行锁仍持有：busy，不结算；scope 退出或进程死亡后：unknown 原子结算，不要求 live resources、不 mutation |
| Activity result 或 Interpreter consume ACK 丢失 | 精确 durable replay，不重复结算或 adoption |
| 第三方修改 parent | 既有 three-way/conflict/indeterminate 检查，不覆盖新内容或无关脏文件 |

结算失败时 measurement、budget、attempt、result、journal 一起回滚；底层 adoption terminal fact
仍可供下一次 reconciliation。不承诺跨数据库事务或 exactly-once 外部文件操作。

## 兼容与排除项

Schema 40 → 41 只新增三个证据表与不可变约束，不重写历史。不使用保留 source binding 的既有 DW5b-1
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
