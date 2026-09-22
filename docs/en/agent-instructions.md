# Neuro Code agent instructions

[简体中文](../zh-CN/agent-instructions.md) · **English**

## Source of truth

- Neuro Code source code, tests, and executable behavior are the source of truth.
- Do not mechanically port external modules or synchronize files by path.
- Deliver independently designed, testable vertical user capabilities.

## Architecture

- Deliver vertical user capabilities through the canonical ports in
  `src/neuro_code/application/ports` (`neuro_code.application.ports.*`).
  New production code must use the canonical application ports paths.
- Domain/application code must not depend on UI, provider, database, or
  platform implementations.
- Preserve CLI/config/session/protocol compatibility at boundaries while using
  Python-native internals.
- Route side effects through permissions and workspace/platform adapters.
- Never weaken an explicitly requested sandbox or expose a credential.
- Keep identical Markdown file sets under `docs/en/` and `docs/zh-CN/`.

## Completion checks

Run all of the following for implementation changes:

```bash
uv run python scripts/check_all.py
```

`scripts/check_all.py` is the single source of truth for the completion checks
(lock check, documentation parity and consistency, Ruff, format, mypy, pytest
with coverage, and build); adding a gate means editing that script rather than
several hand-written copies. `--quick` skips the slow test run and build.

Update the compatibility matrix in both languages and relevant architecture or
ADR material whenever observable compatibility or a stable internal boundary
changes.
