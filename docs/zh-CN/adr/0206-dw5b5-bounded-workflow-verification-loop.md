# ADR 0206：有界 Workflow VERIFY / REPAIR 闭环

[English](../../en/adr/0206-dw5b5-bounded-workflow-verification-loop.md)

- 日期：2026-10-12
- 状态：已接受（DW5b-5 内部能力）
- Schema：保持 43

## 决策与范围

DW5b-5 将现有 durable Interpreter、Activity store、VERIFY adapter 和 REPAIR
adapter 组合为一个显式调用的有界推进能力。`WorkflowLoopCoordinator.drive_once()`
不是第二个 Interpreter、Agent loop、scheduler、worker 或后台服务。每个控制决策都由
`DurableWorkflowInterpreter.advance_once()` 根据最新持久化 Run、Step、Activity、output、
journal 与 budget facts 作出。Coordinator 只调用精确持久化 invocation 对应的现有
VERIFY/REPAIR adapter，不自行执行 Bash 或修改文件。

这只是内部 composition 能力，不增加 CLI/TUI 入口、自动 Ultracode 路由、通用 Planner、
completion judge 或产品工作流界面。验证闭环通过不会将 Workflow Run 置为 COMPLETED。

## 有类型的控制流结构

允许的闭环使用 DW1 现有 `Activity` 和 `Branch` 节点静态展开：

```text
VERIFY_1 -> Branch(status == PASS)
              PASS：结束
              FAIL：REPAIR_1 -> 新 VERIFY_2 -> Branch(status == PASS)
                       PASS：结束
                       FAIL：REPAIR_2 -> 新 VERIFY_3 -> Branch(status == PASS)
                                PASS：结束
                                FAIL：结束
```

最后一次 FAIL 分支为空，因此最后一次验证失败后不会再执行没有后续验证机会的
REPAIR。loop-plan recognizer 最多接受 3 个不同 VERIFY step ID 和 2 个不同 REPAIR
step ID，检查每条精确的 status `ResultRef` 边，并拒绝额外的 VERIFY/REPAIR 声明或
不支持的结构。次数在编译后的定义中冻结，模型文本、任意 JSON、Python 表达式或运行时
计数器都不能增加上限。

本方案刻意不使用 DW1 Repeat。Repeat 是后置测试：先执行 body，再计算 `until` 条件；
若不改变其语义，它无法表达“最后一次验证仍失败时不再修复”。静态 Branch 链无需修改
IR、validator、compiler 或 schema。

## 因果证据与新鲜度

每次 REPAIR 输入都解析到同一 Run 和父 session 中已合法消费的精确 VERIFY FAIL，绑定完整
`StepIdentity`、result fingerprint、独立 VERIFY journal proof 和当前有界父工作区证据。
现有 REPAIR adapter 继续负责首次执行权限、owner/fence、permission、父 Runtime、工作区、
文件修改、证据与可信用量。REPAIR 完成或文件发生变化都不能推导为 PASS。

后续 VERIFY 使用新的 step 和 invocation identity，执行一次新的真实、已批准命令。其有类型
输入绑定精确的前序 durable REPAIR output 作为来源。VERIFY source resolution 现在仅在验证
该 REPAIR 的精确 terminal proof 后接受已完成 REPAIR；这不会把 REPAIR 变成验证结果。普通
VERIFY adapter 会重新采集工作区 projection、运行已批准命令，并在 Interpreter 消费结果时
再次要求新鲜证据。过期 PASS 不会被消费。这些有界 projection 是乐观证据，不是全局原子
文件系统快照。

## 推进、预算与恢复

一次显式 `drive_once()` 最多执行 96 个 durable Interpreter action。每一步之前重新读取 Run
和不可变定义，并校验调用方当前 owner/fence。它把 Activity 执行和恢复委托给现有 adapter，
再让 `advance_once()` 根据 durable facts 消费结果或发布下一步。busy、外部 DAG 工作、权限
阻塞、取消、owner 替换、unknown accounting、工作区过期或其他未解决事实都会返回有类型的
停止结果。没有 sleep、重复轮询，也不会自动重放历史 RUNNING。

静态次数限制为 3 次 VERIFY、2 次 REPAIR。现有逐 Activity reservation 与唯一 DW2 Run
ledger 继续限制 model calls、tool calls、tokens 和 wall time。用量只来自 ADOPT、VERIFY、
REPAIR 已有的可信计量。unknown 不等于零，不能通过进入下一轮清除。预算耗尽或副作用不确定
时安全停止。

application-level 结果为 `VERIFIED`、`EXHAUSTED`、`BLOCKED`、`FAILED`、
`INDETERMINATE`、`WAITING` 和 `CANCELLED`。`VERIFIED` 表示最近所需的真实 VERIFY PASS
已通过有效新鲜度证据完成消费，不代表 Workflow 完成。控制流结束后仍保留既有
`WAITING: completion requirements pending` 状态。Completion requirements、通用 retry 与
产品路由属于后续工作。

## 验证边界

定向验收使用真实 SQLite 持久化、临时 Git 父工作区、经现有 ToolExecutor 运行的真实前台
pytest，以及带离线 scripted Provider 的现有 AgentRuntime 执行 REPAIR。Provider response
是测试 fixture，不构成真实商业 Provider 或正确率证明。DW5b-2/3/4 已有的恢复、权限、沙箱、
计量与执行仲裁仍由原 adapter 和 store 负责。
