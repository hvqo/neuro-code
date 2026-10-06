# ADR 0198：DW3 Workflow expansion 原子发布

**简体中文** · [English](../../en/adr/0198-dw3-atomic-workflow-dag-publication.md)

- 状态：已接受发布基础
- 日期：2026-10-06
- 范围：已确定 intent → immutable DAG publication；尚无 Interpreter

## 不可变批次与窄接缝

`application.ports.workflow_publication.WorkflowPublicationStore` 接收冻结的
`WorkflowExpansionIntent`，通过既有 SQLite session store 发布新的 `TaskDag`。
复用 DAG 的 8 nodes、16 edges、每节点 4 dependencies、最多 4 parallel 上限。
每次 expansion 使用新 DAG id，既有 graph definition 不被修改。
Scheduler、Leader、Worker、Permission 与 Sandbox 路径保持原有行为。

调用者提供已解析的任务 prompt 和 input fingerprint。DW3 校验声明的 TaskBatch/Map
任务覆盖、依赖形状、route、Repeat/Map 身份 scope 与 parallelism。
它不展开 prompt、不判断 branch、不推进 Repeat、不读取真实 result、不生成 Map items。
Profile/capability intent 保留在不可变 Definition 中，不赋予执行权威；后续 adapter
仍须经过既有权限边界解析。

## Expansion identity 与冻结成员

显式、全局唯一的 `expansion_id` 绑定 run、StepIdentity
`(step_id, iteration, item_key)`、input fingerprint、冻结 members 和 DAG definition
fingerprint。每个 `ExpansionMember` 绑定 opaque member key、声明的 task id、DAG node id
及 input fingerprint。按 `(member_key, task_id)` canonicalize，DAG ordinal 必须遵循
该顺序。TaskBatch 使用固定 member key `batch`；Map 使用调用者提供的 opaque item key。
每个 member 对应一个 DAG node，每个 item 包含完整 template task set，不截断成员。
TaskBatch/Map 只允许收窄声明的 parallelism。

member-set hash 与 identity hash 使用 canonical UTF-8 JSON/SHA-256。
完整 immutable intent 还绑定 parent session、prompt、dependency order、max_parallel 与
UTC DAG 创建时间。retry 比较完整 payload，包括既有 DAG fingerprint 未覆盖的
max_parallel。同 expansion id 不同 payload 拒绝；同 run/step instance 不允许通过
更换 expansion id 再发布。新的 Repeat iteration 或 Map item 使用不同 StepIdentity。
已单独写入的 DAG 即使 definition 相同，也不能被 publication 接管。

## 单事务与持久 linkage

Schema 36→37 仅新增 `workflow_expansions`。外键关联既有 run、step instance、DAG 和
publication journal generation；run/step/DAG 删除受限，journal 外键延迟至 commit 校验。
DAG id 与 run/step 的唯一键防止重复发布；update/delete trigger 保留不可变 expansion
证据。Migration 使用既有串行 SQLite migration transaction。

Adapter 在一个 `BEGIN IMMEDIATE` transaction 内完成：

1. 检查 Run generation、owner identity/fence；仅 RUNNING/WAITING 能发布新工作。
   NEEDS_ATTENTION 和终态不能发布。
2. 校验成员与全新 DAG identity。
3. 对新 node count 预留并结算 generated-task 预算。
4. 用当前 connection 插入 DAG/nodes，无中间 commit。
5. 增加 Run generation，保存 WAITING position 和 WAITING step instance。
6. 插入 immutable expansion 及一条有界 `dag_published` journal fact。

Expansion 表承担 durable DAG linkage；waiting reason 只用于可读提示。
其他未解决 Step 仍保留在 snapshot 中。Journal 保存 hash、linkage 和 task count，不再
复制整组 prompt，因此既有 32-KiB journal 上限保持不变。读取使用同一 WAL snapshot，
校验 intent hash、DAG definition/parallelism、Step input、publication accounting 和
对应 journal。DAG lifecycle 可通过既有 CAS API 演进，definition 与 publication
identity 不变。

## Retry、crash 与预算

`expansion_id` 就是 publication request identity，其 SHA-256 命名 journal event 和
预算 reservation。精确 retry 返回原 expansion/DAG 身份及当前 lifecycle snapshot，
标记 `replayed=True`。Retry 可使用不同 CAS metadata 与调用时间：它只确认既有事实，
没有写入，也不授予 stale owner 推进权。新的 publication 必须通过当前 generation/fence。
调用者必须保留原始 immutable intent。

Commit 前失败会整体回滚 DAG/nodes、Expansion、Workflow linkage、reservation/consumption
及 journal。Commit 后响应丢失通过同 identity 恢复，不猜测接管 orphan DAG，不重试副作用，
不承诺 exactly-once worker execution。

Generated-task 使用 DW2 run ledger，在同一事务结算实际插入的 node count；本次纯发布操作
不执行 Model/Tool，不消费 token/wall 预算。既有未结算容量、unknown usage、收窄 ceiling
和 reservation 数量上限继续生效。并发发布或预留不能超卖；retry 不重复预留/消费。

## 验证与留项

数据库回归覆盖单批次、7-item fan-out、成员顺序、非法上限、冲突 retry、竞争进程、
stale generation/fence、预算竞争、六个失败窗口、重开一致性、migration rollback 和
既有 DAG lifecycle 兼容。Publication 不创建 SessionTask、worker、lease 或 tool。

DW4+ 仍负责 Interpreter、真实结果 projection、branch/repeat 决策、activity adapters
与 completion proofs。DW3 尚无自动 Workflow execution、Runtime/Planner/UltraCode/TUI
接入或第二套 scheduler；Issue #165/#167 仍独立处理。
