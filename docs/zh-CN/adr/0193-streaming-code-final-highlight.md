# ADR 0193：稳定的流式代码与一次最终高亮切换

**简体中文** · [English](../../en/adr/0193-streaming-code-final-highlight.md)

- 日期：2026-10-04
- 状态：已接受；等待 Konsole 验收
- 范围：Assistant fenced-code 呈现

## 问题与证据

Main 每次 view commit 都重新词法解析增长中的代码。未闭合字符串和注释可能在后续字符
到达后获得不同 token 分类，使已有字符重新着色。此前本地实验还将 EOF 的 closing candidate
误认为稳定闭合：`code\n` → 追加三个 backtick → 再追加 `oops`，出现
PLAIN → SYNTAX → PLAIN。MarkdownIt 修订 EOF 解释是正确行为；错误是呈现层提前把
暂定解析结果当作最终状态。

## 决策

每条 AssistantMessage 在既有 `FenceRenderCache` 旁持有一份 `FencePresentation` 元数据。
Identity 使用消息 generation 和 opener 在归一化源码中的位置。Parser object ID 只绑定
当前一次 parse，不作为跨 revision 身份。Canonical source 被替换时开启新 generation；
恢复的历史消息从 complete 开始，销毁时清空这两份消息私有状态。

对于 parser 映射、源码可以确认的顶层围栏：

```text
ACTIVE（稳定普通代码）→ FINALIZED（完整语法高亮）→ CACHED（相同语法模式）
```

Streaming 期间，显式 closing line 必须已经换行定型才能最终化。没有 newline 的 EOF
closing candidate 继续 ACTIVE，因为下一段 delta 仍可能使它失效。Response complete
可以最终化 EOF 围栏，包括未闭合围栏；不补围栏、不修改模型文本。每个 identity 只最终化
一次，状态单向推进。Cache miss、淘汰、主题和宽度变化不会重置生命周期。每次 parse
安装前固定 foreground 决策，构建新 view 时旧 view 不会提前改变 foreground。

ACTIVE 使用既有 Rich Syntax 表面、padding、wrapping 和主题普通 Text 样式，绕过
Pygments。最终代码使用正常 Rich/Pygments renderer。嵌套、缩进、无映射或无法确认的
结构继续原渲染链。Markdown 全文解析仍为权威。不新增 parser、渲染结果缓存、timer、
Runtime event、动画算法或 streaming pacing 参数。

现有消息私有 LRU 仍保留 32 entries / 估算 8 MiB。Render key 保留代码、语言、Syntax
Theme、宽度、样式和颜色能力；不包含 height/max-height：结果是裁切前的 segments，
measure/paint 不同的高度预算不应造成重复语法工作。正常淘汰和渲染条件变化可以触发重新
渲染，但不能让最终化代码退回 plain。

## 验证与终端验收

测试逐段回放 Python、Rust、TypeScript、JSON、shell、Diff，包含多行字符串/注释、
未闭合字符串、EOF closing candidate 失效、多围栏与正文尾部。断言 ACTIVE foreground
稳定、只最终化一次、最终结果等价于静态 Rich/Pygments、文本/背景/cell/高度不变，覆盖
生命周期隔离、resize/主题/Syntax Theme、淘汰以及 cancel/error 清理。专门的 benchmark
使用相同合成 tape 对比 main renderer；headless CPU/commit 数据不代表真实 Konsole
scanout 性能。

在 Konsole 运行真正的 Neuro Code TUI；Enter 开始，检查完成后 Ctrl+C 退出：

```bash
uv run python -m tests.visual.code_flicker.replay --mode stable --speed slow --theme system
uv run python -m tests.visual.code_flicker.replay --mode stable --speed normal --theme system
uv run python -m tests.visual.code_flicker.replay --mode baseline --speed normal --theme system
uv run python -m tests.visual.code_flicker.benchmark --output /tmp/neuro-code-flicker-benchmark.json
```

终端回放将每次 view commit 生命周期记录到 `/tmp/neuro-code-fence-replay.json`。
200 行 Python 围栏增长期间保持 plain，闭合时一次 foreground 切换，后续 Markdown 到达
期间保持 syntax。Main 与 stable 模式采用相同 delta timing。缺少 newline 的 EOF closing
candidate 可以延后到 response complete 才高亮。最终高亮仍可能产生一次冷渲染长帧；
全文 Markdown 和 layout 成本仍存在。
