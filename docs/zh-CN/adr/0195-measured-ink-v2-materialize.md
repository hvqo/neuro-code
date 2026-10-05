# ADR 0195：Measured Ink V2 Materialize

**简体中文** · [English](../../en/adr/0195-measured-ink-v2-materialize.md)

- 日期：2026-10-05
- 状态：已接受实现；等待最终生产终端验收
- 范围：仅 Assistant 正文文字到达呈现

## 决策

对符合条件的新呈现正文 grapheme 采用用户选择的 A22 Materialize。固定参数为：最多
ΔL*=22、寿命 160ms、24fps、最多八个活跃 glyph，前景与背景对比度至少 4.5:1。
正文 25ms/40Hz commit cadence 保持独立。Canonical delta 仍完整且立即更新；不改变
Runtime event、模型输出、代码流式渲染或 Agent pulse。

glyph 首次呈现时，渲染前景朝实际 canvas/background 移动，CIELAB L* 最多变化 22，随后
沿单调路径恢复 canonical foreground。不允许 overshoot。160ms 到期后精确恢复原 Rich
style。新到达 glyph 不得修改已有 birth 时间，也不能重启动效。

## 颜色与能力契约

根据具体 canonical foreground 和实际可见背景生成动画计划。Graphite/Porcelain 使用解析后
的 canvas fill；System 必须有经过验证的 terminal palette。TrueColor 在 CIELAB 中插值并
输出 RGB。ANSI256 先量化候选颜色，再检查输出差异与对比度；首帧必须区别于 canonical，
且至少保留两种不同的可见量化颜色。无法证明对比度、单调性或可见差异时，该 glyph 保持 canonical。
ANSI16、未知能力和无法解析的颜色均静态显示。受对比度限制时可采用小于 22 L* 的变化；
绝不超过上限或降低对比度下限。

只叠加 foreground。Rich 背景、metadata 和其他 style attributes 均保留。现有 source 证明边界
不变：只处理顶层普通正文中安全、完整的 grapheme。Heading、格式化 span、link、inline/fenced
code、syntax 与 diff 颜色、复杂/不安全 grapheme 和来源不确定的映射保持静态。每段 source
metadata 尾部仍最多 12 个 glyph；活跃动画上限为八个。流式 Markdown 重建期间会保留仍活跃的
source identity，直至 settle，但不会改变首次呈现 birth。

## 生命周期与刷新所有权

生命周期由 ADR 0194 管理：接收阶段登记 source range，首次可见呈现建立 birth，共享时钟只在
缓存的渲染行上叠加效果直至 settle。本 ADR 只改变视觉计划。Animation OFF 不创建 arrival 动画
工作、timer 或 repaint。24fps 下只刷新受影响行；tick 不重新解析 Markdown，也不请求 layout。
所有 glyph settle 后停止 timer。Resize、主题变化、取消、完成与 dispose 继续遵循 ADR 0194 的
清理及 source identity 行为。Fenced-code streaming 由 ADR 0193 管理，不属于 Measured Ink。

## 验证与验收

回归覆盖首次呈现 birth、A22 颜色变化与单调 settle、精确恢复 canonical、对比度、ANSI256 差异、
能力失败关闭、八 glyph 上限、append rebuild 后 birth 稳定、CJK cell geometry、Markdown 保护、
OFF 零刷新，以及既有生命周期测试。生产验收需在真实 Konsole 会话中比较启用效果与 OFF；
headless 测试不能证明真实终端调色板下的视觉感知。

```bash
uv run neuro
```

比较前可在 Appearance → Input 中启用或关闭 `text_arrival_animation`。
