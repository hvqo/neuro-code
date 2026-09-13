# ADR 0159：只读 Git 变更检查

- Status：Accepted
- Date：2026-09-13
- Scope：B2 普通 Agent 的有界只读 Git 检查

## Context

Neuro Code 已经可以通过加固后的 worktree adapter 执行 Git 操作，但普通
Agent 及其用户需要一个有界的仓库身份和当前变更视图。通用 Bash 不是合适的
检查边界：它允许调用方控制 revision、path、config、hooks 及其他 Git 模式，
也不能为 CLI 和 model tool 提供唯一的类型化 projection。

## Decision

增加一个面向 application 的 `GitInspectionApplication` port 和一个
`GitInspectionService`。本地 infrastructure adapter 是 Git 检查执行与解析的
唯一 owner。它使用固定 Git 命令和严格的 Git porcelain v2 `-z` parser，生成
仓库身份、branch 或 detached HEAD 状态、upstream 计数、status entry，以及
分离的 staged/unstaged diff projection。Rename/copy、unmerged、untracked、
binary 和 submodule 仍只表示 metadata；不会自动读取 untracked 文件内容。

公共普通接口共享这一 projection。`git_inspect` 是无副作用 tool，只接受有界的
`status`、`diff` 和 `all` view 选择；`inspect git` 使用同一个 application
service。不增加 TUI 或 ACP surface，已有 `GIT_READ` permission classification
仍是 Git read 的 authority。Interface 不执行 subprocess，也不计算另一套 status
model。

Adapter 运行时设置 `GIT_OPTIONAL_LOCKS=0`，禁用 hooks 和 fsmonitor，并通过既有
local process sandbox 隔离 network，同时禁用 system/global Git configuration。
Path、status record、diff bytes、error 和 rendered output 均有界并脱敏。固定 diff
参数禁用 external diff、textconv、color 和 rename processing；binary output 只
提供 metadata，不提供 patch 内容。Truncation 和 incomplete projection 使用明确的
类型化状态；malformed 或未知 protocol record fail closed。

只读检查路径不修改 workspace、index、ref、config、history、checkpoint、session、
verification generation 或 B1 state。不创建 database row 或 durable recovery state。
Adapter 在返回结果前重新检查 repository identity 与 status HEAD；如果身份不一致，
fail closed，而不是返回混合 snapshot。

## Compatibility 和 non-goals

既有 Git/worktree 行为、permission、checkpoint/undo、provider、session schema 以及
ACP/TUI contract 保持不变。本 capability 不是 Git commit、branch、worktree、history、
checkpoint、rollback 或通用 Git GUI 功能，也不自动发现 untracked content、不递归检查
submodule、不产生 verification evidence。后续任务可以增加面向用户的变更展示；B2 只建立
共享的有界只读 projection。

## Validation

Focused tests 覆盖 clean、detached、staged/unstaged/mixed、untracked、rename/copy、
conflict、binary、large、malformed、non-repository、unsafe configuration、timeout、
cancellation、submodule、CLI 和 model-tool path。
