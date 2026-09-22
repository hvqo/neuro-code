# ADR 0166: Session deletion purges owned orchestration records

[简体中文](../../zh-CN/adr/0166-session-deletion-purges-owned-orchestration-records.md) · **English**

- Status: Accepted
- Date: 2026-09-21
- Scope: `SqliteSessionStore.delete_session` data lifecycle for orchestration bookkeeping

## Context

A session can own durable orchestration bookkeeping: Ultracode executions, Agent
Swarm runs, Leader attempts and decisions, task DAGs and their nodes, dependency
relays, recovery claims, planning and replan attempts and proposals, result
adoptions with targets, and parent context relays. Their foreign keys reference
`sessions(id)` with `ON DELETE RESTRICT`, which is deliberate: nothing may
silently destroy durable evidence.

`delete_session` only walked the subagent child tree and refused sessions that
still held a writable subagent lease. Every other restricting table was left to
SQLite, so deleting any session that had ever used those workflows failed with a
raw `sqlite3.IntegrityError: FOREIGN KEY constraint failed`. The TUI surfaced that
verbatim, and the session could not be removed at all.

## Decision

`delete_session` removes the orchestration records owned by the deleted session
subtree before it removes the session rows, inside the same transaction and in
child-first order: result-adoption targets, leader decisions, replan proposals,
planning proposals, recovery claims, leader attempts, replan attempts, planning
attempts, swarm runs, Ultracode executions, result adoptions, parent context
relays, then DAG-owned dependency relays and nodes, then the DAGs. Dag-scoped
tables are filtered by the DAGs the subtree owns, so a purge never touches
another session's records.

Two boundaries are unchanged:

- sessions that still hold a **writable subagent lease** are refused, because the
  lease owns on-disk workspace resources that need explicit cleanup;
- sessions that are currently open in a conversation are refused by the
  application layer, not the store.

Any remaining foreign-key failure is converted into a `SessionError` instead of
leaking a raw SQLite error. The whole deletion stays one transaction, so a
refusal leaves the database untouched.

The schema keeps `ON DELETE RESTRICT`. The purge is explicit application-side
knowledge rather than an implicit cascade, so a new restricting table fails
closed until it is handled deliberately.

## Consequences

- Deleting a session from the history now also deletes that session's
  orchestration evidence. This is intended: the user owns the session, and the
  alternative was an undeletable history entry.
- Published behavior of `delete_session` changes from "fails with a raw SQLite
  error for orchestrated sessions" to "removes the session and its owned
  orchestration records". No schema version change is involved.
- Cross-session references that the extraction rules do not cover fail closed:
  the transaction rolls back and the caller receives a `SessionError`.
- Verified against a copy of a production-shaped database: every session
  deletes successfully, and `PRAGMA foreign_key_check` reports no violations
  afterwards.
