# ADR 0175：有效设置与按能力筛选的 Web 体验 V2

**简体中文** · [English](../../en/adr/0175-effective-settings-and-web-capability-ux-v2.md)

- 状态：已接受
- 日期：2026-09-24
- 范围：TUI 设置导航、偏好来源、运行时重载与 Web Search profile 选择

## 背景

Agent 偏好已经覆盖输入、执行、请求、上下文、Web、开发、安全和后台任务。设置表单曾把继承状态和原始存储值放在过于显眼的位置；Web Search 模式也不能选择可信且可执行的 profile。运行时设置保存仍被描述为下次启动生效，尽管应用已有受控的 composition reload seam。

界面应展示当前有效值及其来源，同时继续由既有组件负责供应商能力、权限、沙箱和 Runtime 组合决策。

## 决策

### 导航与来源

设置分为外观、模型与连接、Agent、网页能力、开发、权限与安全、高级。普通表单显示有效值及一种来源标签：默认、用户、项目或 CLI。显式 CLI 值会展示出来，不会误标为已保存偏好。高级总览仍不显示验证命令正文。

用户范围适用于所有项目。项目范围沿用现有的工作区偏好，保存在用户 state 目录中。它不使用 SessionProject identity，也不会把偏好写入代码仓库。可选字段在现有 version-1 JSON 格式中增量保存；无需数据库或偏好 schema 迁移。

### Web Search 选项

普通表单提供关闭、自动、自定义。自动模式交给既有的能力感知 Runtime resolver。自定义模式同时保存模式和精确的供应商 profile。可选 profile 来自活动 Runtime 的无凭据能力投影，只包含凭据可用且可信 Search 后端可执行的配置；不会根据供应商名称猜测兼容性。

没有可执行 profile 时，设置页显示类型化不可用原因；供应商设置存储可用时，还会提供“管理供应商”入口。保存自定义选择前，会与当前候选列表再次校验。运行时仍通过既有 resolver 解析路由，并由既有 registry 注册模型可见的 web_search 工具。权限拒绝或路由不可用时继续 fail closed。Web Fetch 普通表单保持关闭/自动，高级页保留原始路由控制。

状态投影显示实际路径、受限的 profile/model 标签和类型化原因，不显示端点、凭据或供应商错误。工具 schema 及顺序不变。

### 保存与运行时生命周期

影响 Runtime 的偏好变更使用现有受控 TUI reload exit code。退出前，TUI 捕获当前 Session ID。Bootstrap 关闭旧 composition，重新读取配置和偏好，并通过既有 resume 流程传入该 ID。不会重放当前轮次。运行中的轮次会在持久化前阻止运行时设置保存。纯界面偏好实时更新，不重建 Runtime。

### 边界

偏好通过 ApplicationSettings 和 bootstrap composition root 进入 Runtime。供应商路由、能力校验、凭据、工具 allowlist、权限策略、工作区访问和沙箱策略仍具权威。设置页只接收安全的能力投影，不接收包含秘密的 route adapter，也不能任意访问 state 文件。

## 影响

- 用户可以在编辑前看到有效值及其来源。
- 自定义 Web Search 选择当前已配置且可执行的路由，运行时重载后保留活动 Session。
- 缺少凭据、后端能力或工具权限时，自动与自定义模式都会 fail closed。
- 现有 JSON 设置继续可读，非交互 CLI/ACP 行为不变。
- 供应商专属缓存参数、新的供应商管理流程和额外 Web Fetch 模式不在本决策范围。

## 验证

回归测试覆盖偏好层优先级和来源、项目模式覆盖会清除继承 Search profile、有效值表单、自定义 profile 保存与 reload 通知、无供应商时的明确提示、Session ID 传递，以及自定义偏好经 Runtime capability inspection 到达 web_search tool registry 的路径。
