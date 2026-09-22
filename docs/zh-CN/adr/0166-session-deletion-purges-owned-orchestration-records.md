# ADR 0166：删除会话时清理其自有的编排记录

[English](../../en/adr/0166-session-deletion-purges-owned-orchestration-records.md) · **简体中文**

- 状态：已接受
- 日期：2026-09-21
- 范围：`SqliteSessionStore.delete_session` 针对编排记账数据的数据生命周期

## 背景
一个会话可以拥有持久化编排记账数据：Ultracode 执行、Agent Swarm 运行、Leader 尝试与决策、
任务 DAG 及其节点、依赖中继、恢复占用、规划与重规划尝试及提案、结果采纳及其目标、父上下文
中继。它们的外键以 `ON DELETE RESTRICT` 引用 `sessions(id)`，这是刻意的：任何东西都不得
静默销毁持久化证据。

`delete_session` 只遍历了子代理子会话树，并拒绝仍持有可写子代理租约的会话。其余带限制的表
都交给了 SQLite，因此删除任何用过这些工作流的会话都会失败，并抛出裸的
`sqlite3.IntegrityError: FOREIGN KEY constraint failed`。TUI 原样显示该错误，会话完全无法删除。

## 决策
`delete_session` 在删除会话行之前，于同一事务内按子到父的顺序移除被删除会话子树所拥有的
编排记录：结果采纳目标、Leader 决策、重规划提案、规划提案、恢复占用、Leader 尝试、重规划
尝试、规划尝试、Swarm 运行、Ultracode 执行、结果采纳、父上下文中继，然后是 DAG 自有的依赖
中继与节点，最后是 DAG 本身。按 DAG 归属过滤的表以子树拥有的 DAG 为界，因此清理绝不会触碰
其他会话的记录。

两条边界保持不变：

- 仍持有**可写子代理租约**的会话会被拒绝，因为租约拥有需要显式清理的磁盘工作区资源；
- 当前已在会话中打开的会话由应用层而非存储层拒绝。

任何剩余的外键失败都会被转换为 `SessionError`，不再泄漏裸的 SQLite 错误。整个删除始终是
一个事务，因此拒绝时数据库保持原样。

Schema 继续保留 `ON DELETE RESTRICT`。清理是应用侧的显式知识，而不是隐式级联，因此新增的
限制表在未被显式处理前会失败关闭。

## 后果
- 从历史中删除会话现在也会删除该会话的编排证据。这是有意为之：会话归用户所有，而另一种
  选择是历史条目无法删除。
- `delete_session` 的对外行为从"对编排过的会话抛出裸 SQLite 错误"变为"移除会话及其自有编排
  记录"。不涉及 schema 版本变化。
- 提取规则未覆盖的跨会话引用会失败关闭：事务回滚，调用方收到 `SessionError`。
- 已在一个接近生产形态的数据库副本上验证：每个会话都能成功删除，且之后
  `PRAGMA foreign_key_check` 无任何违规。
