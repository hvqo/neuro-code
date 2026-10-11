# ADR 0205：一次真实 Workflow REPAIR 回合

[English](../../en/adr/0205-dw5b4-real-workflow-repair-adapter.md)

- 日期: 2026-10-11
- 状态: 已接受（DW5b-4 内部集成）

## 状态与范围

DW5b-4 接受。REPAIR 只运行一次有界的既有 AgentRuntime 回合，不调度 VERIFY、不重试
不确定执行、不创建修复循环、不判断 Workflow 完成、不发布 DAG、不新增 CLI/TUI 入口。
REPAIR 完成不等于 verification PASS。ADOPT/VERIFY 保持原引擎与权限边界。

## 精确已消费 FAIL

REPAIR 声明的输入必须通过 ResultRef 解析到同一 Run/session 和适用 StepIdentity/iteration
中已经合法消费的真实 VERIFY FAIL。联合验证已消费 output、VERIFY execution、不可变
terminal evidence 与独立 Run settlement journal。字面量 FAIL、PASS、歧义 selector、
缺失证明及输入不一致均关闭执行。claim 前当前有界父工作区 projection 必须等于 FAIL
验证时的 projection，失效证据不得授权修复。

目标、精确配置的验证命令、脱敏失败摘要、工作区身份与批准额度只是有界的**不可信任务
数据**，不能授予权限。Prompt evidence 上限 10,000 字符，目标上限 2,048 字符；response
脱敏后上限 4,096 UTF-8 字节。模型说“修复了”不证明文件变更或验证结论。

## 复用父执行边界

由 composition 拥有的 BoundWorkflowRepairTurn 保留精确父 ConversationBinding，调用
既有 ConversationRunner.run。Provider、Profile、有效 capability、Permission/审批、
Workspace、Sandbox、hooks、上下文管理、取消和进程清理均来自父 binding。
不创建新 binding、隐藏 Shell、第二套 Agent Loop 或内部隐藏会话。

WORKFLOW_REPAIR 是明确的内部 TurnSource/ModelRequestSource，不冒充 USER。
既有父 turn lock 串行执行。任务 prompt 和 assistant 回复只进入临时模型上下文；普通
父会话 durable items 不变。精确 turn recovery/events 与执行事实持久化，通用 turn
retry 不重建此内部输入。本回合禁用自动配置验证；后续显式 Workflow VERIFY 仍是
独立 Activity。

额外可信工具白名单只允许既有工作区读取/发现/编辑与前台 Bash，排除 delegation、
background、extension 入口。作用域内的工具 execution context 移除 background manager，拒绝显式后台
Bash 并禁止前台自动提升；父 binding 本身不修改，只收窄父权限。只读 Profile、权限拒绝、必需 Sandbox
不可用时不得获得写权限。

## 首次执行与仲裁

claim 和 reservation 复用既有 ledger。RUNNING 与唯一不可变 execution identity 同事务
提交，绑定 invocation、Run、Step、session/root、owner/fence、reservation、已消费 FAIL、
精确 turn ID 和 turn input fingerprint。只有成功首次提交才建立 live observation scope。
历史 RUNNING 不能再创建回合或取得计量权限。公共 reconciliation 不接受 usage/receipt。

非阻塞 OS lock 复用本地 SQLite 锁机制。workspace 锁绑定规范数据库路径与父 root，
不同 repair invocation/Store/进程不得并发执行或抢先结算 unknown；invocation 锁保护
恢复。锁从首次执行覆盖至结算，异常、取消、进程死亡后释放，锁文件不删除以维持稳定
inode。不通过 PID 或过期时间猜测执行者死亡，不在模型请求/文件操作期间保持 SQLite
写事务。取消/失去 owner 的 Run 不得授权下次模型或工具进入。已启动任务的清理完成后
才释放执行锁。

## 可信计量

窄作用域 observer 包围实际 Provider adapter attempt，以及权限/hooks/preflight 之后的
既有 ToolExecutor 进入点。retry/failover wrapper 转发到实际 leaf attempt。真实进入后
失败的调用也计数；prepare/权限拒绝不虚构 tool invocation。intent 本身不是进入/返回
证明。普通 runtime 没有 observer，保持原调用形态和行为。无法归属用量的 Provider
托管工具使 tool accounting 保持 unknown。

ACK 绑定连续 ordinal、精确 intent、model/tool 身份、返回/失败和 Provider 报告的
processed input/output tokens。缺少 tokens 时保持 None，绝不估算为 0。受控
perf_counter_ns 计量包括 runtime 与 observation 的 wall time。known usage 仅属于
完整 ACK 的 live 首次执行。Python scope identity 只提供可信 composition 隔离，
**不是抵御任意 Python 代码的操作系统安全边界**。模型/MCP/Skills 参数不能提交这些
观察事实或替换 composition 依赖。

默认最多 4 model calls、8 tool calls、32,000 input tokens、8,000 output tokens、
60,000ms。reservation 和 runtime budget 与父上限及 Workflow 剩余额度取更严限制。
dispatch 检查已有 known usage 与上限；Provider 不可拆分的响应仍可能超过 token
reservation，保留实际 overrun 并要求 attention，不伪造精度。generated_tasks=0，
任务发布计费仍归 DW3。

## 工作区与终态事实

既有有界 checkpoint projection 提供 repository/root/HEAD/index/content fingerprints，
最多接受 256 个 path entries。前后 projection 和最多 64 条 changed paths 摘要及截断
标志区分确定变更、无变更、不确定。模型等待期间工作区不得改变；工具可以修改，下一次
验证的 snapshot 成为其自有边界。第三方变更无法通过这些 optimistic 检查获得全局原子
排除保证。

正常 Runtime completed、完整 known accounting、稳定权限与工作区证据才产生 Activity
COMPLETED，以及 DW1 `{status: "COMPLETED", response: string}`。权限/preflight/runtime
阻塞映射 BLOCKED，已知执行失败可为 FAILED。缺失 ACK/tokens、取消、预算超额、失去
owner、不确定工作区映射 INDETERMINATE/NEEDS_ATTENTION，不重新打开 terminal Run。
不成功回合不发布可消费的成功 output。

Repair evidence、不可变 Activity result、既有预算结算、local Activity event 和独立
Run journal anchor 同事务提交。读取与独立 journal 联合验证 fingerprint/source/turn。
仅重写本地证据组并重算 fingerprint 不能替换历史锚点；不承诺抵御重写整个数据库和
历史 journal 的恶意管理员。

## 崩溃与 replay

遗留 CLAIMED 的模型/工具/token usage 为 0，wall time unknown。历史 RUNNING 不再
执行，即使文件已达期望状态或 turn terminal 已存在，计量不完整仍 unknown，要求
显式 attention。完整 terminal replay 不运行模型/工具、不写文件、不新 claim、不重复
结算，不要求旧 live 工作区/HEAD 仍存在。Interpreter 在之后独立 tick 消费。

SQLite、Provider、文件系统不是一个事务。协议防止自动重复 dispatch 并报告不确定
窗口，**不承诺外部 exactly-once**。有界 projection 不是全局文件系统快照；另一个
数据库、外部编辑器、独立组合的普通 runtime 不受本地 repair 锁覆盖。既有父 runner
串行与 optimistic 前后检查构成支持边界。

## 持久化与验证

schema 42→43 仅增加 workflow_repair_executions、workflow_repair_events、
workflow_repair_evidence，具有有界 JSON、FK/UNIQUE/CHECK 和 immutable triggers。
不创建第二套 ledger。事务 migration 保留旧数据，重复 initialize 幂等。新 repair
publication 带 protocol discriminator；历史无版本 Activity 事实仍可读，新 REPAIR
不能用 generic finish 伪造真实终态。

测试区分真实 SQLite 事务/reopen、临时 Git/文件变更、离线 scripted Provider 经过真实
AgentRuntime、真实 pytest VERIFY、注入 VERIFY exit/fault，以及真实独立进程终止。
不宣称商业模型或跨文件系统 exactly-once 验收。普通 Runtime/Provider、Activity/
Interpreter、ADOPT/VERIFY、migration、Permission/Sandbox、Swarm/UltraCode 回归仍需
通过。scripts/check_all.py 是完成门禁唯一清单。
