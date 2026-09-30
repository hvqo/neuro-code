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
到 `shift+enter` 的规范化，以及粘贴、编辑和退出恢复。`TerminalInputNormalizer`
按固定顺序处理输入：原生修饰键事件、`TerminalInputCompatibilityRegistry` 中按终端
身份/版本限定的规则、最后是 Ctrl+J / F2 备用键。归一化器始终返回一个动作：`SEND`、
`NEWLINE` 或 `PASS_THROUGH`；最后一种让 Textual 继续处理普通文本输入和编辑。
其他 Enter 事件保持既有发送语义。
`TerminalKeyboardCapability` 仍只记录实际观察到的原生修饰 Enter，不根据终端品牌推断。
`KONSOLE_VERSION` 只用于识别这条有版本范围的兼容规则，不代表终端普遍支持增强键盘上报。

Konsole 25.12.x 是已验证的兼容规则。Konsole 25.12.3 默认 keytab 将 Shift+Return
编码为 SS3 `ESC O M`。Textual 将其规范化为 `key="enter", character=None`；普通
Return 则为 `key="enter", character="\\r"`。兼容注册表只在已验证的 Konsole
25.12 版本范围内，将无字符的 Enter 或 keypad-enter 事件映射为换行，普通 Return
仍发送。Konsole 会向其子 Session 注入专用数字环境标记 `KONSOLE_VERSION`；解析器只接受
25.12.0 至 25.12.x 的严格六位编码，不从 `$TERM` 推断。标记缺失/格式错误、已知
multiplexer 或 SSH 环境一律失败关闭。以后可以只向注册表增加终端规则，无需在
`PromptInput` 中加入终端名称分支。

每条注册规则都是可审查的数据，记录终端系列、含下限/不含上限的版本范围、Textual
观察到的 key/character、对应的原始 wire 序列、结果动作、已知取舍、证据链接和回归测试
引用。原始序列仅作为规则证据；运行时仍由 Textual 唯一解析，归一化器只匹配其规范化事件。

SS3 序列不包含物理按键是否为 Shift+Return 的信息。因此对匹配的 Konsole 版本，
物理小键盘 Enter 也会插入换行；应用无法区分这两者。Help 会说明这一代价。本规则
不修改终端配置或 keytab。原生 CSI-u `shift+enter` 优先；未匹配的终端仍由普通
Enter 发送，Ctrl+J / F2 仅作为 Help 中的备用键。仍需在真实 Konsole 验收。

来源：[Konsole 版本环境变量导出](https://github.com/KDE/konsole/blob/v25.12.3/src/session/SessionManager.cpp#L1254-L1286)、
[Konsole 25.12.3 默认 keytab](https://github.com/KDE/konsole/blob/v25.12.3/data/keyboard-layouts/default.keytab)、
[Textual 1.0.0 输入解析器](https://github.com/Textualize/textual/blob/v1.0.0/src/textual/_xterm_parser.py)、
[Kitty 协议](https://sw.kovidgoyal.net/kitty/keyboard-protocol/)。

### 应用侧协商可行性

2026-09-30 审计使用本机安装的 Textual **1.0.0** 与 Konsole **25.12.3**。
Textual POSIX `LinuxDriver.start_application_mode()` 已在启动输入线程前发送
Kitty 消歧 push (`CSI >1u`)，退出 alternate screen 前发送 pop (`CSI <u`)。
焦点事件本身不是键盘能力响应，每次聚焦重新 push 会使协议栈不平衡。因此复用
驱动生命周期，不新增竞争输入读取器，不额外启用其他协议模式。

为核验真实 Konsole 路径，offscreen Qt harness 调用了本机
`libkonsoleprivate.so.25.12.3` 的 `Vt102Emulation`：设置 UTF-8、重置终端状态、
使用默认按键翻译器，将应用输出送入 `receiveData()`，将 Qt Return/Shift+Return
送入 `sendKeyEvent()`，捕获 `sendData()`。这验证真实模拟器与按键翻译器；
不是 GUI/物理按键验收，也不是 fake transport。

| 探测 | 实际响应 / 按键输出 |
| --- | --- |
| Device attributes `CSI c`（正向对照） | `CSI ?62;1;4c` |
| 启用 focus reporting 后获得焦点（正向对照） | `CSI I` |
| Kitty 查询 `CSI ?u`，在 `CSI >1u` 前后 | 均无响应 |
| modifyOtherKeys 查询 `CSI ?4m`，在 `CSI >4;2m` 前后 | 均无响应 |
| Return，在任一启用请求前后 | CR (`0d`) |
| Shift+Return，在任一请求或聚焦前后 | SS3 `ESC O M` (`1b4f4d`) |

对应的 [VT 模拟器源码](https://github.com/KDE/konsole/blob/v25.12.3/src/Vt102Emulation.cpp)
没有 Kitty 键盘 flags 或 modifyOtherKeys 处理分支，按键仍由 keytab 翻译。
[modifyOtherKeys 是 xterm 协议](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html)，
不是所有终端的通用能力。这些请求无法在该版 Konsole 恢复缺失的 Shift 信息。
结论限定于已验证版本，不根据 `$TERM` 推断，也不声称未来 Konsole 版本不支持。

终端输入兼容层不新增终端输入读取器、协议协商请求、启动等待、聚焦探测、配置修改
或 IME/粘贴拦截。当前 Windows 驱动不协商 Kitty；终端或 multiplexer 声称支持，
不等于当前驱动/解析器实际送达独立事件。带 release events、alternate keys、
associated text 的额外 Kitty flags 超出当前解析器契约，不试探性开启。

本决策仅改变外壳几何和输入提示。V1A 的颜色与自适应 System 表面、V1B 的排版、
权限行为、运行状态值和持久会话历史仍由现有实现负责。

## 验证

确定性 Textual 截图覆盖 Graphite、Porcelain、System 在 120×40、100×32、80×24
的界面，包含聚焦/空闲单行输入与长草稿。几何测试约束主阅读区占比、阅读轴、
底部状态栏、模态层、主题切换和草稿的增长/收缩。归一化器和 PromptInput 测试覆盖
普通 Enter、增强 Shift+Enter、Konsole 精确版本范围、未知终端 fallback、注册表扩展及
Ctrl+J/F2。真实驱动 PTY 回归覆盖协议 push/pop 与退出恢复、Konsole SS3 归一化后再由
普通 Enter 提交、原生修饰 Enter，以及 bracketed 多行中文粘贴。人工 Konsole 验收还需
确认物理小键盘 Enter 的取舍，并复核 IME、历史、选区和布局行为。
