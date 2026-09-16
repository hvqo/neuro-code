# ADR 0162：Fresh active-context rollover

- 状态：Accepted
- 日期：2026-09-15
- 范围：CM3a 有界 normal-agent context lifecycle control

## 背景

Normal Agent 会保留 canonical durable `SessionItem` sequence，并在每次 model request
中基于该 sequence 加上刷新后的 synthetic instructions、skills、Working Set、Provider/runtime
guidance 以及其他 binding-owned projection 构建 context。现有 compaction 可以在安全边界替换旧的
有界 projection，但还没有一个显式的 user/model control，可以在继续同一个 session 的同时启动
完全 fresh 的 active context。

CM3a 需要增加这个 control，同时不能创建第二份 history、复制 Working Set、改变 provider affinity，
也不能把 transient runtime context 当作 conversation history。持久 generation boundary 还必须让
control 与下一次 model request 之间发生 crash 时保持确定性。

## 决策

在现有 `sessions` row 增加一个单调递增的 `context_generation` integer、一个 committed exclusive
canonical `context_generation_start_index` 以及可选的 pending turn-boundary anchor。Schema version 32
增加 generation marker；schema version 33 增加 boundary columns，并以 additive 方式迁移 legacy schema-32
database。对于已经处于非零 generation、但无法恢复历史 boundary 的 session，migration 将 committed
boundary 设为当前 canonical item count，这是 fail-closed 选择。`SessionStore` 负责读取并原子递增
generation 和 boundary metadata；`SessionContextRolloverApplicationService` 是 application boundary。
这些值是 session metadata，不是复制的 history sequence、summary 或 Working Set row。

Normal composition 将只绑定到当前 `ToolContext.session_id` 的 runtime-only `new_context` tool。
该 tool 没有 model arguments，使用 exclusive，并且不会通过 child/subagent binding 暴露。它声明
`side_effecting=False`，因为现有 flag 表示 workspace/shell mutation；它的 bounded acknowledgement
会明确报告 `durable_write`。在递增 marker 之前，tool 会验证配置的 output limit 能表示最大的有效
acknowledgement。因此过小的 limit 会在 mutation 前失败。成功递增后返回 generation、`fresh_context=true`
和 `durable_write=true` 的 compact JSON result。

`AgentLoopRunner` 继续保留完整的内存 turn sequence，供既有 finalization 和 persistence 使用，但
另外维护 active model context projection。Generation 在 run 开始时大于零，则 active projection 以现有
system messages、持久 exclusive boundary 之后的 canonical items 以及本回合新的 user input 开始；旧
durable item 仍在完整 sequence 中，但不会注入这个 active projection。成功执行唯一的 `new_context` call
后，也会立即安装同样的 projection boundary：system prefix 和 current user message 作为下一次 request
的 seed，随后是新的 generation notice 以及之后的 runtime/model/tool item。Control 会将当前 durable item
count 记录为 committed safe fallback；如果 runtime 提供 candidate canonical boundary 和 owning turn ID，
也会将其记录为 pending anchor。该 turn 完成后，atomic session write 只有在 canonical prefix 已包含
candidate 时才 promotion；因此同一 generation 的后续 normal turn 会保留彼此的 durable message 和
provider-native item。Instructions、skills 和 current Working Set 由既有 `ContextBuilder`/Working Set
路径为每个 request 重新构建。

该 control 必须是 model step 中唯一的 tool call。混合 batch 会被拒绝且不会递增 generation。Control
不可用、参数无效或 output limit 不足时 marker 保持不变。Control result 在 turn 完成时仍经过普通的
tool/event/finalization path。对于 unresolved owning turn，rehydration 使用 committed fallback，因此
process 在 finalization 前停止时，不会把未提交的 turn 放入 active projection。Finalization 只有在
durable prefix 能表示 pending candidate 时才 promotion，并清除 anchor；explicit abandonment 只清除
匹配的 anchor，不改变 generation 或 fallback。Recovery 不依赖持久化的 `new_context` tool result。
既有 unresolved-turn recovery 规则仍然适用；rollover 不会自动 replay Provider request。

Rollover 不会删除或替换 compaction。现有 compaction 仍由原有 safe-boundary gate、provider-window/
accounting 规则、durable compaction row 和 recovery reconstruction 拥有。新的 generation 会清除前一个
active context 的内存 compaction projection；之后如果在允许的边界独立触发 compaction，则只评估新的
active projection。Provider selection、failover、native context affinity、permission、verification、
workspace/checkpoint state 和 final response commitment 都沿用既有路径。

Session 重新打开时，durable compaction resume 同样会针对当前 active projection 进行评估。
因此现有的精确 source-count、source-fingerprint 和 provider-origin 检查会接受有效的当前
generation record，同时忽略 rollover 之前的 record；`ContextPreflight` 会计量重建后的当前
generation projection。

## 不变量与非目标

- Session identifier 和 canonical ordered durable `SessionItem` history 不变。CM1 list/search/read
  仍是按需检索旧的、精确的、可公开 item 的方式。
- 当前 CM2 Working Set row 在每次 request 再次读取；rollover 不复制、reset 或重写它。Working Set
  synthetic model message、rollover notice、刷新后的 instructions/skills 以及其他 runtime-only message
  沿用既有 persistence filter，不进入 canonical history。
- 非零 generation 会在后续 normal turn 中持续累积 boundary 之后的 canonical item。下一次显式
  rollover 会递增 generation 并安装新的 boundary，使此前 generation 的普通 item 和 provider-native
  item 都不进入 active projection，但仍可通过 CM1 获取。
- Rollover 永不改变 provider identity、permission、verification evidence、workspace authority、sandbox
  policy 或 final-response truth；它只改变 active model projection 和 durable generation marker。
- Control 有界、session-scoped，并且只属于 normal agent；不存在独立的 rollover threshold，有界的
  automatic policy 由 [ADR 0163](0163-automatic-fresh-context-rollover.md) 单独规定。没有 cross-session
  memory、embedding retrieval、semantic search、history schema/index redesign、artifact redesign，也不
  移除现有 compaction。

## 兼容性与验证

Schema 变更是 additive 的：现有 session 从 generation zero 开始，既有 history/Working Set/compaction
data 仍可读取。已有 schema-32 非零 generation 会被保守地 backfill 到当前 history 末尾，因为旧 schema
没有保存 exact boundary。Fork session 保留现有 history-copy 行为，但使用 default generation，也不会
继承 CM2 Working Set state。聚焦测试覆盖回合内 fresh projection、reopen 后的 cross-turn accumulation、
provider-native item isolation、Working Set 与 canonical history 保留、写入前 output-limit 拒绝、schema
migration 以及下一次 model step 被中断后的 generation recovery。Ruff、format、聚焦 mypy 和文档 parity
是本变更的 change-level check；仓库完整 CI matrix 仍是最终的完整验证 gate。
