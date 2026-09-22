# ADR 0172：项目记忆 V1

**简体中文** · [English](../../en/adr/0172-project-memory-v1.md)

- 状态：已接受
- 日期：2026-09-23
- 范围：由可选 `SessionProject` 拥有的本地、有界、跨会话记忆

## 背景

ADR 0165 将项目引入为可选的会话分组。持久项目决策和针对项目的用户反馈仍需在每个会话中重新发现。记忆必须跟随稳定的项目身份，保留不属于项目的会话，并且始终从属于当前工作区、Git 状态和仓库指令。

## 决策

**`SessionProject.id` 是唯一的记忆所有者身份。** `cwd` 仍是工作区绑定，不会隐式创建或选择项目。每个项目的文件保存在 Neuro Code 配置的 state root 下：`project-memory/<project-id>/`；即使工作目录相同，不同项目也不共享记忆，项目重命名不会改变其身份。`project_id` 为空的会话没有 Project Memory scope。

**记忆是精简、可检查的应用能力。** 领域记录稳定的 `memory_id`、名称、描述、`project | feedback | user | reference` 类型、正文、来源会话及创建/更新时间。`user` 只表示与当前项目相关的背景。应用端口由有界文件适配器实现，包含 manifest、生成的 `MEMORY.md` index，以及每条记忆独立的正文文件。UUID 项目身份、记忆文件名模式、禁止跟随链接的读取、目录包含性检查，以及严格的文件/名称/数量/字节限制会拒绝 traversal、链接、错误 manifest 和超限内容。自动记忆不会写入用户代码仓库。

**请求上下文只包含 index，不包含全部正文。** 项目绑定的 Main Agent 会在仓库指令和 Skills 之后、会话历史之前注入有界 index。该消息有专用的 synthetic reason，不会成为持久会话历史。Index 和 `read_project_memory` 工具都将记忆标注为上下文证据，而非指令；当前仓库、Git 状态和 `AGENTS.md` 始终优先。该工具只接受一个精确记忆身份，并通过受当前 binding scope 限制的应用只读服务读取。它不能读取 state root 中的任意路径。Subagent binding 默认不获得此 scope。

**提取异步执行、有界且由应用生命周期拥有。** 项目绑定会话中的持久用户 Turn 成功完成后，组合根拥有的 manager 会安排提取；每个项目同一时间至多运行一个 Provider 请求。提取仅读取该会话 extraction cursor 之后的持久对话后缀，通过现有 manifest 更新匹配记忆，禁用工具，并受来源、prompt、输出、事件、记忆数量、队列及 20 秒限制。每条新记忆都会记录来源会话。调用 Provider 和持久化前会脱敏配置中的凭据及可识别凭据。明确的中英文 remember/forget 请求通过有界确定性操作处理；存在歧义的删除不会执行。其他候选必须来自严格有界的 JSON 响应；project/feedback 描述保留 Why 与 How to apply。失败和取消会生成带类型、可观测的结果，不会撤销或使已提交的用户 Turn 失败。关闭时会取消并等待 manager 拥有的工作任务；未处理的持久 Turn 会在重启后继续符合提取条件，因为它的 cursor 尚未前进。

**Session 生命周期继续权威管理 scope 变化。** Resume 从 Session 行恢复 `project_id`。项目视图可以显式指定项目 ID 并新建会话；首次 Turn 持久化 Session 行之前就会绑定该 ID。Fork 项目会话时会随持久会话副本继承 Project owner，但 Subagent binding 仍不会获得记忆 scope。移动当前打开的会话会与 Turn 串行，并为后续请求切换 Context 与 Recall scope。删除项目会与提取串行，先清除该 ID 下的文件，再解除其会话归属；若发现不安全的文件，就 fail closed 并保留数据库项目。若之后的数据库删除失败，项目仍存在但记忆为空，可由后续成功 Turn 重新生成。现有 v35 Session schema 保持不变。

## 后果

- 未归属项目的既有会话、Session resume、Context compaction、Fresh Context Rollover、Provider affinity、权限规则、工作区适配器和沙箱行为继续保持权威。
- Project Memory 不保存代码结构、路径、Git 历史、`AGENTS.md` 规则、计划、临时进度或普通调试过程。
- 全局用户记忆、向量检索、图记忆、云同步和完整记忆管理界面均不在本范围内。TUI 仅增加明确的“在项目中新建会话”操作和项目删除提示。
- 底层可选项目/会话关系详见 [ADR 0165](0165-session-projects-and-library-management.md)。
