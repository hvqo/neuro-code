# ADR 0163：Automatic fresh-context rollover

[English](../../en/adr/0163-automatic-fresh-context-rollover.md) · **简体中文**

- 状态：已接受
- 日期：2026-09-15
- 范围：CM3b 有界 automatic normal-agent context recovery

## 背景

CM3a 提供显式 `new_context` control 和 durable context generation boundary。现有 normal-agent
runtime 也拥有权威的 `ContextPreflight` assessment 和 safe-boundary automatic compaction path。
一个有界 request 在允许的一次 compaction（保留选定旧信息）之后仍可能过大，但如果丢弃 active
projection，fresh request 又可以适配。

CM3b 需要连接这些已有 owner。它不能增加第二个 context threshold、summarizer、generation counter、
history store 或 transient reset。Compaction 和 rollover 的目的不同：compaction 保留选定旧信息的
有界 summary，rollover 启动新的 active generation，同时保持 canonical history 和外部 task state。

## 决策

`AgentLoopRunner` 为 normal、finalizing user turn 拥有一个有界 automatic recovery cycle。
`ContextPreflight` 仍是唯一的 capacity decision，并使用与 model request 相同的 request-shape、tool、
output-reserve、safety-margin 和 provider-window accounting。

决策序列是确定性的：

1. 构建真实 request 并运行 `ContextPreflight`。
2. `SAFE` 正常继续；`UNKNOWN` 保持现有行为，不臆造 capacity 或 rollover threshold。
3. 不可约的 `BLOCKED` request 在 compaction 或 rollover 之前，沿用已有 deterministic budget-limited
   path 完成。
4. 对 `COMPACTION_REQUIRED`，在已有 safe boundary 调用一次既有 automatic compaction gate。基于所得
   compatible projection rebuild request，并再次运行 `ContextPreflight`。从更早 turn resume 的 compatible
   durable compaction 是可复用 state，不会消耗本 request cycle 的 compaction attempt。如果 planner 选择
   完全相同的 source range，则复用已有 projection 而不再次调用 summarizer；但这个 no-op 仍是该 cycle
   唯一的 bounded compaction decision。因此在考虑 rollover 之前，source range 变化时仍可以产生新的
   compaction。
5. 如果 projection 仍为 `BLOCKED`，automatic rollover 只在当前 user turn 的第一次真实 provider request
   之前 eligible，并且必须满足 durable controller、session 和 turn identity 可用，当前 generation 存在
   可丢弃的 active history，request 可约（`irreducible_tokens < capacity_tokens`），且 prospective fresh
   projection 严格更小。这会排除自动丢弃未提交的 tool result、assistant tool call 和 provider-native item。
   同一个 model-step cycle 不允许第二次 automatic rollover attempt。
6. 对完全相同的 prospective fresh request 运行同一个 `ContextPreflight`，其中包括 current user、rebuild
   后的 Working Set/instructions、冻结的 tools、provider identity、output reserve 和 safety margin。
   只有 `SAFE` preview 可以使用当前 canonical boundary 和 pending turn anchor 调用已有 CM3a rollover
   controller。`BLOCKED` 或 `UNKNOWN` preview 不修改 durable state、不调用 provider，并进入 deterministic
   block。成功 commit 后，使用 fresh generation rebuild 真实 request，并在调用 provider 前再运行一次
   defensive final preflight；仍为 `BLOCKED` 时，沿用 deterministic budget-limited finalization，不再进行
   compaction 或 rollover。

Automatic action 直接调用 application rollover controller，不合成 model tool call，也不持久化伪造的
`new_context` result。Controller 的 monotonic generation、exclusive canonical boundary、pending turn
anchor、committed fallback、finalization promotion 和 abandonment cleanup 仍是唯一的 durable rollover
语义。如果 durable transition 无法提交，runtime 保留旧的 in-memory projection 并进入 deterministic
budget path。

Fresh projection 复用 CM3a 语义：system prefix 和 current user request 各保留一次；refreshed instructions、
skills 和 current CM2 Working Set 由已有 owner 重建；前一 generation 的 ordinary item、compaction
projection、synthetic runtime notice 和 provider-native preserved/reasoning state 不会重新注入。
Canonical durable `SessionItem` sequence 不变，因此旧的安全内容仍可通过 CM1 `session_history` 显式恢复。

Provider origin 仍绑定真实 generation。Fresh boundary 之后，failover 选中的 provider 可以为新 generation
产生的 native state 建立 durable origin。已有 provider affinity 和 native replay compatibility 仍会拒绝
过期或 foreign state。Permission、sandbox、workspace/checkpoint、verification、final-response、turn
recovery 以及 parent/child binding boundary 继续由已有 path 拥有。

Automatic policy metadata 只加入既有 preflight event projection：有界 boolean 标识 eligibility、preview/attempt
和 committed fresh-boundary success。Raw context、prompt、provider payload 和 secret 不会写入 diagnostic
metadata。因此 blocked 或 unknown prospective preview 可以和 committed rollover 区分，而无需新 event 或
durable record。

## 不变量与非目标

- 对一次 model request/preflight recovery cycle，最多执行一次 automatic compaction decision 和一次
  automatic rollover。Compaction decision 按当前 cycle 单独跟踪，而不是从较早的 compatible active
  projection 是否存在来推断。同一 source range 的 reuse 不会重复调用 summarizer，但会消耗该 cycle 的
  decision；之后的 normal model step 可以开始新的有界 cycle。
- `UNKNOWN` 不会被当作 zero capacity、infinite capacity，也不会被当作猜测 threshold 的许可。不可约 request
  不会 rollover。
- 成功的 automatic rollover 是同一 session 内的 durable CM3a transition；它不会删除 compaction record、
  改写 canonical history、复制 Working Set，也不会 reset task、permission、verification、workspace、
  sandbox、provider 或 finalization state。
- Automatic rollover 只适用于第一次 provider request 之前的 policy。Mid-turn pressure 继续沿用已有
  compaction/block 行为，直到未来的有界 tool-result policy 能证明未提交 evidence 可恢复。
- Pending boundary 处的 cancellation 或 crash 沿用 CM3a recovery。Unresolved turn 不会被自动 replay；由
  finalization 或 explicit abandonment 决定 candidate boundary 是否 promotion。
- 没有 user-configurable rollover threshold、generic context-policy DSL、新 summarization model、cross-session
  memory、semantic retrieval、embedding index、automatic Working Set rewrite、automatic subagent rollover、
  UltraCode change、CM3b 后续 policy，也不移除 explicit `new_context`。

## 兼容性与验证

已有 CM3a schema 和 ports 足够支持 CM3b。CM3b 不增加 database column、compaction metadata、history index
或新的 durable counter。既有 compaction 的 source-count、source-fingerprint、provider、model、affinity
和 non-overlap compatibility 规则仍是权威；新的 generation 只清除旧 active in-memory compaction
projection。

聚焦 regression 覆盖 safe 和 unknown request、只进行一次 compaction 且不 rollover、compaction 不足后进入
fresh provider request、fresh seed 在不 mutation 的情况下仍被 block、不可约 block、current user 和 Working
Set 保留、旧 history/native state 排除且 CM1 保持 durable、mid-turn tool/native evidence 保留、committed
boundary 处的 cancellation/reopen，以及新 native state 的 failover rebinding。既有 CM3a、compaction、
preflight、provider、recovery、Working Set、history、verification 和 architecture checks 仍属于变更验证；
仓库完整 CI matrix 仍是最终 gate。
