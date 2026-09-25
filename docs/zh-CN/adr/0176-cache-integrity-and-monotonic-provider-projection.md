# ADR 0176：缓存完整性与单调 Provider 投影

**简体中文** · [English](../../en/adr/0176-cache-integrity-and-monotonic-provider-projection.md)

- 状态：已接受
- 日期：2026-09-25
- 范围：Provider 请求诊断、DeepSeek 历史回放、合成上下文投影、cache epoch 与缓存用量指标

## 背景

Prompt Cache 受 Provider adapter 最终生成的请求影响，而不只取决于 `ModelContext`。重新生成旧指令、Working Set、
运行时通知或供应商专属历史，都可能使可复用前缀失效。系统还需要区分结构上的连续性与 Provider 远端报告的缓存命中。
缓存命中率本身不是质量目标：总上下文、未缓存输入、延迟、模型调用数和正确性同样重要。

## 决策

### 最终请求轨迹

用户显式设置 `NEURO_PROMPT_TRAJECTORY=1` 后，Provider adapter 在构造最终请求体之后、派发之前发出有界结构 metadata：
请求序号/来源、供应商/模型、context generation、cache epoch、消息/工具数量、按密钥计算的消息/工具/稳定前缀/请求指纹，
以及公共前缀、首次分歧和 append-only 比较。进程使用随机 HMAC key；不会保留请求正文，也不会输出消息文本、工具参数、
隐藏 reasoning、headers、endpoint 或凭据。指纹数与 binding 数都有上限。超过消息边界时，不做比较，也不生成整个请求指纹，
避免把截断比较冒充完整结果。进程重启后会生成新的 key 和 trajectory。

这是一项可选的开发诊断，不是完整 Runtime Trace，也不代表远端 Provider 的缓存行为。只有 Provider 实际报告的用量字段才会被
传递；只有输入 token 语义与分母足以精确计算时才设置 `cache_reuse_ratio`。未知缓存字段保持 `None`。

### Cache Epoch 与规范历史

契约为 `Stable Prefix → Monotonic Provider Projection → Explicit Cache Boundaries`。同一 epoch 内，普通 Main Agent 请求
保持此前 Provider-visible message sequence 作为消息边界前缀，并保持工具定义不变。工具 schema、供应商/模型、配置、Project
scope、新 binding、Microcompaction 批次、Full Compaction 或 Fresh Context Rollover 变化会推进类型化 cache boundary。
Cache epoch 是应用结构，不代表 Provider 一定命中缓存。

规范 Session History 仍由 Session persistence 层拥有。新增的有界内存 Provider Projection Journal 只保存应用拥有的类型化
synthetic control messages，并锚定在它首次可见的规范条目边界之后。Working Set、计划、预算、监督、指令范围和技能目录变化会追加；
相同的最新修订会去重。Journal 不保存原始工具结果、凭据或隐藏 Provider payload，也不会把 journal 条目写入 history、export、
resume、fork 或 compaction 输入。Journal 到达容量上限时 fail closed。进程重启会丢弃 journal，并开启新的 binding epoch。

每个 epoch 首次适用的项目指令和技能快照留在稳定前缀中。作用域或目录变化会追加完整的当前修订，保证深层指令继续生效，并明确
覆盖旧兄弟目录的作用域。Project Memory 仍是按 generation 固定的 snapshot；后台 extraction 不会改写 active request prefix。
已提交的 Fresh Context Rollover 会刷新该 snapshot，Project rename 不会。

### DeepSeek 回放

使用 DeepSeek V4 DSML dialect 且启用 function tools 时，按其当前工具调用协议回传历史 assistant `reasoning_content`。
它仍是 Provider request 字段，不会显示为普通 assistant 文本，也不会进入生成的 compaction summary。原始 function argument JSON
的词法序列化仅保存在有界、进程内的 replay window，并绑定 tool-call identity 与规范语义指纹；只在同一 Provider adapter
实例/dialect 内使用，binding 或指纹不匹配时回退到规范 JSON。不会把原始序列化加入规范持久历史。其他 OpenAI-compatible
dialect 保留原有行为。

## 影响

- CI 可根据真实 adapter 请求形状检测未声明的旧前缀改写，而无需保存敏感请求正文。
- 合成运行时修订保持仅追加，并与持久历史隔离。
- Microcompaction 继续以批量方式改写投影并显式开启新 cache epoch；之后的 snapshot 在下一边界前保持稳定。
- 应用可以分别报告结构前缀连续性和 Provider 报告的缓存用量。
- Provider 缓存命中保证、精确 tokenizer 前缀估算、持久化 Provider replay payload、供应商专属 cache key 和完整 trace UI
  不在本决策范围内。

## 验证

确定性测试覆盖 HMAC 脱敏、请求来源与 boundary metadata、DeepSeek 五步投影连续性、工具 schema 稳定性、合成修订顺序与容量、
重启行为、DeepSeek reasoning/argument 回放、Provider failover/retry、受支持 adapter 的最终请求体追踪以及缓存用量语义。
CI 不调用付费 Provider。
