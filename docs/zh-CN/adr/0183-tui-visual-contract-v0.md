# ADR 0183：TUI 视觉契约与截图基线 V0

**简体中文** · [English](../../en/adr/0183-tui-visual-contract-v0.md)

- 状态：已接受
- 日期：2026-09-29

## 背景

Neuro Code 已有语义主题 token、统一的会话阅读轴、分组工具活动、自适应
Composer 高度和无头 Textual 测试。这些测试保护交互与局部渲染契约，但尚无
覆盖多种视口和主题、可重复且便于审查的全屏视觉基线。因此，即使行为测试
通过，整体阅读体验仍可能发生视觉回归。

本 V0 在真正重构视觉之前建立确定性截图回归。它不修改生产样式、主题值、
Markdown 规则、Tool Activity、Runtime、Agent 行为、Prompt 或 Context、Session
行为及 Provider 行为。

## 决策

使用 Textual 从真实 TUI 组件导出确定性 SVG 截图。fixture 使用本地假 runner、
固定标签和时钟、固定工作区元数据，不联网，也不使用持久 Session 或真实
Provider。基线矩阵覆盖空会话、用户与助手消息、长 Markdown、分组工具活动、
错误、权限确认、多行 Composer、设置和 Trace；视口为 120×40、100×32、80×24，
主题为 Graphite、Porcelain、System。

以下原则构成后续 TUI 工作的视觉契约：

- **中性色优先：** 普通会话内容和界面结构采用中性表面与文字。
- **默认常规字重：** 粗体用于标题、选中状态和有意义的强调。
- **一个主强调色：** 主强调色用于交互与焦点，避免多个装饰色互相竞争。
- **语义颜色：** 成功、警告、错误颜色只表达有意义的状态。
- **语法高亮独立：** 代码语法拥有独立调色板，不重定义会话内容配色。
- **紧凑且自适应的 Composer：** 高度随有效输入内容和可用视口空间调整。
- **清晰阅读轴与层级：** 会话、状态和活动保持对齐并有清楚的视觉顺序。
- **适配浅色与深色层级：** 浅色、深色及终端提供的颜色环境中都要保持对比度和表面区分。
- **主题只改变调色板：** 主题可以改变颜色，但不能改变布局语义或控件位置。

这些原则是评审约束，不要求本阶段重做当前 TUI。参考了 Codex TUI 的克制
语义色、自适应 Composer、基于源文本的 Markdown 重排，以及分离的历史/活动
单元等原则；没有复制 Rust 实现或目录结构。

## 已知基线问题

V0 截图揭示 System 主题存在语义角色对比不足：surface、selected surface、border、
dim/muted text 以及 Composer 的表面、边框和弱化文字大量映射到相同或近似的
ANSI bright_black。这是保留在基线中的改造前缺陷；记录它不代表认可当前视觉质量。
System 快照表示 Textual 的确定性 ANSI 渲染，不代表每位用户终端的实际 ANSI 调色板。
V1A 的首要目标是恢复语义角色对比，并改进 System 主题适配。

## 后果

- 全屏变化可以对照按视口区分的已提交基线进行审查。
- SVG 基线可直接检查，文本、几何和调色板变化可进行差异比较，且无需新增渲染依赖。
- 更新快照必须显式启用并进行视觉检查；普通测试不会改写预期输出。
- 后续视觉重构应先更新契约，或在设计评审中解释偏离原因，再开展大范围组件修改。

## 验证

使用 `uv run pytest tests/test_tui_visual_snapshots.py -q` 运行定向测试。
基线更新和画廊查看步骤见 `tests/visual/README.md`。本 ADR 不授权生产界面改动。

## V2B 阅读契约演进

V2B 让 Assistant Markdown 标题使用 UI 语义角色：H1 accent + bold、H2 accent、
H3 primary emphasis、H4 primary、H5 emphasis、H6 secondary。仅相邻顶层正文段落
在已有非 compact Shell 中额外增加一行空白；compact 保持一行空白。自动换行以及
heading/list/code/quote/table transition 保留原间距。render-time Shell policy callback
让 resize 与 streaming 重排而不累加 spacer、不修改原文。详见
[ADR 0188](0188-assistant-markdown-semantic-reading-rhythm-v2b.md)。
