# ADR 0159: Read-only Git change inspection

- Status: Accepted
- Date: 2026-09-13
- Scope: B2 bounded read-only Git inspection for normal Agent runs

## Context

Neuro Code can already execute Git operations through its hardened worktree
adapter, but a normal Agent and its user need a bounded view of repository
identity and current changes. A generic Bash command is not an acceptable
inspection boundary: it permits caller-controlled revisions, paths, config,
hooks, and other Git modes, and it does not provide one typed projection for
the CLI and model tool.

## Decision

Introduce one application-facing `GitInspectionApplication` port and one
`GitInspectionService`. The local infrastructure adapter is the sole owner of
Git inspection execution and parsing. It uses fixed Git commands and a strict
Git porcelain v2 `-z` parser to produce repository identity, branch or detached
HEAD state, upstream counters, status entries, and separate staged and
unstaged diff projections. Rename/copy, unmerged, untracked, binary, and
submodule state remain metadata; untracked file contents are never read
automatically.

The public normal surfaces share this projection. `git_inspect` is a
non-side-effecting tool with only the bounded `status`, `diff`, and `all` view
selection. `inspect git` uses the same application service. No new TUI or ACP
surface is added, and the existing `GIT_READ` permission classification remains
the authority for Git reads. Interfaces do not execute subprocesses or compute
an alternate status model.

The adapter runs with `GIT_OPTIONAL_LOCKS=0`, disabled hooks and fsmonitor,
system/global Git configuration disabled, and network isolated by the existing
local process sandbox. Paths, status records, diff bytes, errors, and rendered
output are bounded and redacted. Fixed diff arguments disable external diff,
textconv, color, and rename processing; binary output is represented by
metadata rather than patch contents. Truncation and incomplete projections
are explicit typed states, and malformed or unknown protocol records fail
closed.

The read-only inspection path has no workspace, index, ref, config, history,
checkpoint, session, verification-generation, or B1 state mutation. It does
not create database rows or durable recovery state. The adapter rechecks the
repository identity and the status HEAD before returning a result, so a
repository identity mismatch fails closed rather than returning a mixed
snapshot.

## Compatibility and non-goals

Existing Git/worktree behavior, permissions, checkpoint/undo behavior,
provider behavior, session schema, and ACP/TUI contracts are unchanged. The
capability is intentionally not a Git commit, branch, worktree, history,
checkpoint, rollback, or generic Git GUI feature. It does not provide
automatic untracked-content discovery, recursive submodule inspection, or
verification evidence. A later task may add user-facing change presentation;
B2 only establishes the shared bounded read projection.

## Validation

Focused tests cover clean, detached, staged/unstaged/mixed, untracked,
rename/copy, conflict, binary, large, malformed, non-repository, unsafe
configuration, timeout, cancellation, submodule, CLI, and model-tool paths.
