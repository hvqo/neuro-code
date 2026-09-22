# TUI 外观主题

在 `/settings` → **外观主题** 中选择。方向键上下移动即可预览，Enter 或点击选项应用并保存，Esc / 返回恢复进入选择器时的主题。Tab 可在列表和返回按钮之间导航；长列表支持滚动。预览不写入偏好，也不会提交草稿。

输入区不显示额外标题，编辑区下方放置换行提示和发送按钮。Enter 与发送按钮共用已有提交流程；Ctrl+J 或 F2 换行，也可点击「换行」按钮，或用 Tab 聚焦该按钮后按 Enter。Alt+Enter / Shift+Enter 仅在终端能透传独立按键事件时可用，不能保证所有终端都支持。输入区随终端宽度展开，多行高度有界；输入区与历史用户消息使用独立于普通面板的主题底色；输入区顶部保留细分隔线，聚焦时使用主题强调色，用户消息左侧使用强调色细线。占位文本、操作提示、光标与文字选区也使用更清晰的对比色。`system` 保留终端默认底色，通过默认前景色分隔线区分区域。

共有 13 个选项。`porcelain` 为暖瓷白；`graphite` 保留已有曜石黑的存储标识；`matrix` 是 Neuro Code 自行设计的黑底绿字配色。首次启动仍默认暖瓷白，已有选择保持不变。

`system` 直接使用终端默认前景、背景与 ANSI 颜色，通过 Textual 的 `textual-ansi` 渲染模式跟随终端，不查询 OSC、不猜测 RGB，也不等同于读取操作系统的深浅模式。外观由终端配置决定；选中项使用反色和标记保持可辨识性。设置与首次供应商配置共用相同主题。

以下主题参考原项目的颜色值，再独立映射到 Neuro Code 的正文、表面、焦点、代码与状态语义；没有移植其布局或实现。部分辅助文字提高亮度，以适应 TUI 小字号与阅读对比度。它们不是原项目的逐像素复刻。

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

文字使用按主题分别定义的语义配色：标题和链接为蓝色，行内代码与工具活动为青色，数字与装饰器为橙色，关键字为紫色，字符串和成功状态为绿色，提醒使用暖黄色。正文保持中性前景色。浅色主题采用较深文字色，深色主题采用柔和亮色；Matrix 保留绿色主调，System 使用 ANSI 色。代码、Markdown 和 diff 在切换主题后使用同一套映射，不修改消息内容。
