# ADR 0182：Agent Profile 与能力模型 V1

**简体中文** · [English](../../en/adr/0182-agent-profile-capability-model-v1.md)

- 状态：已接受
- 日期：2026-09-28
- 范围：声明式 Agent 角色、能力解析与逐 binding 运行时投影

## 背景

Neuro Code 已有 Provider capability 事实、具体 Tool Registry、子代理
capability manifest，以及权威的 Permission、Workspace 和 Sandbox 检查。缺失的
部分是一个 binding 阶段投影，用来明确表达角色意图及当前真正可执行的工具和上下文。
没有该投影时，schema 暴露、工具 dispatch、Provider 原生工具和 Trace 诊断可能各自
做出不同判断。

## 决策

应用层使用以下链路：

```text
AgentProfile → Capability Resolution → Runtime Binding → Effective Agent
```

`AgentProfile` 是不可变的声明式意图，包含身份与角色、行为和静态指引、与
Provider 无关的模型/推理策略、类型化能力请求、记忆/上下文策略、执行预算上限、
子代理/工作区写入策略和验证意图。它不包含 Provider endpoint、凭据、Session 状态或
可变执行状态。

Capability Resolution 将请求能力与具体 Runtime 可用性、显式 Provider 和平台事实、
Profile override、当前权限和沙箱策略，以及精确的父级能力上限求交：

```text
effective = requested ∩ runtime ∩ provider ∩ platform ∩ override
            ∩ permission ∩ sandbox ∩ parent
```

每项不可用的请求能力保留一个类型化原因。未知的 Provider function tool 或 hosted
tool 支持不视为可用。只有工具所需的全部类型化能力均有效时，才会绑定该工具。
Provider 原生工具 schema 在 composition 阶段按 Profile 意图和显式工具级拒绝规则过滤；
具体 Provider capability 事实进一步决定可用性。路径范围权限仍在每次调用时执行。

`EffectiveAgentBinding` 在 composition 边界创建一次，记录选定的 profile/provider/model、
推理策略、不可变能力解析、精确本地与 Provider 工具名称、收窄后的预算、安全约束标签和
确定性 fingerprint。Runtime 对模型可见定义和工具 dispatch 使用同一份受限本地工具目录。
动态 MCP 目录更新仍是显式扩展更新，且只有具备 `extension.invoke` 的 binding 才会接受。

内建 Profile 包括 Main、Explorer、Planner、Reviewer、Writable Worker 和 Leader。现有
子代理工厂显式选择 Explorer 或 Writable Worker；内部 Planner 与 Leader binding 选择各自
profile。未标注角色的既有 binding 为兼容性继续使用 Main。Profile 策略只能收窄能力和预算。
Main 不追加静态指引，因此系统前缀保持不变。其他静态 profile 指引只在创建 binding 时加入；
profile 变更发生在 binding/cache 边界，不发生在活动请求序列中。

Profile 不授予权限。PermissionManager、规范 Workspace 目标、审批、Sandbox 执行、验证、
受管 worktree lease 及既有 parent/child grant 仍是权威。Writable Worker 需要当前 parent 写入
上限、可写 sandbox 和既有 managed-workspace relay；每次真实修改仍经过现有 tool pipeline。
Project Memory 写入仍由应用层拥有。`read_project_memory` 保持稳定 schema；当前 binding
没有项目 scope/capability 时，dispatch 会失败关闭。

Trace 只记录有界的 profile 身份、有效/不可用能力标签、预算、安全标签和 binding fingerprint，
不记录指引、prompt、工具参数、凭据或记忆正文。本轮不增加数据库 schema、设置编辑器、第二套
权限系统或第二个 Runtime。

## 后果

角色 wiring、Provider/工具可用性与 Runtime 能力投影现在共享一个可测试的 binding 契约。
后续 Dynamic Workflow 可以请求 profile 和更严格的 override，并复用同一个 resolver 与
Runtime 投影；它仍必须把 profile 视为意图，并保留现有安全权威。
