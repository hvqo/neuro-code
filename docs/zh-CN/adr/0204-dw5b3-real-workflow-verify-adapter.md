# ADR 0204：真实 Workflow VERIFY Adapter

**简体中文** · [English](../../en/adr/0204-dw5b3-real-workflow-verify-adapter.md)

- 日期：2026-10-09
- 状态：已接受，用于内部 DW5b-3 集成
- 范围：一条显式验证命令；不实现 REPAIR 或 completion judge

## 决策与权威

`VerificationTracker` 观察 Runtime 事实，并非执行器或内容版本权威。Bootstrap 从用户显式配置的 `verification_command` 构造 `ApprovedWorkflowVerification`。既有命令分类器仅接受 pytest 与静态检查族。Workflow 输入、worker response、README 和模型 JSON 不能提供命令或权限授予。

`WorkflowVerifyActivityAdapter` 使用真实 parent binding 的 `ToolExecutor`，复用当前工具集合、Capability、Permission、Workspace、Sandbox、hooks 与 undo 边界。Bash 负责前台进程、超时、有界脱敏输出与进程树取消；本次调用禁用后台自动提升。没有第二个 Shell、Agent Loop、scheduler 或自动发现测试。工具进入观察发生在权限／工作区 preflight 完成后、进入既有工具 port 之前；权限拒绝计零次调用。测试与静态检查可能写缓存和文件，命令分类不等于只读保证，也不绕过副作用策略。

## Durable identity 与 schema 42

Schema 41 只有 ADOPT 专用 mutation 计量，不能表达验证命令、退出事实和工作区版本。最小原子 41→42 migration 新增两张 insert-only 表：`workflow_verification_executions`（每 invocation 一份执行身份）、`workflow_verification_evidence`（每 execution 一份终态证据）。FK、唯一约束和不可变 trigger 保留既有 Activity／ledger；不会给旧 attempt 伪造 known usage。

Execution 绑定 immutable invocation/request、Run/session/Step、owner/fence/reservation、精确可信配置、parent root 与有界工作区证据。已消费 ResultRef 输入绑定既有 typed output fingerprint 和 journal，包括适用的 ADOPT／projection 事实。只将业务输入与冻结来源比较，不接受 latest DAG 或 caller ID 作为权威。终态证据绑定 execution fingerprint、终态、exit code、有界脱敏 summary／digest、output 和 usage；Activity result fingerprint 及独立 local event 冻结证明。只重算命令、exit code、workspace 或 evidence row 的指纹不能替换原结果。

## 首次执行与恢复

Claim 在既有 run ledger 原子预留预算。首次 owner CAS 将 RUNNING 与 execution identity 一起提交，之后才调用工具。非阻塞 OS lock 按 resolved 本地 SQLite 路径＋invocation 建立，从 preflight 持有到终态结算。不同 Store／进程共享互斥边界（POSIX flock、Windows 单字节锁）；异常、取消和进程死亡关闭 descriptor，绝不 unlink lock file。busy recovery 不写任何状态；真实命令执行期间不持有 SQLite 写事务。

历史 CLAIMED／RUNNING 不重新授予 dispatch。缺少终态证据则保守进入 INDETERMINATE：RUNNING tool/wall usage 保持 unknown；遗弃 CLAIMED 的执行用量为零，准备 wall time 未知。不以超时／PID 或文件现状推断调用次数。进程内 task-local 首次执行 scope 限制 known settlement；这是可信 composition 边界，不是对任意 Python 或恶意数据库管理员的 OS 隔离。命令／workspace port 不暴露为模型、MCP、Skill 参数。

Evidence、immutable Activity result、budget consumption、Run 安全状态和 journal 在一个 SQLite transaction 中提交。Rollback 保留 RUNNING，不产生半份证明；提交后 outer ACK 丢失则返回原终态事实，不重复执行。取消等待既有 Bash 清理进程后释放仲裁。unknown／overrun 进入 NEEDS_ATTENTION；late accounting 不重新打开终止 Workflow。

## 工作区证据与新鲜度

复用有界 checkpoint projection：repository/root/HEAD、Git index、tracked 内容及 non-ignored untracked 内容。HEAD 和观察 generation 都不是内容身份。超限／不支持的 projection 不产生 PASS；ignored cache 不在 source projection 内；不做无限仓库哈希。

执行前捕获 projection 和有界 source-path metadata watch。命令运行中每 50ms 观察这些路径，不重复哈希文件；inode/mode/size/mtime/ctime 变化会使证据失效，即使后来恢复原字节。结束时重新捕获完整有界 projection，Interpreter 消费前再次捕获。变化的 source revision 在提交 Workflow output 前拒绝 stale PASS／FAIL；绕过 fresh-consumption scope 的真实 VERIFY output 写入 fail closed。

这是有界、乐观的文件系统 freshness，不是 filesystem 与 SQLite 原子隔离。两次观察之间发生并完全恢复的外部变更，特别是新建又删除的 untracked 文件，无法普遍检测；ignored 文件与外部资源不属于验证范围。不得将局部证据宣称为范围外资源的全局 PASS。验证／消费时 source writer 应保持静止；没有分布式文件系统锁。

`workspace_generation` 是执行开始时捕获的 durable Run generation，仅用于顺序／关联；内容身份由独立 projection fingerprint 表达。

## 输出语义

- 真实 exit 0＋完整且未变的 source evidence：Activity COMPLETED，`status=PASS`。
- 可信 pytest／static-check exit 1＋完整且未变证据：Activity COMPLETED，`status=FAIL`，供后续 Branch／Repeat 消费。
- 缺少可信配置、不支持 source、缺少显式要求的沙箱启动器或工具进入前权限拒绝：BLOCKED，不生成 PASS／FAIL。
- 无可靠 exit code、signal、timeout、cancel、工作区变化或 crash：INDETERMINATE／NEEDS_ATTENTION。
- 其他非零 exit（如收集／配置／执行错误）：FAILED，不伪造测试 FAIL。

DW1 output 保持 `{status: string, workspace_generation: integer}`，真实成功执行仅使用 PASS／FAIL。计入真实前台 tool invocation 与受控 `perf_counter_ns` wall time；generated tasks、model calls、tokens 为零。预留一调用及配置 deadline＋有界准备 allowance；超额保存真实值，不截断。恢复不接受 caller amounts／receipt。VERIFY completion 不宣告 Workflow COMPLETED，不执行全局 completion requirements。

## 验证与限制

Focused tests 区分临时 Git 仓库中的真实 Bash／pytest／ruff 与用于 crash／ACK／overrun 的注入边界。覆盖 SQLite rollback／reopen、独立进程仲裁／死亡、stale PASS、dirty content、已消费 ADOPT→VERIFY、FAIL routing、Permission／Sandbox 拒绝、migration 与 legacy regression。进程树行为复用既有平台 suite。不能宣称 Shell 副作用 exactly-once；不确定执行永不自动重跑。

Result Adoption core、Task DAG scheduler、正常 verification tracking 和 Swarm／UltraCode 不变。Interpreter 只发布／消费 Activity，不执行命令。REPAIR、VERIFY→REPAIR loop、completion policy、Planner 和 UI 接入留待后续。
