# ADR 0173：确定性 Microcompaction V1（微压缩）

**简体中文** · [English](../../en/adr/0173-deterministic-microcompaction-v1.md)

- 状态：已接受
- 日期：2026-09-23
- 范围：Context Preflight 前的确定性模型可见投影清理

## 背景

Tool Result Guard 会限制单条结果，但较长的会话仍可能保留很多旧结果。Full Compaction 可以总结较大的历史区间，Fresh Context Rollover 可以开启新的 active generation；二者都具有更广泛的改写语义和成本。若每次请求只清除一条旧结果，前缀会持续移动并反复扰动 Prompt Cache。

Microcompaction 需要缩小请求，同时保持规范会话历史不变，也不能取代现有 compaction、rollover、permission、provider affinity、recovery、verification 或 artifact 所有者。

## 决策

**Microcompaction 是确定性的投影清理，不是 Full Compaction。** 它不调用模型、不生成语义摘要，也不写入会话、artifact、audit、verification 或 recovery store。它复制模型可见的 `ModelContext`，只把符合条件的工具结果正文替换成一个固定、有界、通用的 marker。User 和 Assistant 原文不变，每个 assistant call 仍与其有序结果相邻。

**先对最终将发送的请求做压力判断。** 普通 Runtime 顺序为 Tool Result Guard → Microcompaction → Context Preflight → Full Compaction → Fresh Context Rollover。Microcompaction 只在容量已知且 preflight 为 `COMPACTION_REQUIRED` 的普通用户 turn 上触发；现有 Full Compaction 逻辑也会在工具批次后的安全点运行前先评估它。最终 Context Preflight 使用压缩后的投影。若结果为 `SAFE`，Runtime 跳过 Full Compaction 和 rollover；否则继续既有有界路径，不改变其语义。不可约的 `BLOCKED` 与容量未知请求保持现有行为。

**只处理完整、足够旧且由 Runtime 确认成功的 group。** 一个 group 必须由连续的 assistant 工具调用消息和紧随其后的结果组成；结果必须与每个 call 匹配，并按调用顺序排列，而且整体位于当前用户消息之前。最近三个历史 group 和当前 turn 的全部 group 都受保护。error、media/binary、不完整、重复或终态未知的 group 会跳过。Runtime 只从终态工具事件获知结果状态；进程重启后旧结果视为未知并保持完整。格式错误的上下文不能授权改写。

**一次明确触发形成一个批次。** 触发条件是可执行的上下文压力。已完成批次会在同一 session 与 context generation 中固定。后续批次需要再次遇到压力，并且稳定前缀/compaction boundary 已改变，或追加达到至少八个稳定条目或 2,048 个估算 token。该批次会同时处理当时所有符合条件的 group。若确定性估算未同时节省至少 1,024 个序列化条目字节和 256 个 token，则丢弃该提案。这样可避免逐请求滚动清理，并跳过收益很小的改写。

**投影状态有界且只驻留内存。** `MicrocompactionRuntimeState` 在 `(session_id, context_generation)` 下跟踪精确 group fingerprint、稳定边界/前缀 fingerprint、compaction identity 和聚合 telemetry。扫描最多处理 16,384 个条目和 8 MiB 的保守序列化尺寸；嵌套值最多 65,536 个节点、深度 32；单个 assistant group 最多 128 个 call 和 256 个 content part。最多保留 4,096 个 group fingerprint 与 8,192 个终态结果状态；优化状态不持久化。进程重启后规范历史仍是权威来源；空快照和未知状态都会 fail closed。已提交的 Fresh Context Rollover 会重置状态。Full Compaction 可以改变 active projection，但不能改变规范来源；compaction identity 变化时，会把固定 group 集合与新投影重新核对。

**Telemetry 只包含聚合值。** 既有 Context Preflight event 可以携带 trigger reason、稳定边界、group/result 数量、前后估算序列化字节与上下文 token、节省量、类型化 `NOOP` reason，以及估算值是否因来源上限而饱和。它不会包含工具结果正文、参数、路径、artifact ID 或 secret。字节估算使用确定性序列化条目；token 估算是 Provider 中立的近似值，不是 Provider 上报用量。

Microcompaction 遵守 `Stable Prefix → Append-only Conversation → Volatile Tail`。上下文缩减必须同时权衡正确性、token reduction 和 Prompt Cache preservation。历史投影只在显式压力边界上一次性批量改写，不会每个请求清除一条结果。Provider 专属 cache key/breakpoint、语义检索、LLM 摘要、完整 Runtime Trace 与 TTFT telemetry 不属于本 ADR。

## 影响

- 模型请求可省略多条旧的成功工具结果；Session History 和现有 history/artifact 读取能力仍保留原始事实。
- Preflight 评估 Provider 实际收到的同一个 Microcompaction 投影。
- 若微压缩后的请求已安全，则跳过 Full Compaction 和 Fresh Context Rollover；收益不足时仍进入既有路径。
- 重启后不恢复优化快照，也不信任历史工具终态，因此恢复路径确定且 fail closed。
- 参见 [ADR 0164](0164-model-facing-tool-result-guard.md)、[ADR 0160](0160-durable-history-addressability-and-rehydration.md)、[ADR 0162](0162-fresh-context-rollover.md) 和 [ADR 0172](0172-project-memory-v1.md)。
