# ADR 0199：DW4a 持久化 typed result projection

[English](../../en/adr/0199-dw4a-typed-result-projection.md) · **简体中文**

- 状态：已接受持久化基础
- 日期：2026-10-06
- 范围：精确终态结果事实；不执行 Workflow，不做 Adoption

## 来源与身份

`WorkflowProjectionStore.project_workflow_result()` 只接受 expansion id、预期
run/session scope、时间戳，以及可选的预期 source hash。调用方不能传任意 DAG、
response 或 output dict。在同一 SQLite snapshot 中重新验证 DW3 expansion、
definition、冻结成员、publication journal、budget linkage 和 exact DAG definition。
DAG 与所有 node 都必须终态；COMPLETED 只能包含 completed node，其余 FAILED、
CANCELLED、INDETERMINATE 遵循现有 scheduler 的终态分类。

Projection identity 来自完整 expansion identity。Source fingerprint 绑定
Definition/Run/Session、Step/iteration/item、input/member hash、exact DAG id、
definition、max_parallel、终态 DAG/node generation、节点定义、状态/错误/workspace
元数据、精确 result fingerprint 和 worker linkage。Completed node 必须对应同一
持久化 SessionTask、child session、workspace lease、checkpoint、parent relay
（含完整性摘要）、final workspace fingerprint 和 changed-file count。不读写工作区文件。

## 完整 response，而非 preview

现有 DAG `response_preview` 限制为 8 KiB，截断不带标记。可写 worker result 已经过
脱敏，限制为 32 KiB，并有 `truncated` 标记。Preview 或截断的 worker result 都不能
满足 DW1 的 `response` 字段。

Schema 37→38 增加 `task_dag_result_evidence`，在现有 terminal node CAS 的同一事务
中仅为 DW3 已绑定的 DAG 保存精确返回的脱敏 worker response 和截断证据。普通
DAG 保持原有存储与会话清理行为。证据绑定 worker/child identity、
node generation、完整 node snapshot hash 和 canonical payload hash。不改变执行、
scheduler、脱敏或 worker response 上限，不引入 judge。截断结果拒绝投影；未来完整
artifact contract 属于独立改动。Legacy/recovery completed node 缺少 exact evidence
也拒绝，不从 preview 或猜测的 session turn 重建。

Failed/cancelled/skipped/indeterminate node 没有产生结果时，`response` 明确为空字符串，
source facts 记录 `not_produced`；有 preview 但缺少 exact evidence 时拒绝。
Status 始终是实际终态值，保留失败与不确定性，不补默认成功。
`response` 是完整的脱敏 worker-contract 文本，不是未脱敏模型 transcript、事实正确性
判断或 verification PASS。

## 复用 DW1 output schema

`workflow_output_schema()` 暴露 DW1 ResultRef validator 使用的同一 schema。
TaskBatch 保持 `tasks.<task_id>.{status,response}`；Map 保持
`{count,items:[{tasks:...}]}`。按冻结 member/task/node binding 映射任务；Map item
按 canonical member key 排序，source facts 保存 key，供未来 item selector 使用。
不猜 node 顺序，不建立第二套 schema。严格验证 required fields 和 scalar 类型，
不 coercion。Canonical UTF-8 JSON 使用排序 object key 与确定性的 SHA-256。

## 持久化、重试与边界

`workflow_result_projections` 每个 expansion/DAG 仅保存一个 immutable fact。
两张新表使用 restrictive FK 和禁止 UPDATE/DELETE 的 trigger。
串行 `BEGIN IMMEDIATE` 完成来源验证与插入；失败全部回滚，并发调用收敛到一份事实。
Exact retry 返回原始结果（含原 timestamp）；source 或持久化 payload 改变时完整性
检查拒绝，预期 source hash 不符时 conflict 拒绝。Restart 读取重新验证 linkage/hash。
Result JSON 上限 1 MiB，worker response 32 KiB，保留既有 8-node 上限。
Hash 用于发现意外损坏与 stale source，不能防御可重写整个数据库及所有摘要的攻击者。

Projection 本身是 durable fact，不是新的 journal/control transition。不改变 Workflow
Generation、Owner、Step status、Budget、Waiting reason 或 Journal；不执行
Branch/Repeat/Map、不发布工作、不宣告 Workflow COMPLETED、不提升 Permission/Sandbox
权威、不修改父工作区。Schema-valid 不等于事实正确。Verification/completion requirements
由后续 Interpreter 负责。DW4b 才可通过现有权威边界做 completed-DAG adoption；
DW4a 没有调用 Adoption、Activity、Planner 或 UltraCode adapter。
