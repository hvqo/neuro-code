# ADR 0201：DW5a 持久化 Workflow Interpreter

**简体中文** · [English](../../en/adr/0201-dw5a-durable-workflow-interpreter.md)

- 状态：已接受的内部基础
- 日期：2026-10-07
- 范围：有界持久化控制与确定性 fake Activity

## Ownership 与单步模型

`DurableWorkflowInterpreter.advance_once(run_id, expected_generation, owner_id,
owner_fence, updated_at)` 每次最多选择一个持久化动作。不执行 DAG，不等待 worker，
不循环到完成。IR 的声明顺序、已完成 step instance 和 BRANCH/ITERATION 事实决定
下一动作。遍历最多 256 个声明，journal 最多读取 40 页、每页 100 条；这是读取上限，
不是执行循环。等待中的 tick 返回无进展，不写重复事件，由调用方显式安排后续 tick。

所有写入经过 DW2 CAS 或 DW3 原子发布接缝。确认丢失后，调用方必须重新读取当前
 generation。旧 generation、owner 或 fence 在引用解析、Activity 计算之前停止。
DW3 replay 只是历史事实读取，不能代替推进或 ownership。替换 owner 保持 DW2 的
NEEDS_ATTENTION，必须通过已有机制显式 reconciliation；Interpreter 不自动豁免。

## Typed values 与 schema 39

Schema 38→39 仅新增 `workflow_run_inputs`、`workflow_step_outputs`，使用禁止
UPDATE/DELETE 的不可变 trigger。输入是通过 DW1 schema 验证的 canonical JSON，
SHA-256 必须等于已有 Run input fingerprint。创建 Run 后、claim ownership 前使用
`put_workflow_input` 固化。创建与附加输入之间 crash 会留下不可执行 Run；缺少输入时
不使用调用方临时字典或默认值伪造恢复语义。旧 Run 仍可读取，没有原始不可变输入
snapshot contract 时不得执行。Generation=0 且缺输入的 Run 是 uninitialized durable fact；
未来 entrypoint 必须先附加相同 immutable input，再 claim/execution。

InputRef 从 snapshot 读取。TaskBatch/Map ResultRef 只读取已消费 step 精确绑定的
DW4a Projection，重新验证 Run/Expansion/DAG/member/node evidence；不回退到 preview、
latest result 或 transcript。output 表只保存 Projection identity/fingerprint，不重复
保存 worker response。Activity output 和空 Map 使用同一 DW1 schema、显式本地 provenance，
不冒充 worker result/projection。Repeat 的 `iterations/last` 或已证明的 iteration-1
selector 从持久化迭代事实及精确 body output 派生，不新增 schema。ItemRef 只在单个
Map item 内有效。Literal/ArtifactRef 仅是数据，完整性引用不读取文件、不授予权限。

## Publication 与 Map

首次遇到 step 固化 READY 和已解析输入的 canonical digest。下一 tick 构造确定性的
新 immutable Task DAG 并调用 DW3。Expansion/node/DAG ID 绑定 Run 与 StepIdentity；
DAG creation time 使用 Run 的持久化创建时间，避免 retry 改变 canonical publication。
Task prompt 保留声明模板，附加 canonical 输入数据，不执行表达式或模板引擎。
Profile/capability 声明与 member/task/node、input identity 一起冻结到不可变 Expansion
member，不拼接到 prompt 充当绑定。Canonical payload 参与 member、publication、journal
fingerprint。DW3 在同一事务提交 DAG、上述绑定、Run/Step WAITING、budget、journal；
不增加 sidecar 双写或 schema。旧 publication 保持原历史 JSON/digest，不重写 records，
但缺少 execution intent 时不可执行。

Worker 启动前，Task DAG store 读取 exact durable node intent，验证 publication/Run/
Step/DAG/member/journal linkage。Writable Subagent 在分配执行资源前重新核对 exact
intent 与 parent session。复用现有 built-in Profile catalog，只接受 writable-worker role
及 managed-worktree policy；未知 custom ID、不兼容 built-in 均 fail closed，绝不 fallback。
不新增 custom registry。现有 create_binding 接收 exact profile 和未扩大的 Writable grant。
首次 model call 前检查 EffectiveAgentBinding 的 profile identity 与 required capability
子集。Required capabilities 不是 grant；parent、Permission、Sandbox、provider、platform、
runtime、profile ceiling 仍求交。拒绝启动使用已有 FAILED node 语义，不 retry/replan，
不解释为 verification failure。普通非 Workflow DAG 保持默认 writable profile 和 scheduler
行为，不增加自动 Workflow execution 入口。

Map 在发布前冻结有界 typed collection。member key 包含补零原始序号及 item digest，
保留 array 顺序并区分重复值。Node ID 绑定 member/task，dependency 限于同一 member，
排序沿用 DW3 canonicalization。仅发布一个 DAG，最多 8 nodes，继续使用现有 parallelism
限制。空 Map 原子写入 `{count:0,items:[]}` 和 completed step，不创建零节点 DAG。
Generated tasks 仅由 DW3 事务预留/消费，Repeat/replay 不重置 Run budget。

WAITING tick 只查询自己的确定性 Expansion。DAG 非终态或缺少 Projection 时无进展。
COMPLETED DAG 与已有精确 DW4a Projection 允许原子提交 result linkage、step completion、
Run 与 journal。FAILED/CANCELLED DAG 将 Run 停为 FAILED；INDETERMINATE 进入
NEEDS_ATTENTION。不会从 worker 文本自动推导 retry、adoption、repair 或 replan。

## Branch 与 bounded Repeat

Branch 只评估 DW1 `eq/exists`，一次写入 BRANCH decision。`exists` 先解析 schema 中的
required path，缺失或损坏的 durable result fail closed，不当作 false。Restart 读取
既有选择，不重新依据 live state 决策；未选路径不创建 step、Activity 或 DAG。

Repeat 初始化 READY，持久化 RUNNING，记录 iteration 1，逐 tick 完成 body，再检查
post-body `until`。True 完成 Repeat control step；false 且未到上限才记录下一 ITERATION。
最大迭代次数 ≤3；到上限仍 false 以 `repeat_limit` 持久化失败。Body instance 绑定 iteration。
不引入 nested Repeat/Map、mutable VM stack、bytecode、runtime DAG mutation 或第二 scheduler。

## Fake Activity 与 crash consistency

窄 `FakeWorkflowActivity` port 只做同步纯确定性计算。内置 fake ADOPT/VERIFY/REPAIR
明确返回 `status:"fake"`；ADOPT 无 parent change，VERIFY 仅返回 iteration 编号，
REPAIR 明确没有执行 repair。不访问文件、Permission、Tool、Model、Result Adoption 或
Verification。稳定 invocation ID 绑定 Run、step 和 resolved input。Commit 前可以重新
计算纯函数，但 CAS 只保留一个逻辑 output fact；这不声称未来真实副作用 exactly-once。

本地 output/Projection link、completed step、Run generation、journal 同一 SQLite
事务提交，并通过 FK 绑定 step 和 journal generation。异常全部回滚。Commit 后 ACK
丢失时，restart 从持久化 Branch、Iteration、Publication、Projection consumption 或
Activity result 推导下一动作，不重复创建 DAG、逻辑 Activity 或 generated-task 计费。
读取验证 schema、digest、provenance 和 step/journal linkage，拒绝篡改。

## Journal payload 契约

STEP 是 discriminated payload family，不保证每个事件都存在 `change`。Consumer 必须
按 `operation` 分派：DW2 `transition` 含 `change`，DW5a `consume_typed_output` 含 typed
output linkage。未来 consumer 不得无条件读取 `payload["change"]`；本轮不重构 event kind。

## 结束边界与排除项

控制流耗尽进入 WAITING，原因为
`control_flow_exhausted: completion requirements pending`，统一为单一命名常量
`CONTROL_FLOW_EXHAUSTED_REASON`，后续 tick 静止。
它不是 Workflow COMPLETED、verification PASS 或 adoption 授权。本阶段不评估 DW1
completion requirements。Task DAG COMPLETED、worker response、schema-valid output 和
fake status 都不证明业务成功。

不修改 scheduler/Leader 控制，不增加执行引擎，不接真实 parent Activity、filesystem mutation、
Planner/UltraCode、model judge、CLI/TUI/Trace、Plugin/Hook。DW5b 必须单独定义真实
Activity 的执行/恢复与 verification 消费，继续复用现有 adoption engine 和权限边界。
本轮不修复 Issue #165/#167/#169/#171。

## 验证

真实 SQLite 测试覆盖 sequence、input immutable/upgrade、Branch replay、Repeat
1/2/3/limit/restart 与精确 selector、Map 0/1/7/item isolation、同 item dependencies、
Projection consumption、失败终态、owner fencing、并发发布/结果、预算耗尽、事务
回滚、五类 commit-before-ACK 窗口，以及 input/projection/output tamper。
既有 DW1–DW4b、migration、adoption、scheduler、UltraCode 回归仍为门禁。

Execution-intent 回归额外覆盖仅 profile/capability 改动的 identity 区别、restart/replay、
DAG insert 后原子回滚、旧 publication 缺 intent 拒绝、篡改、exact binding 与各 ceiling
收窄后的 required capability 拒绝。真实 composition 测试证明 exact built-in 到达
create_binding，未知 profile/缺少 runtime capability 在首次 model call 前停止。
