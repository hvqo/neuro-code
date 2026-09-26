# ADR 0178：Runtime Trace 与 Agent DevTools V1（运行时追踪与 Agent 开发工具）

**简体中文** · [English](../../en/adr/0178-runtime-trace-and-agent-devtools-v1.md)

- 状态：已接受
- 日期：2026-09-26
- 范围：进程内 Agent Runtime 诊断、Textual Trace 查看和仅含 metadata 的导出

## 状态

已接受。

## 背景

Session history 能说明对话内容与持久化工具调用，但不是运行时序视图。要定位慢轮次、重复模型步骤、供应商重试、上下文压力、权限等待和串行工具批次，需要有界的生命周期事实，同时不能保存 Prompt 或工具正文。普通日志缺少稳定层级和有用的轮次聚合，也不能成为另一份 Runtime truth。

## 决策

**Trace 是只读投影，不是历史或权威。** Runtime 复用已有生命周期事件，并新增少量临时计时事件。应用层拥有的 `TraceCollector` 将事实归约为 trace、turn、step、request、provider attempt、tool batch、tool、context、replan、verification、finalizer 和 subagent 记录。它不参与执行决策，也不会进入 Prompt、Project Memory、compaction source、recovery fact 或持久 Session history。

**先观察，再解释。** Runtime 报告单调时钟耗时和类型化结果。轮次摘要、耗时最长操作、并行重叠、缓存用量和时间轴由 collector 根据事实计算。没有 LLM 解释或评分 Trace。trace ID 标识一个被观察的回合；request ID 和 tool-call ID 复用现有身份。子 Trace 可以引用父 Trace，但不会合并两者的记录或历史。

**计时名称只表示实际观测到的边界。** Provider TTFT 从 Provider request 开始，计至首个响应输出事件（文本、推理或工具输出）。用户可见 TTFT 从 TUI 接受回合开始，计至首个非空可见文本 delta。Request duration 与 stream duration 分别记录。Tool permission wait 为 `TOOL_REQUESTED` 到 `TOOL_STARTED`；execution duration 为 `TOOL_STARTED` 到工具终态事件。Context build、verification、finalizer 和 replan span 使用单调时钟。系统不推断私有 network/connect 耗时。

Provider Attempt 时长由 model stream processor 实测。其 Trace 起点按测得时长从失败或完成事件的终止边界回推，因此每个 attempt 都在其父 MODEL request 时间范围内。终态 turn identity 会同步到 TURN record 和所有子 record。效率摘要分别显示 Provider、Permission Wait、Tool Execution、Context 与 Runtime/Other；Runtime/Other 是扣除并合并这些已测区间后的剩余回合时间。汇总缓存复用率采用 token 加权比例 `sum(cache_read_tokens) / sum(input_tokens)`，不是每请求比率的简单平均。

**保留和渲染均有界。** 内存 collector 最多保留 32 个 turn、总计 8,192 条 record、每个 turn 2,048 条。Ledger 每次最多渲染 48 行。导出仅包含 metadata，大小上限为 4 MiB，支持 JSON 和 JSONL。进程重启会清空 Trace，因为它是诊断数据，不是持久 truth。

**隐私采用白名单。** Trace 可保存安全的 provider/model 标签、身份 ID、状态/错误类别、耗时、token/cache 数量、context generation 和 cache boundary 事实、工具名称、字节数、截断与 artifact 标志，以及有界 provider-attempt 摘要。不会保存 API key、authorization、完整 Prompt、隐藏推理、工具参数、工具结果或 shell output 正文、带 secret 的 URL/query 或供应商原始错误消息。导出使用相同的投影。

**采集失败采用 fail-open。** Collector 或诊断交付失败不会改变 Agent 结果。诊断事件只交付给当前 interface，不会返回到 `AgentRunResult.events`，也不会持久化。CLI JSONL 会过滤这些仅供 DevTools 使用的事件，以保持现有公开事件流。Provider request、cache boundary 语义、Session history、permission、workspace、sandbox、verification 与 compaction 仍由原有组件权威负责。

## 后果

- `/trace` 打开可分页、可搜索的 Textual ledger，提供 inspector、计时与用量摘要，以及基于实测 span 的时间轴。
- `/trace export` 复制仅含 metadata 的 JSON；`/trace export jsonl` 复制仅含 metadata 的 JSONL。
- 单个轮次视图可以对照 Provider TTFT、用户可见 TTFT、token/cache 用量、重试/故障转移、permission wait、execution time、并行重叠、context 操作、replan、verification、finalization 和已呈现的 subagent 执行。
- Trace 只在进程内保留。OpenTelemetry、持久 Trace 存储、供应商私有 network timing、自动效率建议和 LLM 生成的 Trace 摘要均不在范围内。

## 验证

测试覆盖层级、模型计时与用量、Provider retry/failover、并行工具批次与权限等待、context 与 compaction 事件、rollover、replan、verification/finalizer/subagent span、失败/取消、保留限制、脱敏/导出、临时交付、模型投影不变、CLI 事件过滤，以及包含 1,100 条记录的分页 TUI Trace。
