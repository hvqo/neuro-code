# ADR 0191: Measured Ink 流式文字呈现

**简体中文** · [English](../../en/adr/0191-measured-ink-streaming-presentation.md)

- 日期：2026-10-03
- 状态：已接受；用户已选定固定 Production 参数
- 范围：仅 Assistant TUI 呈现

## 决策

仅采用探索 D：Measured Ink，20fps / 180ms / 最多十二个最近正文 grapheme。
Provider delta 整块立即更新 canonical source，不设逐字队列，不改变 Runtime event，
不新增光标或 marker，不修改 Agent pulse。开关两种路径均以独立的 25ms（40Hz）预算
合并 view commit，不受 50ms 动画 clock 限制。这是调度预算，不是显示速率保证；source
停顿、解析/渲染成本与终端 backpressure 仍会影响 frame pacing。
动画跟随 arrival，不控制内容接收。Appearance → Input 新增可继承的布尔配置
`text_arrival_animation`，缺省开启；旧 version-1 preference 仍能读取，不暴露高级参数。

## 来源映射与 Unicode

`text_arrival.py` 只接受能证明来源的顶层纯正文段落：Markdown-it 的 source lines、
inline content、text/softbreak children 与 Rich Text 必须精确一致。不搜索子串、不猜索引，
不改写 response。带格式段落、heading、link、escape/entity、inline/fenced code、Syntax、
Diff、list、quote、table 均静态呈现。整个格式段落静态降级，优先保护语义；自定义非正文
语义样式同样不启用动画。短暂前景叠加不修改背景与 geometry，结束时严格恢复 canonical。

使用 [regex](https://pypi.org/project/regex/) 的 Unicode UAX #29 `\X` 分段，替换探索 helper。
source code-point 索引与 terminal cell 分离，完整安全 Latin/Han grapheme 才携带 metadata；
换行和 cell width 仍由 Rich/Textual 处理。emoji、ZWJ、flags 与复杂 shaping 静态降级。
每个段落只标记末尾十二个候选 glyph，并限制到 source 最后 384 个 code point；实际叠加
全局最多十二个 glyph。可证明 glyph 首次呈现时固定 birth，追加 combining mark 不会
重启动画；接收时间仅用于诊断，见 [ADR 0194](0194-measured-ink-presentation-lifecycle.md)。

## 缓存与刷新所有权

`AssistantMessage` 持有有界 arrival timeline、50ms animation clock、一个可取消的 asyncio
one-shot view deadline 和当前 Markdown view。deadline 到期后进入 widget message pump；
迟到也必须执行。Textual 1.x one-shot 默认跳过迟到 callback，不能用于保证最终正文提交；
generation guard 同时失效已入队的旧 callback。截止时间相对上次 commit，新 delta 不会不断推迟截止时间，
animation tick 不 flush pending content。committed growth 只保留一个 post-layout scroll，
连续布局之间保留末尾跟随意图；向上阅读历史或替换 transcript 时取消。现有固定单行状态槽
更新文字只 repaint，不要求 layout；显隐与真实 geometry 变化仍由 Textual 正常处理布局。
Agent pulse 的内容与节奏不变。
每个 `AssistantMarkdown` 内容不可变，content commit 创建新实例并解析一次。单项 line cache
的 key 包含 width、compact policy、输出颜色能力、base style、resolved syntax theme 和
Markdown semantic styles。主题、Syntax、width、viewport 变化失效或替换当前 view。
动画 age 不在 cache key 中。animation-only tick 不创建 Markdown、不解析 source，只叠加
缓存 source-tagged strips，并对受影响行请求局部 refresh，不请求 layout。Textual 仍负责
屏幕合成，局部 dirty region 不意味着 compositor 成本为零。

完成、取消、错误、discard、restore、新 response、view 隐藏、unmount 停止两种 timer、
失效旧 callback 并清理 arrival。resize/reflow、主题或 Syntax 刷新只失效呈现 geometry/style，
保留 source 生命周期。首次呈现启动 clock，最后 presented glyph 到期即停止；有界
pending/settled identity 不轮询。[ADR 0194](0194-measured-ink-presentation-lifecycle.md)
替代原有接收时间过期规则。Headless、NO_COLOR、ANSI16/unknown 静态降级。关闭动画仍复用
合并刷新和缓存，不创建 arrival ranges。

## 验证与限制

确定性回归覆盖整块 delta、cache 等价、source proof、Unicode、受保护 Markdown、自定义
语义样式、glyph 到期、局部 tick、过期 deadline 必达、显式 commit/layout 屏障
（message idle 不等于 timer 完成）、主题/resize、完成/取消/错误/restore、配置继承。
保留的测试专用 replay 通过完整 controller/event 路径，在固定 Production cadence 下
比较 animation off/on，记录 commit/可见更新分布、exclusive CPU、Markdown/Syntax、
layout/scroll、compositor 与 refresh 次数。synthetic stress tape 显式标记；可选 JSONL
录制保留 `at_ms` 与完整 `text` delta。Runtime event 时间不是 Provider socket 接收时间。
headless serialization 不是终端输出；`native_pacing` 测原生 queue 与 writer thread 的实际
write/flush；`pty_pacing` 使用控制 POSIX PTY 与快速 reader，并不模拟 Konsole。
探针仅存在于显式测试工具中，通过有界 hook 使用，Production 不加载。
重复的早期 replay 与多 cadence exploration 入口已移除。

### 为什么选择 25ms / 40Hz

此前共享 50ms clock 将正文 commit 限制为约 20Hz。选定 25ms 预算提高常见可见更新频率，
同时保留 Measured Ink 的 20fps。一段中英/CJK 录制（629 个完整 delta，约 13.9s）在原生
terminal driver 回放中的数据为：

| 正文预算 | Commit P50 / P95 / P99 / 最大（ms） | CPU 秒 |
|---|---|---:|
| 原 50ms | 50.5 / 51.8 / 60.7 / 69.6 | 2.61 |
| 选定 25ms | 25.5 / 28.2 / 58.6 / 70.9 | 4.03 |

25ms 的尾部可见更新为 25.6 / 40.7 / 57.5 / 81.1ms。CPU 增加约 54%（回放期间约占
一个核心 28%），这是明确的响应性/CPU 取舍。更高测试频率收益递减。
测量含探针成本，不包含 Konsole paint/font/GPU；不代表 Provider 收益或保证肉眼连续。

已知瓶颈：每次 content commit 仍解析完整 Markdown，长代码仍全文 Syntax render。
合成大代码块回放曾观测到 67.5ms Syntax render 与约 80ms inclusive layout；部分 parse
长尾与 18～20ms GC 重合。更快调度不能单独解决这些长帧。
animation tick 引起的 Markdown parse 为零；arrival 到期后文字 timer 停止，测量 idle
窗口的 assistant refresh/layout/parse 为零。既有 Agent pulse 在轮次结束前仍可正常运行。

```bash
uv run pytest tests/test_tui_text_arrival.py tests/test_tui_frame_pacing.py -q
uv run python -m tests.visual.text_arrival.frame_pacing --output /tmp/frame-pacing.json
uv run python -m tests.visual.text_arrival.native_pacing --theme system --case long-cjk
# 在 Konsole 中按 Enter 开始；--off 只关闭 arrival 叠加。
# --recording /path/to/deltas.jsonl 保留录制的 delta 边界与时间。
uv run python -m tests.visual.text_arrival.pty_pacing --output /tmp/native-pacing
```

Konsole 观感、字体/CJK shaping、远端终端 repaint 成本仍需人工验收。长回答每次 view commit
仍解析完整 Markdown，并非增量 parser。探索 A/B/C 与全局 render monkeypatch 不进入 production。
不预期修改 settled snapshot 或 UI palette。
