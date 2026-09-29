# ADR 0184：TUI 颜色系统与对比度基础 V1A

**简体中文** · [English](../../en/adr/0184-tui-color-system-and-contrast-v1a.md)

- 状态：已接受
- 日期：2026-09-29

## 背景

V0 的 81 张快照建立了改造前基线，并暴露出语义角色坍缩，尤其在 System
主题中，表面、选中表面、边框、弱化文字和输入区结构常被最终映射到相同的
ANSI `bright_black`。源代码虽然分别命名了别名，Textual/Rich 的最终色值却
没有区分。交互测试可以通过，而实际阅读层级依然难以辨认。

## 决策

Textual 与 Rich 渲染统一使用紧凑的语义颜色约定：

| 层级 | 角色 |
| --- | --- |
| 结构 | `canvas`、`surface`、`surface_subtle`、`surface_selected` |
| 阅读 | `text_primary`、`text_secondary`、`text_muted` |
| 边界 | `border_subtle`、`border_normal`、`border_focus` |
| 含义 | `accent`、`success`、`warning`、`error` |

既有 `BG_*`、`FG_*` 和组件变量只是这些角色的兼容别名，不形成第二套配色
权威。使用填充表面的主题需要区分画布与表面、选中与普通表面、边界与底色，
以及前景文字与所在背景。System 则有意减少填充，不模拟深色配色。焦点强于
普通边界。Assistant 正文是主要阅读层，用户消息、工具、状态和输入区用更克制
的颜色形成次序，不改变几何布局或字重政策。代码语法色与会话颜色隔离。

Graphite 和 Porcelain 是接受数值门禁的深色与浅色参考主题。各相关表面上的
普通文字以 WCAG 4.5:1 作为回归保护，普通边界和焦点约以 3:1 保护，同时
检查主文字、次要文字、弱化文字的对比度顺序。弱分隔线可以更克制。这些
门禁用于阻止意外坍缩，不构成审美认证，也不保证每个终端的实际呈现。

System 是终端感知的自适应调色板，而不是透明/仅默认背景主题。一个小型启动
适配器会在 Textual 接管终端输入前，有界地查询一次 OSC 10/11。若终端返回了一对
对比度足够的默认前景/背景色，且输出能力为 TrueColor 或 ANSI256，则从实际 FG/BG
派生表面与次要文字颜色。ANSI16、查询不支持、响应无效或默认颜色对比度过低时，
fail-soft 回退到终端默认色、可见的 ANSI 白色面板细线、dim 次要文字、反色选中态和
ANSI 蓝色焦点。查询最多增加 60ms，失败不会阻止启动。System 不把
`ansi_bright_black` 用作大面积填充。

画布、弱/选中表面、Composer 和 User Message 在输出能力允许时保持不同角色。
System 专属的 Composer 细线复用现有顶部留白，因此测得的几何尺寸与文字坐标不变；
焦点状态将该细线改为自适应 accent，fallback 时则使用 ANSI bright blue。保留已经
通过验收的 Markdown 行内代码“仅前景色/无背景”契约；围栏代码和 diff 保留独立样式。
终端配色未知时无法声称 RGB 对比度，确定性语义角色测试覆盖该 fallback。Textual SVG
使用注入的深色、浅色或 unknown fixture，仍不代表每种真实终端配色，仍需真实终端验收。

V1A 更新 V0 的 81 张快照，并为深色、浅色和 unknown palette 在对话与设置视图
增加六张确定性 System 截图。本轮修正只改变 System palette resolution 以及表面/
边框颜色，保留此前已通过验收的行内代码修复。不改变 Composer 高度、宽度、外边距、
消息几何、Header、Markdown 层级、工具活动、设置和 Trace 布局，也不改变 Runtime、
Context 或持久化。Composer 已有的一格顶部留白改为边界线，其内容坐标保持不变。

## 结果与验证

切换主题仍只改变配色，不改变布局语义。三个 V0 主题、三个 viewport 的快照约束
最终呈现；六张额外快照固定自适应/fallback palette fixture。对比度与角色测试约束
最终色值，主题切换和焦点测试检查组件区域及焦点颜色。Gallery 可通过 `--before-ref`
将已提交的 V0 SVG 与 V1A 并排比较；palette 专用样本只显示 V1A。具体命令和 ANSI
限制见 `tests/visual/README.md`。后续视觉阶段若要修改几何布局，需单独审查。
