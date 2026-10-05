# ADR 0196: DW1 Workflow IR and pure validator foundation

**English** · [简体中文](../../zh-CN/adr/0196-dw1-workflow-ir-and-pure-validator.md)

- Status: Accepted for the DW1 foundation
- Date: 2026-10-06
- Scope: immutable definitions only; **no workflow execution path exists yet**

## Context and boundaries

DW0 identified a need for bounded branching, iteration, and collection expansion
above immutable Task DAG batches. Existing DAG, Leader, Writable Worker, Profile,
Permission, Workspace, Sandbox, verification and recovery owners remain intact.
DW1 establishes data and validation contracts. It adds no Interpreter, Run,
scheduler, repository, migration, bootstrap wiring, model call, tool, prompt,
CLI/TUI entry or runtime event. It cannot publish a DAG or run an activity.

`neuro_code.domain.workflows.compile_workflow` accepts strict JSON and produces
an immutable `WorkflowDefinition` or `WorkflowValidationError.diagnostics`.
It performs no I/O. Direct typed definition construction receives the same
structural/dataflow validation. Nested values are frozen dataclasses and tuples;
mutable source/projection dictionaries never become part of the IR.

## Source and identity

Strict JSON is reviewable, deterministic and separates declarative data from
execution. Unknown fields, duplicate JSON keys, coercion, floats, NaN/Infinity,
unknown kinds and unsupported versions fail closed. No YAML, Python/JavaScript
expressions, `exec`, regex conditions, plugins or file discovery are supported.
Prompts are bounded opaque template text; DW1 does not interpret or substitute
them. Bindings are explicit typed data, not expression strings.

`version: 1`, `definition_id`, `input_schema`, `budget`, `steps` and
`completion_requirements` are required. Source and canonical output are bounded
at 128 KiB. Input schema is a small closed required-field tree: string, integer,
boolean, object, bounded array, or artifact. This is not general JSON Schema:
no optional/nullable fields, arbitrary unions, floating numbers, schemas from
plugins or validation execution. Objects require `properties`; arrays require
`items` and `max_items`. Container depth is bounded at eight.

Source is a declaration. IR is its validated normalized immutable meaning.
A future Run identity identifies an execution and is **not** a definition hash.
Canonical UTF-8 JSON uses sorted object keys, no ASCII escaping, compact
separators and no timestamps, UUIDs, reprs, paths inferred from the host or
locale. Step/task array order remains meaningful. Capability lists are sorted;
dependency sets follow task declaration order; bindings and path/schema names
are sorted. Omitted task `route` normalizes to `writable_subagent`.
SHA-256 of canonical bytes is the definition fingerprint. It detects meaningful
definition changes; it is not a grant, artifact-content check, run ID, freshness
proof, cryptographic signature or proof of successful execution.

## Five node kinds

| Kind | Declaration and completion contract | DW1 bounds |
|---|---|---|
| TaskBatch | `tasks`, `max_parallel`; join means the batch completes, no Join worker | Reuses Domain Task DAG constants: 8 nodes, 16 edges, 4 dependencies/task, parallel 1–4 |
| Activity | `activity`, typed `inputs`; future parent-owned intent only | `parent.adopt`, `parent.verify`, `parent.repair`; no handler exists |
| Branch | `condition`, exactly two named `paths`, distinct `then_path`/`else_path` covering both | Structured forward paths, not jumps or arbitrary target IDs |
| Repeat | `steps`, required `max_iterations`, post-body `until` | 1–3; no nested Repeat, including through Branch |
| Map | bounded `collection`, required `max_items`, one TaskBatch `batch` template | One flattened batch; `max_items × tasks <= 8`, template parallel <= 4; no Map/Repeat/nested expansion body |

Ordered `steps` supplies sequence. TaskBatch supplies parallelism; TaskBatch/Map
completion supplies join. No sixth node kind is introduced. A future runtime
must create distinct immutable DAG batches, not mutate a running DAG, and reuse
the existing scheduler. Map's declared ceiling must cover the collection schema
maximum: silently truncating an oversized collection is forbidden.

TaskSpec requires `task_id`, `prompt_template`, `profile_ref`,
`required_capabilities`, `inputs` and `depends_on`; `route` is optional in source.
Only the existing `TaskDagNodeKind.WRITABLE_SUBAGENT` route is admitted. Task IDs
are unique per batch; dependencies are batch-local and acyclic. All step IDs,
including Map template IDs and mutually exclusive branches, are globally unique.
Identifiers/profile names use bounded ASCII identifiers; CJK prompt/literal
content is preserved without Unicode normalization or newline conversion.

## Types, references and dominance

Bindings are tagged `literal`, `input`, `item`, `result` or `artifact` objects.
Literal values are bounded strings/integers/booleans. InputRef uses declared
input-schema paths. ItemRef is available only in Map TaskSpec bindings.
ResultRef has `step_id`, explicit `field_path` components and optional `iteration`
and `item_key`. Field paths select required object properties; they are not
expressions or unchecked array indexes. ArtifactRef contains `artifact_id`, a
lowercase SHA-256 `integrity_fingerprint`, and optional opaque
`workspace_identity`. Existing worktree/adoption identities are execution-owned,
so they are not repurposed as a generic artifact type. No artifact is opened.

Fixed output schemas make paths statically checkable:

- TaskBatch: `tasks.<task_id>.status` and `.response`.
- parent.adopt: `status`, `parent_workspace_changed`.
- parent.verify: `status`, `workspace_generation`.
- parent.repair: `status`, `response`.
- Repeat: `iterations`, `last.<guaranteed body step>.<field>`.
- Map: `count`, `items` (bounded array of batch results).
- Branch: no promoted result; branch-local results stay local.

These are **future projection contracts**, not claims that today's runtime
already produces them. Downstream adapters must validate actual outputs later.

Consumers can read only already-produced results that dominate them. Branch
outputs are not unconditionally available; even `exists` cannot disguise an
invalid dataflow. Repeat body outputs are visible to post-body `until`, but only
Repeat's `last` projection escapes the loop. Repeat is post-tested: at least one
body execution is required before successful continuation. Consequently an
explicit selector can name **iteration 1 of a completed Repeat**; later iterations
are not guaranteed. Map item-key selectors have no finite key-set proof in DW1
and are rejected. Optional output/key-selection proofs are deferred rather than
represented by runtime `None`.

Conditions are only `eq` and `exists`. Equality requires an exact scalar type
(boolean is not integer); no boolean scripting language exists. Conditions are
validated but **never evaluated** in DW1. Verification PASS/freshness, output
values and reference/artifact resolution are future runtime responsibilities.

## Profile, budgets and diagnostics

Profile/capability fields express intent only. Built-in non-worker role names
contradict the sole writable route. The compiler can receive an explicit immutable
`known_profiles` catalog of Domain AgentProfile values to check known capability
and role contradictions; it does not import the application catalog or discover
custom profiles. A syntactically valid unknown profile remains unverified intent.
Compilation does not establish provider/tool availability or grant permission.
Future grants remain the intersection of actual parent/global/Profile/runtime
capabilities with Permission, Workspace and Sandbox authorities.

Budget declaration requires positive strict integers for `max_generated_tasks`,
`max_model_calls`, `max_tool_calls`, `max_input_tokens`, `max_output_tokens`, and
`max_wall_seconds`. Generated tasks are capped at 32; other declarations at
2^31−1 as a representation bound, not an allocated quota. Total declarations are
capped at 64 and control nesting at eight. Worst-case generated work sums sequence,
uses maximum branch cost, multiplies Repeat by max iterations, and multiplies Map
by max items. It must fit the declared task ceiling. DW1 has no budget ledger,
reservation, spend, authority intersection or wall-clock measurement.

Diagnostics have stable `code`, JSON `path`, `expected`, and bounded `actual`.
Independent structural errors are collected before semantic validation; unsafe
recovery is not attempted. Duplicate JSON object-key errors use root `$` because
standard decoder hooks have no enclosing-path context. No traceback is an API.

## Evidence and deferred work

Deterministic fixtures cover all five kinds, fan-out/fan-in, mixed control flow,
CJK, key ordering, roundtrip and a pinned SHA-256. Pure contract tests live under
`tests/architecture/` so existing Fast CI checks the same golden fingerprint on
Linux, Windows and macOS / Python 3.12 and 3.14. They need no Runtime mocks.

DW2+ must define durable Run/step identities, atomic publication/CAS, validated
output projection, profile/runtime grants, ledger and verification freshness,
recovery, reference selection and parent activity adapters. No DW2+ capability is
implemented or enabled by this decision.
