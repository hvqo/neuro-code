# ADR 0160：持久历史可寻址性与有界回填

[English](../../en/adr/0160-durable-history-addressability-and-rehydration.md) · **简体中文**

- 状态：已接受
- 日期：2026-09-14
- 范围：CM1 持久会话历史可寻址性与只读回填

## 背景

Neuro Code 已经把有序的 `SessionItem` 序列持久化到 session store。Runtime
恢复、compaction、导出和 fork 语义都把该序列作为持久状态的 canonical source。已有的
会话搜索是会话级 discovery projection，并不是针对单个 user、assistant 或 tool-result
item 的可寻址精确读取器。

CM1 需要一种面向模型的方式，在不复制 transcript 到另一个 store、也不在每次 model
request 中放入全部历史的前提下，发现并读取较早的安全会话文本。这个边界必须保持只读、
绑定当前会话、有界，并且在重启和 fork 后安全。

## 决策

应用层 session-item query owner 提供有界的 `list`、`search` 和 `read` 操作。它按需读取
canonical ordered durable sequence，在内存中派生紧凑 projection；不增加 history table、
FTS document 或第二份 transcript。

每个公开 item 获得一个不透明的 `shr1_` reference。它由持久化的一基 ordinal、完整
canonical item fingerprint、当前 session identity 的 digest 以及格式 checksum 派生。解析时
会针对绑定会话、当前 ordinal 和当前 item fingerprint 校验 token。append-only persistence
让未改变的 prefix 保持稳定；跨会话、过期、格式错误或被篡改的 reference 都失败关闭。原始
session identifier 不会进入 reference 或 tool argument。fork 拥有不同的 session scope，
因此不能解析 parent reference。

公开 projection 只包含非 synthetic 的 `USER`、`ASSISTANT` 和 `TOOL` message。它只暴露
脱敏后的可见文本和有界 tool name，不暴露 system message、synthetic runtime context、
provider-native preserved item、assistant 隐藏 reasoning、tool-call argument 或其他内部
payload。`list` 返回按新到旧排列的 metadata，`search` 在安全 projection 上执行有界文本匹配，
`read` 在 UTF-8 边界上返回一个精确的安全 item 文本块。page size、query size、preview size、
read size 和 tool output 分别受限。

一个普通本地 `session_history` tool 暴露这些操作，并声明 `side_effecting=False`。它的
schema 不包含 session identifier。Runtime 通过内部 `ToolContext` binding 提供可信的当前
session identity；tool 只接受该会话此前返回的 reference。既有 tool executor 继续负责最终
output 脱敏和正常执行边界。

query 操作不会写 session state、event、compaction row、note、artifact 或 recovery record。
重新打开 store 时，projection 仍直接从 durable item 重新派生。如果其他生命周期操作替换了
item，使其 canonical fingerprint 不再匹配，旧 reference 会成为 stale，而不会静默解析到不同
内容。

## 不变量与非目标

持久 item 顺序、append-only prefix validation、turn finalization、recovery、compaction、
provider affinity、redaction、permission 与 workspace boundary、verification state 以及
subagent isolation 继续由现有 service 所有。CM1 不增加 working-set abstraction、relevance
eviction、rollover、note 或 summary provenance、artifact redesign、跨会话浏览、parent-child
历史浏览、provider accounting、embedding retrieval，也不增加新的公开 CLI/TUI/ACP history
surface。

## 兼容性与验证

现有 `SessionStore` schema 和 canonical persistence method 保持不变。聚焦测试覆盖 append
后的稳定 reference、格式错误与跨会话拒绝、有界 pagination/search、可见 user/assistant/
tool-result 文本、隐藏/native/synthetic 排除、UTF-8 大 item 读取、fork 隔离、Runtime session
binding、只读行为以及 registry wiring。
