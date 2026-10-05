# ADR 0192：消息私有的闭合围栏渲染缓存

**简体中文** · [English](../../en/adr/0192-message-local-closed-fence-render-cache.md)

- 日期：2026-10-03
- 状态：已接受
- 范围：仅 Assistant fenced-code presentation

## 决策

保留完整 canonical source、全文 MarkdownIt 解析和 Rich/Pygments 渲染。
每条 AssistantMessage 持有自己的 FenceRenderCache，跨 immutable Markdown revision
复用结果。只有权威 token map 与原文能够证明顶层 fence 有显式 closing marker 时，
才缓存原 Rich Segments。未闭合、缩进代码、嵌套或父容器自动闭合的代码仍使用原渲染器，
不重新实现容器去缩进，也不引入增量解析器。

Key 包含完整代码、归一化 lexer/language、已解析 Syntax Theme 选择及 surface/foreground、
影响输出的 ConsoleOptions（包括宽度、高度约束、wrap、encoding、legacy mode）、
color capability 和解析后的 code styles。未知可变自定义 theme 对象直接绕过缓存。
代码、语言、宽度、主题或样式变化会 miss；回到相同 presentation 可复用旧 entry。
animation age、stream revision、source position 和后续普通正文不属于 key。
同一消息内的相同围栏可以共享结果。

LRU 最多保留 32 entries、每消息估算 8 MiB，计入 source、Segments、文本及 style 开销。
估算不是精确 RSS 保证；过大结果照常渲染但不保留。Eviction 不影响正确性。
Unmount/disposal 清空 entries；conversation replacement 删除所属 widget。
不新增全局结果缓存、parser replacement、后台 worker 或 timer。既有 current-view cache
保持独立。正文仍为 25ms/40Hz；当前 Measured Ink 采用 A22 Materialize，24fps/160ms/最多
8 个活跃 glyph（ADR 0195）；滚动与 Runtime 不变。

## 验证与限制

测试比较 canonical Segments 和 syntax styles，覆盖闭合证明、code/language/theme/width/
style/capability miss、有界 LRU、重复及多个围栏、Diff、disposal、resize、animation on/off。
仅测试使用的 replay 对比原 Rich fence renderer 与 production 的同一完整 delta tape；
fixed-work pass 将每 revision 工作减少与实际 commit 吞吐增加区分。
Instrumentation 只存在于显式 benchmark，不被 production 导入。

```bash
uv run pytest tests/test_tui_fence_cache.py -q
uv run python -m tests.visual.markdown_render.benchmark --output /tmp/fence-replay.json
uv run python -m tests.visual.markdown_render.benchmark --fixed-work --output /tmp/fence-fixed.json
```

冷启动大围栏、仍在增长的 open code、全文 Markdown parse，以及整条消息的 wrap/layout/
compositor 成本仍存在。Snapshot 输出应保持一致；synthetic headless timing 不是实际
Provider 或 Konsole latency。不发布 stable-prefix block cache、inline token memo、
A/B 探索模式或增量 Markdown parser。
