# ADR 0186：TUI Composer 与外壳布局 V1C

**简体中文** · [English](../../en/adr/0186-tui-composer-and-shell-layout-v1c.md)

- 状态：已接受
- 日期：2026-09-30

## 背景

V1A 与 V1B 之后，120×40、100×32 中的空 Composer 仍为单行草稿预留 9 行，
Header 预留 3 行。输入表面至少 6 行，操作栏上方空行、运行状态栏上方空行和外层
底部留白共同挤占主阅读区，尽管 `PromptInput` 已能测量自动换行后的文档高度。
在 80×24 下，5 行 Composer 仍占据较大比例；空会话尤为明显。

## 决策

### 布局契约

视觉权重依次为对话内容、Composer、会话元数据、品牌。Header 和底部运行状态栏
各占 1 行；模型、推理强度、交互模式、上下文用量和工作区只在底栏集中展示。
新会话的 transcript 保持为空，不再重复显示 Ready、模型和路径；恢复会话只在
transcript 保留会话身份提示。品牌不随草稿增高而移动。

输入正文与底栏元数据共用阅读轴；包括紧凑布局在内，Header 与该轴至多错开一格。
长回答和工具活动在 transcript 内滚动，不撑大外壳；权限模态层覆盖外壳，不重新
分配其行数。切换主题只改变 palette，不改变这些几何规则。

### Composer 与键盘

Composer 由有界的输入表面和底部单行运行状态栏组成。空输入表面
在普通宽度占 2 行，在紧凑布局占 1 行，并随可见草稿行数增长。输入编辑器同时受
现有 8 行上限和终端高度四分之一约束，矮终端至少保留 2 行预算；更长草稿在编辑器
内部滚动。短草稿释放的空间全部交给主阅读区，空会话仍从顶部阅读轴开始。状态栏
固定在底部，保留原有数据和提示；附件、运行中状态及命令提示在出现时保留自身行。

默认 `Enter` 提交草稿。发送按钮与编辑器共用一行；不再有专属操作/提示行或
换行按钮。Ctrl+J / F2 备用键移至 F1 / `/help`。用户自行选择的 Enter 换行模式保留。

Textual 1.x 负责终端输入、POSIX 的 Kitty 消歧启用 (`CSI >1u`)、CSI-u `13;2u`
到 `shift+enter` 的规范化，以及粘贴、编辑和退出恢复。`TerminalKeyboardCapability`
只记录真正收到的修饰 Enter；启用请求、终端名称、环境变量都不代表支持。
不增加输入读取器、协议解析器或启动等待。独立 `shift+enter` 在选区插入换行；
旧 CR 和 SS3 keypad Enter 保持发送，Ctrl+J / F2 保持换行。

已审计 Konsole 25.12.3 的默认 keytab：Shift+Return 输出 SS3 `ESC O M`，Textual
将其解析为 keypad Enter。这不是可靠的 Shift 编码，不能全局改为换行而破坏数字
小键盘 Enter。该版本 VT 模拟器没有 Kitty 键盘协商。用户可在终端按键配置中显式
把 Shift+Return 映射为 `\E[13;2u`，或使用支持增强上报的版本/链路；Neuro 不修改配置。
Kitty / Ghostty 可上报 CSI-u；WezTerm 需启用 Kitty 协议选项。Windows Terminal
取决于版本，而 Textual 1.x Windows 驱动不启用 Kitty 协商；已送达的独立事件能处理，
未确认链路保持备用键。Help 显示已观察/未确认能力，不根据品牌声称普遍支持。
终端快捷键和 multiplexer 也可能改变输入链路，因此仍需实机验收。

来源：[Konsole 25.12.3 默认 keytab](https://github.com/KDE/konsole/blob/v25.12.3/data/keyboard-layouts/default.keytab)、
[Kitty 协议](https://sw.kovidgoyal.net/kitty/keyboard-protocol/)、
[WezTerm 选项](https://wezterm.org/config/lua/config/enable_kitty_keyboard.html)。

本决策仅改变外壳几何和输入提示。V1A 的颜色与自适应 System 表面、V1B 的排版、
权限行为、运行状态值和持久会话历史仍由现有实现负责。

## 验证

确定性 Textual 截图覆盖 Graphite、Porcelain、System 在 120×40、100×32、80×24
的界面，包含聚焦/空闲单行输入与长草稿。几何测试约束主阅读区占比、阅读轴、
底部状态栏、模态层、主题切换和草稿的增长/收缩；键盘测试区分
换行与发送。终端按键上报不受 Textual 控制，因此真实终端仍需人工验证
`Shift+Enter`。已提交的 V1B 快照作为 V1C 对比画廊的改造前基线。
