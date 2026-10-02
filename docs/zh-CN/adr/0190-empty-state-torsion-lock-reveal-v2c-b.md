# ADR 0190：Empty State Torsion Lock Reveal V2C-B

**简体中文** · [English](../../en/adr/0190-empty-state-torsion-lock-reveal-v2c-b.md)

- 日期：2026-10-02
- 状态：已接受；Draft PR 等待真实终端视觉验收
- 范围：仅 TUI presentation
- 前置：[ADR 0189](0189-static-empty-state-identity-v2c-a.md)

## 决策

采用已选择的 **Variant E — Torsion Lock**，作为用户主动触发的可选品牌动效。
保持 V2C-A 原始静态 core mark 的精确几何；按用户实机反馈降低默认对比度：
既有 `text-dim` 使用 28% foreground alpha 并叠加 `dim`，已知 RGB 时与现有 canvas 混合。
未知 ANSI default 保留终端 `dim`，不伪造 RGB。
不启动播放、不循环，不承担 loading 或 Agent 状态含义。
只有在 Logo widget 内左键点击才开始一次播放；播放期间重复点击忽略。
widget 不取得键盘焦点，点击通过消息请求现有 Composer 恢复焦点，不改变编辑器或输入协议。

`EmptyStateIdentity` 负责 canonical resting content、选帧和唯一的 one-shot timer。
`empty_state_reveal.py` 保存三个尺寸的不可变预计算 Braille 姿态。
运行时不读取 PNG、不转换图片。来源仍以 `empty_state_logo.py` 中 SHA256 为准。
原图的连通轮廓按角度归为六个运动分区，不切割轮廓，不重设计已验收的静态标志。
A–D 方案不进入 runtime。

## 几何、样式与时序

每帧保持现有固定 bounding box：large 32×16、medium 24×12、small 16×8。
首尾精确复用 V2C-A canonical rows。
预紧、开合、有限扭转、快速咬合、轻微回弹和归位产生结构变化，亮度仅辅助。
样式通过当前 UI semantic components 解析：`text-dim` + `dim`、`text-muted`、
`text-secondary`。不引入独立 palette、RGB 常量、accent 闪光、插值或连续旋转。

原 720ms exploration 在实机审查中太短；按用户要求延长到 6 秒，保持原 12 个姿态与
非等时序，只延长停留，不增加循环或额外旋转。各帧停留时长（毫秒）：

```text
400 / 350 / 350 / 450 / 600 / 500 / 400 / 350 / 400 / 800 / 600 / 800 = 6000 ms
```

按 monotonic elapsed clock 选择当前帧并安排下一边界。
事件循环延迟时可跳过已过时帧，不累计时序漂移。
逐帧只对本 widget 执行 `layout=False` repaint；不使用会使布局失效的 `Static.update()`。
Textual 仍负责 compositor 和终端输出，这不表示终端完全无需 screen composition 工作。

## 取消与能力降级

首条真实内容、transcript reset/resume/restore、隐藏、空间不足、尺寸类别变化、主题切换、
unmount 或 shutdown 都取消并释放 one-shot timer，恢复 canonical resting content 或隐藏。
callback 带 activation generation，已排队的旧 callback 不能推进后一次激活。
同尺寸重新定位保留进度；large/medium/small 变化时取消，不跨尺寸映射动画 phase。
Resting 没有 timer，不建立 worker 或 polling interval。

`NO_COLOR`、deterministic headless/snapshot、ANSI16 和未知输出能力保持静态。
复用 Textual/Rich 现有 color capability 或既有可信 terminal palette，允许 TrueColor/ANSI256 动画。
不增加 terminal probe，不按终端名称猜测。静态 Logo 点击仍可恢复 Composer 焦点。
Agent pulse 未修改；真实内容出现即移除 Logo，避免与 Agent 动效并存。

## 验证与人工审查

回归覆盖完整播放、精确归位、点击/重复点击、旧 callback、取消、焦点、中文/paste/Enter、
尺寸/主题切换、能力降级、timer 清理和不变的 shell geometry。
六个视觉状态覆盖 Graphite/Porcelain/System × 120×40/100×32/80×24，共 54 张基线。
仅测试 harness 冻结 production clock。

```bash
uv run python -m tests.visual.empty_reveal.render_preview --output /tmp/neuro-reveal.html
uv run python -m tests.visual.empty_reveal.render_preview --terminal --theme system
```

HTML player 使用真实 production Textual 帧截图，用于观察结构，不证明真实终端时序或 CPU 成本。
Repeat/speed 仅属于该 player。无 Provider 的 native preview 使用真实 widget 和普通键鼠输入。
Konsole 仍需验收 Braille 字体对齐、6000ms 节奏、低亮 Resting/Peak、重复点击、播放期间输入与
多行粘贴、resize、首条消息取消。System snapshot 是确定性 ANSI 表现，不代表所有终端 palette。
本决策不包含 Streaming Presence/Motion。
