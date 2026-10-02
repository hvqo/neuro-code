# ADR 0190：Empty State Vortex Reveal V2C-B

**简体中文** · [English](../../en/adr/0190-empty-state-torsion-lock-reveal-v2c-b.md)

- 日期：2026-10-02
- 状态：已接受实现；Draft PR 等待真实终端视觉验收
- 范围：仅 TUI presentation
- 前置：[ADR 0189](0189-static-empty-state-identity-v2c-a.md)

## 决策与设计修正

旧 Torsion Lock 只有 12 个姿态，延长到 6 秒后每帧停留 350–800ms，实机表现为卡顿。
按用户提供的 `neuro_vortex.py` 运动参考，替换为连续采样的分层反向扭转、三维倾斜 / 透视、
短拖尾与一次外扩冲击。亮度辅助结构运动，不将旧帧单纯延时。保持已验收的 V2C-A 六片
静态 core mark，不使用参考文件的替代 Logo、RGB palette 或终端控制循环。
参考文件 SHA256：`9e2850264ecff7a3b5c90323e2cfeb8959eeea4d1de0367e66c7d8ed6dd49de4`。

默认 Resting 沿用低亮 `text-dim` 28% alpha + `dim`。已知 RGB 与 canvas 混合；未知 ANSI
保留终端 dim，不伪造 RGB。不自动播放、不循环，不承担 loading / Agent 状态。
仅 Logo 内左键点击触发；播放中再次点击忽略。widget 不获取键盘焦点，点击消息请求
现有 Composer 恢复焦点，不修改输入协议、IME、paste 或历史。

## 数据、几何、样式与时序

`EmptyStateIdentity` 负责 canonical resting content、选帧、语义样式和单个 one-shot timer。
`scripts/generate_empty_vortex.py` 离线从 canonical Braille dots 生成轨迹。
`empty_state_vortex.json` 保存固定帧数据，`empty_state_reveal.py` 加载为不可变 rows / intensity。
运行时不读取下载目录、PNG，不计算粒子投影，不执行参考脚本。

- 固定 bounding box：large 32×16、medium 24×12、small 16×8；布局不变。
- 24fps、6000ms：144 个 41/42ms 区间，145 个采样包含首尾；末帧不额外停留。
- 首尾 rows 精确等于 production canonical Resting，强度为 0，结束恢复静态 render。
- 外 / 中 / 内层有限反向角位移；倾斜和透视体现结构变化。短拖尾、一次外扩冲击、稀疏
  确定性数据字符只辅助运动；没有持续 spinner、随机 glitch 或长期动画。
- 每 cell 的 16 级强度为主题中立数据。已知 RGB 在既有 resting / secondary semantic colors
  之间插值；未知 ANSI 使用现有 neutral foreground + dim。无专用 palette 或硬编码 RGB。
- 样式只在激活时解析并缓存；按相同 style 连续字符合并 Rich spans。

按 monotonic elapsed clock 二分选择当前采样、安排下一个边界。延迟时跳过过期帧，不积压
catch-up，也不累计漂移。逐帧只对 Logo `refresh(layout=False)`，不 invalidate 全局布局。
Textual 负责 compositor / terminal output；不声称完全没有 screen composition 成本。

## 取消与能力降级

首条内容、reset/resume/restore、隐藏、空间不足、尺寸类别变化、主题切换、unmount / shutdown
取消并释放 timer、丢弃样式缓存，恢复 canonical Resting 或隐藏。generation fencing 防止
已排队旧 callback 影响后一次激活。同尺寸定位保留进度；size class 改变不映射动画 phase。
Resting 无 timer、worker 或 polling interval。

`NO_COLOR`、headless/snapshot、ANSI16 和未知输出能力保持静态。沿用 Textual/Rich 输出能力或
既有可信 terminal palette，TrueColor/ANSI256 可播放。不新增 probe，不按 terminal 名称猜测。
静态点击仍恢复 Composer 焦点。首条消息隐藏 Logo，Agent pulse 未修改。

## 验证与人工审查

回归覆盖完整播放、精确首尾、真实轨迹、24fps cadence、延迟跳帧、重复点击、取消、焦点、
中文/paste/Enter、size/theme、fallback、timer 清理和不变 shell geometry。
六状态 Resting / lift / max-tilt / reconstruction / settle / returned-resting，覆盖三主题 ×
三视口，共 54 张基线。测试 harness 冻结 clock，production 不读取测试状态。

```bash
uv run python scripts/generate_empty_vortex.py
uv run python -m tests.visual.empty_reveal.render_preview --output /tmp/neuro-reveal.html
uv run python -m tests.visual.empty_reveal.render_preview --terminal --theme system
```

HTML player 使用真实 Textual shell、production 帧数据 / 解析样式 / cell geometry 局部播放，
关键帧为完整 Textual 截图，避免为每采样解码全屏图片。Repeat/speed 仅属于预览。
native preview 使用真实 widget / timer / 键鼠。POSIX PTY 可验证调度与输出，不能替代 Konsole。
仍需人工验收连续透视运动、六秒节奏、Braille 字体、低亮首尾、输入、resize、取消。
System snapshot 是确定性 ANSI 表现，不代表所有 terminal palette。本决策不包含 Streaming Motion。
