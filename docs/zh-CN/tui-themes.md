# TUI 外观主题

在 `/settings` → **外观主题** 中选择。方向键上下移动即可预览，Enter 或点击选项应用并保存，Esc / 返回恢复进入选择器时的主题。Tab 可在列表和返回按钮之间导航；长列表支持滚动。预览不写入偏好，也不会提交草稿。

输入区无额外标题；编辑器与发送按钮共用一行，不再常驻快捷键说明。Enter 与发送按钮共用已有提交流程。Ctrl+J / F2 换行，在 F1 / `/help` 中说明。Shift+Enter 只在 Textual 收到独立修饰键事件时可用；不把 SS3 keypad Enter 猜成 Shift。多行高度有界，V1A palette / V1B typography 语义保持不变。System 在终端 RGB 可靠时使用自适应表面；键盘能力与主题无关。

共有 13 个选项。`porcelain` 为暖瓷白；`graphite` 保留已有曜石黑的存储标识；`matrix` 是 Neuro Code 自行设计的黑底绿字配色。首次启动仍默认暖瓷白，已有选择保持不变。

`system` 在 Textual 开始读取终端输入前有界查询一次 OSC 10/11，截止时间为 60ms。对比度足够的默认 FG/BG 配对在 TrueColor/ANSI256 下用于派生表面和次要文字；ANSI16、终端不支持、响应无效或默认颜色对比度不足时，fail-soft 回退默认色、ANSI 白面板细线、dim 次要文字、反色选中和 ANSI 蓝色焦点。查询是可选项，失败不会阻止启动。System 不把 `ansi_bright_black` 用作大面积填充，也不推测操作系统深浅模式。Markdown 行内代码没有背景填充，围栏代码和 diff 保留独立样式。实际对比度仍取决于终端配色；确定性快照注入固定的深色、浅色或 unknown palette，不代表任何真实终端配色，应按 `tests/visual/README.md` 的清单在真实终端检查。设置与首次供应商配置共用主题；首次供应商配置使用可见的 unknown-palette fallback。

以下主题参考原项目的颜色值，再独立映射到 Neuro Code 的正文、表面、焦点与状态语义；没有移植其布局或实现。部分辅助文字提高亮度，以适应 TUI 小字号与阅读对比度。它们不是原项目的逐像素复刻。

| 标识 | 参考变体 | 配色来源 |
| --- | --- | --- |
| `tokyonight` | Night | [tokyonight](https://github.com/folke/tokyonight.nvim) |
| `everforest` | Dark / medium | [everforest](https://github.com/sainnhe/everforest) |
| `ayu` | Dark | [ayu](https://github.com/ayu-theme/ayu-colors) |
| `catppuccin` | Mocha | [catppuccin](https://github.com/catppuccin/palette) |
| `catppuccin-macchiato` | Macchiato | [catppuccin-macchiato](https://github.com/catppuccin/palette) |
| `gruvbox` | Dark | [gruvbox](https://github.com/morhetz/gruvbox) |
| `kanagawa` | Wave | [kanagawa](https://github.com/rebelot/kanagawa.nvim) |
| `nord` | Nord | [nord](https://github.com/nordtheme/nord) |
| `one-dark` | Atom One Dark | [one-dark](https://github.com/Th3Whit3Wolf/one-nvim) |

偏好使用现有原子 JSON 存储中的 `theme` 字段；缺失或无效值回退到 `porcelain`。应用失败不会被误报为保存成功：保存失败时保留当前外观，并显示错误。切换主题会更新 CSS、Rich Markdown、代码与既有消息组件，保留草稿、光标和会话内容。

Graphite 和 Porcelain 使用中性阅读层级：正文优先，次要与弱化文字逐级降低，画布、面板和选中表面克制区分，普通边框可见，焦点边框更醒目。普通文字与关键边界有 RGB 对比度回归保护。一个主强调色用于链接和交互；成功、警告、错误色仅表达状态。Markdown 标题与工具活动遵循中性会话层级。代码语法（包括语法高亮色）仍有独立配色，不改变消息内容。其他已保存主题标识继续兼容；V1A 的 RGB 数值门禁只覆盖 Graphite 和 Porcelain。参见 [ADR 0184](adr/0184-tui-color-system-and-contrast-v1a.md)。

## 独立语法主题

打开 `/settings` → 外观 → **语法主题**。选择后 Python 代码立即预览；保存会记住
选择，Esc/返回恢复打开时的偏好。既有与流式围栏代码同步刷新，正文、行内代码、草稿、
光标、代码 surface 与外壳几何保持原状。保存失败可见，当前配色仍保留。

自动/默认在深色 RGB 代码底色上选择 GitHub Dark，浅色选择 Friendly Light。
六种显式选择是 GitHub Dark、One Dark、Monokai、Dracula、Friendly Light、
Solarized Light，复用已安装的 Pygments 配色与 lexer。覆盖 Python、Rust、JSON、
Shell、Diff；未知语言普通渲染。必要时仅调整 token 前景，使 RGB 对比度达到 4.5:1，
所以深色配色可搭配 Porcelain，浅色配色可搭配 Graphite。代码背景始终由 UI 拥有。

独立可选 `syntax_theme` 字段在旧配置/非法值下默认为 Auto；其他偏好写入不会覆盖它。
System 使用既有终端 palette；可靠 RGB 走同样的适配，未知/ANSI16 使用 default
foreground 与少量 ANSI token 色。保存的选择保持不变。实际 ANSI 对比度需要真实
终端验收。详见 [ADR 0187](adr/0187-independent-tui-syntax-theme-v2a.md) 以及
`tests/visual/README.md` 中的语法 gallery 与人工清单。
