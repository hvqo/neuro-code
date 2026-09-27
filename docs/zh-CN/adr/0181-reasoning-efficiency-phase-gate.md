# ADR 0181：推理效率阶段门修正

**简体中文** · [English](../../en/adr/0181-reasoning-efficiency-phase-gate.md)

- 状态：已接受
- 日期：2026-09-27
- 范围：建议性执行阶段、证据充分性、完成诊断与阶段遥测
- 取代：ADR 0180 中对未知状态的阶段门语义

## 背景

Reasoning Efficiency V1 发布后的真实 DeepSeek A/B 出现性能回退：一个没有 Plan 的
仓库审查任务一直留在 `EXPLORE`，反复请求读取文件，触及工具安全上限后由 execution
finalizer 兜底。Runtime 要求必须有已完成 Plan 或完整 Working Set 才能离开探索阶段，同时
又把普通 `NEXT_STEPS` 条目和不可读取的 Working Set 都当作已知未解决工作。因此，缺少
完成标记被误读为证据不足。Trace 阶段汇总还包含 Finalizer 请求，而回合级模型请求计数并未
包含它们。

后续真实 DeepSeek A/B 又发现阶段振荡：`UNKNOWN` 被错误用作
`EXPLORE → ANALYZE` 的触发条件；分析阶段请求工具后回退到 `EXPLORE`，每批新证据又让阶段
重新进入 `ANALYZE`，增加了模型请求和 Provider 耗时。

## 决策

**执行阶段仍是建议。** 阶段用于指导模型，不成为新的 Runtime authority，不抑制工具调用、
改变 Supervisor 决策、用户推理强度或安全上限。

**证据充分性采用三态。**

- `KNOWN_INSUFFICIENT` 表示存在明确阻塞：未完成的 active Plan、Working Set 中明确的
  `UNRESOLVED_WORK` 条目，或待处理的必需验证。
- `SUFFICIENT` 要求成功证据进展，并且现有 Plan 已完成；若不存在 Plan，则要求完整的
  Working Set，其 Goal 和 Progress 有内容，且 Unresolved Work 与 Next Steps 都为空。
- `UNKNOWN` 表示既没有完成证明，也没有明确阻塞。它不等于证据不足。

普通 `NEXT_STEPS` 文本是建议，不是要求。Working Set 缺失或读取失败时，充分性为 `UNKNOWN`，
不会强制继续探索。模型响应不再请求工具时，只要没有明确未解决要求、没有未完成的 active
Plan，且必需验证已满足或明确 blocked，就记录 `FINALIZE`。完成元数据未知不会阻挡该诊断状态
转换。

**`UNKNOWN` 对阶段选择保持中性。** 它不触发 `ANALYZE`、不要求持续探索，也不阻止正常完成。
`EXPLORE → ANALYZE` 必须由现有 Runtime 事实给出的正向 `SUFFICIENT` 信号触发；证据数量、
低信息模式和未知状态都不能替代 readiness。Planless 任务的完成状态若一直未知，可以继续留在
`EXPLORE`；模型停止请求工具且没有明确阻塞时，可直接综合并正常完成。

**低信息探索仍只提示批量取证。** 连续两轮新的 singleton 证据后，只发一次有界提示，要求将
可识别的独立读取合并批量，并保持在 `EXPLORE`。未知状态下若继续出现 singleton 读取，只追加
一次有界 `excessive_exploration` 检查点，建议仅在存在具体未解决证据缺口时继续调用工具，
否则综合已有证据。它是 `EXPLORE` 检查点，不是阶段转换。充分性未知的成功独立批次也继续留在
`EXPLORE`。

**Analysis backtrack 使用 readiness-edge hysteresis。** 模型在 `ANALYZE` 请求工具时记录
backtrack，并将证据工作交回 `EXPLORE`。回退后，重复的 `SUFFICIENT`、`UNKNOWN` 或普通证据
数量增长本身不能让阶段重新进入 `ANALYZE`。如果回退发生时充分性已经为正向状态，控制器必须先
观察到非充分状态，再观察到新的正向 `SUFFICIENT` 状态；如果回退时充分性为未知或不足，之后的
正向状态即可作为新的 readiness edge。重复或失败输出不会被当作新证据。独立调用仍由模型和现有
Scheduler 批处理，依赖调用仍按顺序执行。

## Trace 契约

回合与阶段摘要分别报告 `main_model_requests`、`finalizer_provider_requests`，以及
`provider_time_main_ms`、`finalizer_elapsed_ms`。Finalizer 总耗时包含 Provider 调用周围的有界
编排，因此不标为 Provider 耗时。Finalizer dispatch 不再增加 Main Model 阶段请求计数。效率
事件可以报告三态充分性；Trace 仍只含 metadata，不包含 Prompt、工具负载或隐藏推理。

## 验证

确定性回归覆盖未知状态中性路由、planless 正常完成、正向 readiness、analysis backtrack 后的
readiness-edge hysteresis、普通 Next Steps 下的正常完成、明确阻塞项的保留、有界过度探索提示、
证据去重，以及分离后的 Main/Finalizer Trace 指标。仓库审查夹具检查正确性、证据覆盖、模型请求数、
工具数、批次、阶段计数、输出 token、缓存复用和模拟 Provider 耗时。夹具耗时只是测试数据，不代表
真实 Provider 性能。
