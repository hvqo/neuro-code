# ADR 0165: Session projects and library management

[简体中文](../../zh-CN/adr/0165-session-projects-and-library-management.md) · **English**

- Status: Accepted
- Date: 2026-09-21
- Scope: Optional project grouping, session/project CRUD, and the TUI management surface

## Context

Session resume existed, but the durable session store only exposed "list recent
sessions", "resume", and "rename the active session". Users could not create a
session deliberately, delete one, rename an arbitrary one, or group related work
together. Coding agents commonly let one project own many conversations, and this
project had no such notion at all.

Two constraints shaped the design:

- sessions must not be forced into a project — a session that belongs nowhere
  stays fully usable, and existing rows must keep working;
- the conversation lifecycle already has one owner
  (`ProfileConversationController`), which serializes turns, replaces bindings,
  and holds the turn lock. Session management must not become a second owner.

## Decision

**Projects are an optional grouping owned by session storage.**
`SessionProject` (id, name, cwd, created/updated timestamps) is a domain value
object in `neuro_code.domain.sessions`; `normalize_project_name()` bounds and
normalizes names, and the store enforces uniqueness of a normalized name. The
`sessions` table gains a nullable `project_id` column and the sessions database
schema advances to v35 with an idempotent `session_projects` table, a unique
name index, and a `sessions(project_id, updated_at DESC, id DESC)` index. The
project column is appended to the existing session projections, so a narrow
projection that does not select it keeps working.

Deleting a project detaches its sessions and never deletes them. Sessions remain
addressable, resumable, and searchable after their project disappears; a session
row whose `project_id` no longer resolves is still listed, and the interface
renders it as belonging to a missing project.

**`SessionLibraryService` owns the bounded management vocabulary.**
`neuro_code.application.sessions.library` exposes project CRUD, arbitrary-session
rename, session deletion, and project assignment over the existing
`SessionStore` port, plus a `SessionLibraryOwner` seam for the two operations
that need the conversation owner: listing workspace sessions and starting a
fresh session. The service caches nothing.

**Starting a new session reuses the binding replacement path.**
`ProfileConversationController.start_new_session()` builds a fresh binding for
the selected profile, asserts it carries no session id, applies the existing
conversation policies, and replaces the binding while holding the turn lock. The
new session is therefore lazily persisted exactly like a cold-start session: it
is written on the first turn, and the previous session stays durable. No new
storage path or eager empty session is introduced.

**Deleting the open session is refused.**
The library raises a configuration error instead of unloading the bound
conversation. Deleting a session removes its events, and silently doing that to
the session the user is looking at would discard the open transcript.

**The TUI management surface is presentation-only.**
`SessionLibraryScreen` renders sessions and projects in two switchable views and
returns one typed `SessionLibraryAction`; the controller performs it through the
library service and re-opens the screen. Rows show the owning project, and the
screen offers open, new session, rename, move to project, and delete, plus project
create, rename, delete, and "show sessions". Destructive operations require an
explicit confirmation modal, and `/sessions` opens the manager while `/resume`
keeps its direct-resume behavior.

## Consequences

- Sessions without a project, and databases created before v35, keep working; the
  migration is serialized in the existing v1→v35 chain.
- The library service and the TUI screen are covered by store, application, and
  Textual harness tests; the previous picker screen remains for embedders that
  inject only the selection service.
- Project identity is local to the sessions database. Export/import snapshots do
  not carry project membership yet, so an imported session arrives unassigned.
- No permission, sandbox, verification, or supervision boundary changes: project
  membership is display and organization metadata only.
