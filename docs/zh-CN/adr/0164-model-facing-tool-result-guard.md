# ADR 0164：面向模型的工具结果保护边界

[English](../../en/adr/0164-model-facing-tool-result-guard.md) · **简体中文**

- 状态：已接受
- 日期：2026-09-16
- 范围：CM4a 普通 Agent 的有界工具结果上下文投影

## 背景
现有各工具已经提供有用的输出上限，部分工具还会把更大的脱敏结果
持久化到既有的会话作用域输出 artifact store。这些上限并不能覆盖扩展工具
或所有终态路径。此前 Runtime 会直接把 `ToolResult.content` 放入
`Role.TOOL` 消息，因此一个意外的大结果可能主导下一次模型请求，尽管 Runtime
仍需要其完整值用于验证、监督、进展、工作区证据、事件和 artifact 元数据。

因此，面向模型的上下文需要一个确定性的投影边界，同时不能创建第二个执行事实
所有者或第二个 artifact store。

## 决策
`ToolResult` 继续是规范执行结果。`ToolResultContextProjection` 是类型化、
Provider 无关的值，描述可以放入面向模型工具消息的有界内容和计量值。应用 Runtime
在 `application/runtime/tool_result_guard.py` 中拥有纯函数
`project_tool_result()` 的策略。

该策略使用既有 `ToolContext.output_byte_limit` 作为配置的本地上限，并应用
64 KiB 与 16,384 个近似 token 的保守 Provider 无关全局上限。token 值使用既有
`estimate_text_tokens()` 估算器；它明确不是 Provider 原生 tokenizer 的结果。
完整请求仍通过 `ContextPreflight`，使用配置的 `ProviderContextWindow`、输出保留值、
安全余量以及 `estimate_model_request_tokens()` 计量。Guard 不是第二套 Provider
预算，也不是独立的 rollover 阈值。

同时满足两个上限的结果按字节原样透传。超大文本使用确定性且 UTF-8 安全的头尾投影，
并加入说明省略量、是否存在既有完整 artifact、以及应优先使用定向、过滤或范围请求
的 marker。错误结果会明确标识为错误输出，并保留尾部，因为退出状态和诊断通常位于
尾部。无法容纳任何字节的零上限会产生空的有界投影，同时在诊断中保留尺寸事实。

投影只在 `ToolExecutor` 构造 `Message(Role.TOOL, ...)` 时应用，包括成功、错误、
权限、取消/跳过、并行和 control rejection 配对。相同的有界元数据会加入既有终态
工具事件。完整规范 `ToolResult` 仍会先传给 hooks、observation、验证、
工作区/进展处理、plan 处理、`ToolExecutionResult` 以及既有终态事件 payload。
原始输出不会复制到投影元数据。

对于并行工具批次，完整且有序的调用集合已知后，由 `AgentLoop` 拥有最终的聚合边界。
它首先对一个基础请求执行 preflight，该请求包含当前上下文、Provider 保留项、assistant
工具调用消息和空结果占位符。已知容量时，基础 preflight 之后的剩余估算容量成为保守的
聚合 token 配额；否则使用 Provider 无关的 token 上限。聚合字节配额取全局字节上限与
`ToolContext.output_byte_limit * call_count` 中较小者。两个配额都按调用确定性分配，
余数分给较早的调用。这样每个结果都有份额，调用身份和提交顺序保持不变，下一次
preflight 看到的就是完整的聚合投影。这些 token 保留仍是近似值；`ContextPreflight`
继续是权威。

只有当 Runtime 自有的 artifact store 边界在本次 executor 生命周期内返回了规范的
`ToolOutputArtifact`，且结果 metadata 与这个类型化句柄完全匹配时，才会发出更完整
artifact 可用的 marker。不会信任单独存在的通用 `output_artifact_*` metadata，因此
custom/MCP 工具不能仅通过选择这些 key 伪造该声明。CM4a 不创建新 artifact，也不改变
既有 artifact store、脱敏、读取、权限、会话关联或垃圾回收契约。Artifact 是外部重新读取
界面，不是模型上下文。

## 不变量与非目标
- 规范 `ToolResult` 不等于面向模型的投影。有界 `Role.TOOL` 消息不能替代验证、监督、
  最终化或 UI/ACP 终态投影使用的完整规范证据。
- 下一次请求仍然从投影后的 `Role.TOOL` 消息正常构建并执行 preflight，不绕过或特殊化
  preflight 计量。
- 并行结果在 `AgentLoop` 批次边界按调用确定性分配份额；大结果不能消耗整个批次配额，
  也不会不可预测地挤掉后续结果。
- 工具调用 ID、名称、并行合并顺序、错误状态、权限和取消配对保持不变。
- 既有 Runtime 边界先执行显式脱敏，再交给规范下游消费者和模型投影；Guard 不会弱化
  脱敏，也不会暴露 artifact 路径。
- 既有 Bash、filesystem、search、web、background、terminal、TUI、CLI 和 ACP artifact
  行为仍由原有 adapter/application 边界拥有。扩展工具同样受到确定性 Guard 约束，但只有
  通过 Runtime store 边界返回的类型化 artifact 才能启用省略 marker。
- 不包含 LLM 摘要、语义检索、自动清除旧结果、新 artifact 数据库、跨会话 artifact
  检索、结果重写、CM3 变更、UltraCode 重设计或子代理策略扩展。

## 兼容性与验证
本变更不增加持久化 schema，也不增加新的 durable result owner。既有 artifact 元数据和
终态事件消费者保持兼容，因为规范 `content`、`is_error`、metadata 和
`execution_result` 字段语义不变；投影事实只是新增的事件字段。聚焦回归覆盖透传、
头尾和错误投影、规范 observation/verification 内容、Runtime 自有 artifact 复用、伪造
artifact metadata 拒绝、脱敏、preflight 缩减以及并行身份/顺序。既有 artifact store/read、
background、TUI/CLI/ACP、recovery、
compaction、rollover、Working Set、history 和 architecture 检查继续作为兼容性门禁。
