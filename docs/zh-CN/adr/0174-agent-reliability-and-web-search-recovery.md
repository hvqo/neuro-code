# ADR 0174：Agent 可靠性、外部证据与网页搜索恢复

**简体中文** · [English](../../en/adr/0174-agent-reliability-and-web-search-recovery.md)

- 状态：已接受
- 日期：2026-09-24
- 范围：Runtime 网页搜索能力解析、外部证据指引、重复执行模式的有界恢复

## 背景

Agent 可能正确发现请求的项目不在工作区中，却因为 MAIN 服务商没有原生搜索工具而无法继续查找公开证据。
仅有 Web Search 配置偏好，并不能证明当前 binding 拥有可执行搜索 route 或模型可见的 `web_search`
定义。另一方面，周期性探索循环可能在首次检测时就进入 `STUCK`，不给模型一次有界的策略调整机会。

Runtime 必须只暴露由可信模型/服务商事实和具体后端支持的能力，保留工作区优先策略、普通工具批处理与安全边界，
并继续提供有限的循环保护。

## 决策

### 有效 Web Search 解析

`WebSearchMode.AUTO` 会在 Provider capability、客户端工具和 binding 的 `allowed_tool_names` 已确定后解析：

1. 只有解析后的 Provider 明确支持 MAIN 内置托管搜索，且能与将暴露的客户端工具共同使用时，才使用 MAIN inline。
2. 否则使用显式配置且可执行的 `WEB_SEARCH` route。
3. 没有显式 route 时，按稳定的 `(name.casefold(), name)` 顺序检查已配置的 Provider profile，选择第一个
   具体托管搜索后端可解析且凭据已配置可用的 profile。MAIN 及 MAIN 故障转移 profile 不参与独立 sidecar 自动发现。
4. 不会把 `UNKNOWN` 模型或后端 capability 提升为 `SUPPORTED`。如果没有被允许且可执行的路径，则不注册工具，
   并在有效 binding 上暴露类型化的不可用原因。

只有在 sidecar 路径可执行且当前 binding 允许 `web_search` 时，组合根才会构建本地 `web_search` 工具。
原生 Provider 工具定义仍由可信 adapter 管理。类型化的 `RuntimeWebCapabilityInspection` 报告有效可用性、路径、
不可用原因、安全的 Provider/模型标签和有效 Web Fetch 路径；不携带凭据或 endpoint。TUI `/status` 使用同一份
组合完成后的 inspection，因此展示当前 Runtime 的实际状态，而非持久化偏好。

### 工作区优先的外部证据

稳定 Runtime 指引要求先检查工作区证据。如果工作区无法提供公开或外部声明所需的证据，且模型可见
`web_search` 可用，Agent 应使用该工具；否则必须说明限制，区分已验证的本地事实与未验证的外部声明，且不能把外部
部分表述为已验证。Shell 仍按既有权限和沙箱规则可用；本决策不会禁止用户明确要求的 Shell 网络操作，也不会允许
Shell 抓取静默替代公开资料搜索。

该指引属于稳定上下文，不会逐回合重写。Replan 指引只补充简短要求：已确认目标不在本地后停止重复扫描；搜索可用时
升级到 Web Search，否则说明能力阻塞。

### 检测、恢复与终态策略

首次检测到重复操作/观察、重复错误、周期循环或无进展时，检测器本身不终止回合。首次检测会创建本回合内存中的有界
状态并返回 `REPLAN`。状态仅包含类型化原因、规范行为指纹的摘要、可选周期长度和当前工具调用计数边界；不保存原始
参数或结果，也不持久化。

Agent 获得一次有界的策略执行机会。新证据、工作区变化、计划变化、验证进展或外部状态进展会清除对应恢复压力。
如果经过最少数量的有界策略调用后异常仍持续且没有实质进展，Supervisor 才返回类型化终态 `STUCK`。既有模型/工具
预算仍是外层执行上限。回合结束后，恢复状态和所有摘要都会消失。

`SupervisorDecision` 和终态完成诊断携带有界的 `reason_code`、`replan_attempted`、`replan_count`、可选
`cycle_period` 及 `progress_since_replan`。TUI 会将已知终态原因映射为简洁本地化文案。诊断和面向用户的能力状态
均不包含参数、结果正文、凭据、可能含密钥的 endpoint、URL 或内部指纹。

## 后果

- DeepSeek 或其他 OpenAI-compatible MAIN 可以通过现有 canonical Web Search service 使用单独配置且可信的 Hosted
  Search 后端。
- `AUTO` 具有确定性并遵守 capability；缺失或被过滤的 route 不会在当前 Runtime 中无解释地显示为已启用。
- 普通本地探索可以转向外部证据，而不会因这次正常策略切换被视作循环；重复且无进展的行为仍会有界地进入终态。
- 规范会话历史、权限、沙箱策略、Provider affinity、批处理顺序及现有 Web Fetch 安全策略保持不变。
- 本决策不增加浏览器自动化、通用 Shell 网络禁令、完整 Runtime Trace 或 Provider 专属缓存控制。
