# ADR 0194: Measured Ink 呈现生命周期

**简体中文** · [English](../../en/adr/0194-measured-ink-presentation-lifecycle.md)

- 日期：2026-10-05
- 状态：已接受实现；等待实机视觉验收
- 范围：仅呈现生命周期；替代 ADR 0191 的接收时间过期与 resize 取消规则

## 证据与决策

视觉信号审计复现了两个问题：scrollbar 改变 message width 而终端尺寸未变，
`on_resize → stop_arrival` 提前丢弃未呈现 arrival；cold render/layout 耗时
286～420ms，超过探索的 280ms receive-based 寿命。这是生命周期缺陷，不是配色问题。

保留既有 Production 效果：20fps / 180ms / 十二 glyph，正文独立 25ms commit，
canonical delta 整块立即接收。不增加 Variant、palette、每字 timer、lexer 或围栏行为。

## Source 生命周期

`Arrival.received_at` 记录接收顺序与诊断；`ArrivalTimeline.births[start]` 记录首次
**presented_at**。状态从现有数据推导，不建立第二套状态存储：

- RECEIVED：range 已注册，没有 presentation birth，也不启动动画 clock。
- PRESENTED：可证明 source 且完整的 glyph 首次进入 `render_lines(crop)` 的可见
  strip crop。`start_visual(source, now)` 只出生一次，age 为 `now - presented_at`，
  绝不按 received_at 计算。
- SETTLED：presented age 到期后恢复 canonical 并停止共享 clock；有界 birth
  tombstone 保证 rebuild、scroll/repaint、主题刷新、追加 combining mark 不会重启动效。

crop 是 Textual 的呈现边界，不是 GPU 实际显示确认。被裁为 padding 的半个宽字符不算
首次呈现。枚举 cached sources 不启动 birth；未展示 glyph 保持 pending，曾呈现过的
glyph 即使移到屏幕外也继续按原 birth 计时。

pending provenance 以最多 256 个 range、末尾 384 个 source code point 限界，
不能按 animation duration 提前删除。源码推进或数量上限可淘汰旧 pending，避免无限积压。
births 使用同一 source window。真实 message generation 边界、complete/cancel/error/
discard、隐藏、restore、unmount 清理 timeline。动画关闭与低能力终端仍静态降级。

## Geometry 与 style 所有权

内部 width change 与真正 app/terminal resize 都只失效 line layout 和 dirty row
identity；保留 pending ranges、既有 births 和唯一 clock，下一次可见 render 重建
source→row。无需根据终端名称或猜测 widget width 变化的原因。content commit 同样
丢弃 row geometry，但保留 source 生命周期。

同一 source generation 的 UI/Syntax Theme 与语言刷新使用 `restyle_stream`；
style/cache key 重新解析，birth 不变。最终 response/restore 使用 canonical `update`
清理 streaming 状态。主题变化不让 settled text 重启动画。FenceRenderCache 与围栏
presentation 不变，Syntax 继续按原有 cache key 正常失效。

## 验证与回放

回归覆盖 0/50/150/300/400/500ms stall、280ms duration 下的 400ms stall、终端
尺寸不变时 156→155→156 width 事件、真实 scrollbar overflow、
120×40→100×32→80×24→120×40、完整/裁剪 CJK、不可见 source、主题刷新、
combining mark、generation reset、有界状态和 idle 文字零工作。

显式测试工具采用 Production mapping/lifecycle，使用完整 synthetic delta 的 540 行
围栏 + 正文 tape。Sentinel 只在诊断时使用 500ms reverse+bold，仍遵守 Production 的
十二 glyph 上限，不进入设置或生产 Variant。

```bash
uv run python -m tests.visual.text_arrival.lifecycle_replay --sentinel --theme system
uv run python -m tests.visual.text_arrival.lifecycle_replay --off --theme system
uv run python -m tests.visual.text_arrival.lifecycle_replay --headless --output /tmp/ink-lifecycle.json
```

Enter 播放，Ctrl+C 退出；`--recording /path/deltas.jsonl` 保留完整 delta 和 timing。
metrics/frames 保存到 repo 外。Native 回放观察现有 writer 的实际 file write/flush，
headless CPU/serialization 不等于 Konsole 肉眼观感。tick-only parse/layout 为零，
settled assistant refresh/parse/timer 为零；独立的既有 Agent pulse 仍可能继续。
修复丢失的动画会增加应有的局部 repaint，不是免费 CPU 优化，也不解决颜色曲线过弱。
完整 Markdown parse 与全部 canonical style 保持不变。
