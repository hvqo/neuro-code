# ADR 0200：DW4b completed-DAG 结果采纳 adapters

[English](../../en/adr/0200-dw4b-completed-dag-adoption.md) · **简体中文**

- 状态：已接受内部 adapter 基础
- 日期：2026-10-06
- 范围：复用 Result Adoption；不执行 Workflow，不提供 verification

## 决策

`CompletedDagAdoptionSource` 绑定有类型区分的 Swarm/Workflow 来源、parent session、
精确 DAG id/generation/definition fingerprint。Workflow reference 固定 Run、Expansion、
Projection identity/fingerprint；Projection source digest 进一步绑定 Step/iteration/item、
冻结 member、终态 node generation、exact result evidence 与 workspace facts。
它不授予 Workflow ownership 或权限。调用方任意 DAG、worker 文本或 preview 不构成来源。

Swarm/Workflow adapters 为既有 `ResultAdoptionApplicationService` 解析同一种 typed source。
组合根提供两个 adapters；不创建假 Swarm row、不混用 id、不新增 copy-back/merge engine 或
scheduler。Workflow adapter 只从 DW4a port 读取 Projection，复用其 DW3 publication 与
完整 source linkage 重验证，并核对 exact DAG。只有 COMPLETED DAG 的 completed writable
workers 可采纳；FAILED/CANCELLED/INDETERMINATE Projection 不能授权采纳。

## 工作区安全与恢复

继续复用既有 application core，检查真实 parent binding/root/repository/HEAD、preserved
lease、managed READY worktree、READY baseline checkpoint、capability/grant 与 final
workspace fingerprint。仍生成相同的有界 baseline→desired 三方 target，拒绝 worker overlap、
protected path 与父同路径冲突，保留父无关 dirty file。所有写入继续经注入的
Permission/Workspace/Sandbox mutation port；adapter 不写文件。

Durable adoption owner/lease、target CAS、desired-image observation 与 forward recovery 不变。
写入成功但 ACK 前崩溃时，通过观察 desired image 恢复，不重复写；绝不覆盖第三方 image。
创建 plan 前再次核对 Workflow 来源未漂移。Live source 重验证保护执行与 forward recovery；
durable terminal replay 只依赖持久化 adoption identity/integrity，不要求执行时资源继续存活。
所有终态均在校验 plan 完整性、精确请求来源与 parent session/root 后返回既有事实，不再解析
Projection、不要求 preserved lease/worktree/checkpoint 或当前 parent HEAD 保持不变。
非终态 replay 继续保留 exact source、live parent 检查、owner fence 与安全 forward recovery。
恢复使用 immutable materialized plan 与既有 adoption owner lifecycle；terminal replay 与
Projection replay 均不取得 Workflow Run ownership。

## 持久化兼容与身份

Schema 保持 v38：在既有带 fingerprint 的 `plan_json` 中增加 source variant。
旧及新 Swarm plan 均保留原 JSON 字段（包括 `swarm_run_id`）与原 fingerprint 算法。
解码只暴露 typed Swarm provenance，不改写旧 SQLite row，不绕过完整性校验。
Workflow plan 不含 `swarm_run_id`，新增 `completed_source`；混合身份 fail closed。
Source kind 防止 Swarm/Workflow identity collision。

调用方提供稳定 `adopt-*` id 与精确 Workflow Projection reference。
Immutable plan 把 adoption/source 绑定到 parent repository/HEAD、exact DAG、worker facts
及生成的 targets。Exact replay 返回/恢复原 plan，不重复 mutation；相同 id 不同
source/projection/parent/DAG 拒绝。并发 preparation 只有在除创建时间外所有字段完全相同时
才复用胜出 plan；仍须单独取得 adoption ownership。Retry 不授予 ownership。

## 结果与排除项

复用 typed `ResultAdoptionRecord` 的 state、plan fingerprint、adoption id 和
`parent_workspace_changed`。Adoption COMPLETED 不等于 verification PASS。
不推进 Workflow Run/Step、generation、budget、journal，不执行 Branch/Repeat、VERIFY/REPAIR，
不标记 Workflow COMPLETED，不增加 Planner/UltraCode 的 Workflow wiring。
既有 Swarm/UltraCode 路径保持兼容。Interpreter/Activity、completion requirements 与验证留待后续。

## 验证

真实 SQLite 回归覆盖 TaskBatch/Map、Projection 完整性、错误 Run/session/Expansion/DAG、
非成功终态、stale preserved resources、overlap、父冲突、无关脏文件、cleanup/父 commit 后的
terminal replay、非终态 source 重验证、exact replay/reopen、
写入后 ACK 丢失的 forward recovery、来源身份冲突与旧 Swarm JSON/fingerprint。
既有 adoption、Swarm、UltraCode、权限与 migration 回归继续作为门禁。
