# ADR 0172: Project Memory V1

[简体中文](../../zh-CN/adr/0172-project-memory-v1.md) · **English**

- Status: Accepted
- Date: 2026-09-23
- Scope: Local, bounded, cross-session memory owned by an optional `SessionProject`

## Context

ADR 0165 introduced projects as optional session groups. Durable project
decisions and project-specific user feedback still had to be rediscovered in
each conversation. Memory must follow the durable project identity, preserve
sessions that do not belong to a project, and remain subordinate to the current
workspace, Git state, and repository instructions.

## Decision

**`SessionProject.id` is the sole memory owner identity.** `cwd` remains a
workspace binding and never creates or selects a project implicitly. Each
project's files live under Neuro Code's configured state root at
`project-memory/<project-id>/`; equal working directories do not share memory,
and renaming a project leaves its identity unchanged. A session with a null
`project_id` has no Project Memory scope.

**Memory is a small, inspectable application feature.** The domain records a
stable `memory_id`, name, description, `project | feedback | user | reference`
type, body, origin session, and creation/update timestamps. `user` means
background relevant to this project only. An application port is implemented
by a bounded file adapter with a manifest, generated `MEMORY.md` index, and one
body file per memory. UUID project identities, memory filename patterns,
no-follow reads, containment checks, and strict file/name/count/byte limits
reject traversal, links, malformed manifests, and oversized content. Automatic
memory never enters a user repository.

**The request context contains the index, not every body.** A Project-bound
Main Agent adds only the bounded index after repository instructions and skills
and before session history. The message has a dedicated synthetic reason and
never becomes durable conversation history. The index and the
`read_project_memory` tool call memory contextual evidence, not instructions;
the current repository, Git state, and `AGENTS.md` always take precedence. The
tool accepts one exact memory identity and resolves it through an
application-owned read service scoped to the binding. It cannot read arbitrary
state-root paths. Subagent bindings do not receive this scope by default.

**Extraction is asynchronous, bounded, and owned by application lifecycle.**
After a completed durable user turn in a Project-bound session, a composition-
owned manager schedules at most one provider request at a time per project. It
reads only the durable suffix after that session's extraction cursor, reuses
the manifest to update matching memories, disables tools, and applies source,
prompt, output, event, memory-count, queue, and 20-second limits. Every new
entry records its origin session. Configured and recognizable credentials are
redacted before extraction requests and persistence. Explicit English or Chinese remember/forget
requests use bounded deterministic operations; ambiguous forgetting is a
no-op. Other candidates must come from a strict bounded JSON response, and
project/feedback descriptions retain Why and How to apply. Failures and
cancellation produce typed, logged outcomes and never undo or fail the
committed user turn. Shutdown cancels and gathers the manager-owned worker;
unprocessed durable turns remain eligible after restart because their cursor
was not advanced.

**Session lifecycle remains the authority for scope changes.** Resume restores
`project_id` from the session row. The project view can start a fresh session
with an explicit project id; that id is bound before the first turn persists
the row. Forking a project-bound session copies its project owner along with
the durable session projection; a subagent binding still receives no memory
scope. Moving the open session serializes with turns and switches both
context and recall scope for later requests. Deleting a project serializes
against extraction, purges that id's files, then detaches its sessions; if the
purge is unsafe it fails closed and keeps the database project. If the later
database delete fails, the project remains but its memory is empty and can be
recreated by subsequent completed turns. The existing v35 session schema is
unchanged.

## Consequences

- Existing unassigned sessions, session resume, context compaction, fresh
  context rollover, provider affinity, permission rules, workspace adapters,
  and sandbox behavior remain authoritative.
- Project Memory extraction does not save code structures, paths, Git history,
  `AGENTS.md` rules, plans, temporary progress, or ordinary debugging.
- Global user memory, vector search, graph memory, cloud sync, and a full memory
  management UI remain out of scope. The TUI adds only an explicit
  “new session in project” action and project-delete disclosure.
- See [ADR 0165](0165-session-projects-and-library-management.md) for the
  underlying optional project/session relationship.
