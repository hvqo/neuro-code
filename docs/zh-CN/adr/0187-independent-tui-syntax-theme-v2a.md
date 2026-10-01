# ADR 0187：独立 TUI 语法主题 V2A

**简体中文** · [English](../../en/adr/0187-independent-tui-syntax-theme-v2a.md)

- 状态：已接受（架构决策；等待人工视觉验收）
- 日期：2026-10-01

## 背景

Rich Markdown 已通过 Pygments 解析围栏代码。旧 `_MonochromePygmentsStyle` 与
`_PaletteSyntaxTheme` 将代码颜色统一映射到 `UiTheme`，因此界面 accent/status
变化也会改变代码配色。原先没有独立语法偏好或代码预览。V1A 行内代码无背景标签、
V1B 排版以及 V1C 外壳/输入契约需要保持稳定。

## 决策

共享 `SyntaxTheme` 标识与 `UiTheme` 独立持久化。表现层注册表复用已安装的
Pygments 配色：GitHub Dark、One Dark、Monokai、Dracula、Friendly Light、
Solarized Light，另有 Auto。深浅背景均有成熟方案，无需复制调色板或增加 lexer/依赖。
注册表为固定允许列表，不引入插件或 Theme Editor。

渲染链路为围栏语言 → 既有 Pygments lexer → 语义 token → 独立语法解析器 →
Rich Syntax。语言标识去除 CommonMark 附加 metadata；未知语言安全退回普通代码。
Python、Rust、JSON、Shell、Diff 有确定性 fixture。关键字/类型、字符串、数字、
注释、名称/函数/类/内建/装饰器、运算符与标点保留 lexer 语义。Diff 的新增、删除、
文件元数据与 hunk 使用所选配色；若上游将 diff 角色设成与 context 相同，则复用该
配色中已有的可区分语义前景色。

代码块 surface 与周围 padding 仍由 UI 唯一拥有，token 样式没有背景填充。语法主题
不修改正文、行内代码、工具/状态 UI、Composer、键盘或外壳几何。代码保持 regular，
上游注释 italic 可以保留。Rich 原有代码块 padding、换行、选择与消息原文不变。

Auto 在代码 surface 的 RGB luminance 小于 0.179 时选择 GitHub Dark，否则选择
Friendly Light。阈值对应深浅前景的对比度交点，不按终端名称猜测。显式选择不会因 UI
变化而改写。Token 对真实 UI RGB surface 的对比度达到 4.5:1 时保留上游颜色，否则
有界地向白/黑混合，在保留色相的同时恢复可读性。未知 ANSI palette 不伪造 RGB
对比度。System 复用 V1A 的终端 palette：可靠的 TrueColor/ANSI256 surface 走 RGB
路径；未知/ANSI16 退回 default foreground、少量 ANSI token 色与 dim 注释。仍保留
用户保存的显式选择，预览说明降级。终端 palette 行为与探测流程不变。

Appearance Settings 增加独立语法主题入口。Select 与代表性 Python 预览立即更新，
当前与流式围栏代码同步刷新。保存持久化，取消恢复打开时的选择；预览和切换均不改变
持久对话、草稿或光标。保存失败保留当前可见选择并报告错误，与 UI Theme 契约一致；
写入仍归原子偏好端口所有。

JSON 增加可选 `syntax_theme` 字段，schema 仍为 version 1。缺失、非法或不可读取时
使用 Auto。其他偏好写入保留语法选择，反向同样保持；启动独立恢复 `theme` 与
`syntax_theme`。

## 验证与局限

测试覆盖真实 lexer/原文保持、token 对比度与无背景样式、diff 角色、System 能力
降级、偏好迁移/重启、预览/取消/保存/失败，以及三种 UI Theme/视口下的几何。视觉
harness 为八种代码/正文 fixture 和语法设置 fixture 提供 120×40、100×32、80×24
矩阵，并覆盖全部显式选择和注入的深色/浅色/未知 System palette。Gallery 支持聚焦
V1C/V2A 比较。
Fixture 创建时仅在 harness 内移除环境 `NO_COLOR`，确保快照保留实际 token 色。
旧基线继承了灰度过滤器；本轮显式重新生成以纠正 harness，不改变 UI palette 或
生产环境的 `NO_COLOR` 行为。最终 SVG 回归同时验证环境无关性和实际关键字/字符串色。

确定性 RGB/ANSI 快照不能证明每一种真实终端 palette。ANSI 降级色的实际对比度需要
人工检查。成熟 lexer 不保证语义分析：例如 Python 使用处可能被识别为 `Name`，
不是 `Name.Function`。V2A 提供高亮，不推断代码含义。自定义编辑器与进一步视觉重构
留待后续。
