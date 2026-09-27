# ADR 0180：Reasoning Efficiency V1（推理效率 V1）

**简体中文** · [English](../../en/adr/0180-reasoning-efficiency-v1.md)

- 状态：已接受
- 日期：2026-09-27
- 范围：在现有 Main Agent loop 中提供确定性的阶段指引与证据充分性控制

## 背景

Execution Efficiency V1 在 repository-review 夹具中减少了碎片化的模型/工具往返，但真实 A/B 表明，模型请求变少并不必然减少 Provider 耗时。少数请求仍会在提出证据需求前进行很长时间的推理。此前把连续 singleton 探索视作 `EXPLORE → ANALYZE` 信号的规则在语义上不正确：碎片化说明应该批量获取剩余证据，不代表证据已经充分。

## 决策

**低信息探索继续留在 EXPLORE。** 连续两轮新的、成功的简单 singleton 证据调用后，现有每回合控制器只发出一次有界、仅追加的批量取证提示，并保持 `EXPLORE`。提示模型结合现有 Plan 与 Working Set 整理剩余需求，一起请求已知的独立证据，并等待有依赖关系的结果。它不会推断任务已经完成，也不会增加模型调用。

**进入 ANALYZE 必须通过确定性的证据充分性 Gate。** 至少要有一条新的成功证据 fingerprint、没有已知未解决工作，并且没有待验证事项。首选现有结构化 Plan 的全部完成状态。只有不存在 Plan 时，才允许现有 Working Set 证明进度已完成；此时 Goal 与 Progress 必须有内容，且 Unresolved Work 与 Next Steps 均为空。Working Set 状态未知时 fail closed。如果应用无法用这些现有信号证明证据充分，就留在 `EXPLORE`。

**先行动，再进行广泛推理。** EXPLORE 提示保持简短、稳定。当剩余文件、搜索或检查已经可明确列出时，提示模型先提出这些工具调用，再做长篇综合。所有调用仍由模型声明；现有 scheduler 与可执行工具能力仍权威决定依赖、并行执行、权限与顺序。

**分析可以安全回退。** `ANALYZE` 阶段发出工具批次后会记录 `analysis_backtrack` 计数。只需补证据时返回 `EXPLORE`，并用一次有界提示要求模型列出剩余的定向需求、批量执行独立读取。工作区变更和验证仍保留 `VERIFY` 边界，同时递增 analysis backtrack 计数。验证后发现新证据缺口时可以回到 `EXPLORE`；不会抑制必要工具。

**FINALIZE 是受 Gate 约束的诊断阶段。** 仅当模型本次没有返回工具调用、没有已知未解决工作、现有 Plan（若存在）已完成，且验证已通过或所有必需项都明确为满足/不可执行的 blocked 状态时，才记录该阶段。Supervisor 的终止决定本身不能证明模型不再需要工具。这些检查不延迟或改变终止行为。

**V1 不修改 Provider 推理强度。** 用户选择的 effort 保持不变。本切片不增加供应商专属的逐请求控制、Planner、Judge、步数限制或 System Prompt 改写。现有仅追加的 Runtime 指引不改变 Stable Prefix；Trace 仍只含 metadata 且不拥有决策权。

## Trace

现有 Trace 摘要按请求开始时的阶段，报告 Main Model 与 finalizer 请求数、输出 token 和 Provider 耗时；同时报告 `analysis_backtrack_count`、ANALYZE 请求所产生的工具调用数，以及 FINALIZE 前返回 EXPLORE 的次数。Trace 不保留 Prompt、工具负载或隐藏推理。

## 确定性基准

repository-review 夹具比较两种路径：碎片化基线在收到批量提示后仍逐个读取，并在早期推理中消耗模拟 Provider 时间和输出 token；优化夹具则批量读取已知的三个独立剩余文件。两者都读取五个文件并给出相同结论。夹具测量值只属于本地测试，不代表真实 Provider 性能。依赖读取变体仍按顺序执行。

## 后果

- 低信息轮次提示模型批量取证，但绝不会据此宣称证据充分。
- 只有现有 Runtime 状态能支持时才进入分析；无法确定时留在 EXPLORE。
- ANALYZE 与 VERIFY 可在不限制正确补充工作的前提下回退。
- Provider 推理策略、Prompt Cache、Supervisor 与规范历史保持不变。
- 按阶段展示的 Trace 指标可在未来 A/B 中区分 EXPLORE、ANALYZE、VERIFY、FINALIZE 的耗时，且不记录隐藏推理。

## 验证

确定性测试覆盖低信息批量提示但不切换阶段、证据充分性及未知状态 fail closed、分析和验证回退、FINALIZE Gate、阶段遥测、仅追加 Context、稳定 System 内容、依赖读取、任务正确性与规范历史不变。
