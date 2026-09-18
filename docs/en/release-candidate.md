# Preview release candidate

[简体中文](../zh-CN/release-candidate.md) · **English**

R1 defines a reproducible local preview candidate for version `0.1.0a1`. The
release line uses the frozen build backend `hatchling==1.32.3`. It does not
publish a package, create a Git tag, or create a GitHub Release.

## Source development

Use a checkout for contributor work:

```bash
uv sync --extra dev --locked
uv run neuro
```

Source development is not release-installation proof. Do not use an editable
environment as evidence that a candidate artifact installs correctly.

## Build and verify a candidate

Start from a clean tracked commit and run:

```bash
uv sync --extra dev --locked
uv run python scripts/release_candidate.py
```

The command builds one wheel and one sdist with `uv build`, audits their actual
archive members, installs each into a temporary virtual environment outside
the checkout, exercises `python -m neuro_code`, `neuro`, and `neuro-code`, and
writes `dist/release-manifest.json`. The manifest records the exact source
commit, filenames, SHA-256 hashes, Python requirement, version, and build
backend provenance. `dist/`
is ignored and must not be committed.

The clean-install check removes checkout `PYTHONPATH`/environment leakage and
proves that the imported module belongs to the temporary environment. It does
not inspect `~/.neuro-code` or any user credential directory.

## Install a locally built candidate

After the command succeeds, a candidate can be tested from outside the
checkout with a fresh environment:

```bash
uv venv /tmp/neuro-code-preview
uv pip install --python /tmp/neuro-code-preview/bin/python dist/neuro_code-0.1.0a1-py3-none-any.whl
cd /tmp
/tmp/neuro-code-preview/bin/neuro-code --version
```

On Windows, use the `Scripts/python.exe` and `Scripts/neuro-code.exe` paths.
The sdist can be installed in the same way when a source-build check is
needed.

## Final engineering gate

Before human acceptance testing, dispatch the manual
`.github/workflows/release-readiness.yml` workflow from the reviewed main
commit. It performs focused Windows/macOS worktree stress and two independent
release-candidate builds, and records the build backend in the reproducibility
report. A passing gate is engineering evidence only: it does not create a
tag, publish a package, or create a GitHub Release.

## Future public installation

No public preview has been published yet. `pip install neuro-code`, `uv tool
install neuro-code`, and PyPI/TestPyPI publication are future release paths,
not current R1 claims.

## Future tag contract

When a release tag is introduced, it must be `v0.1.0a1` and must equal the
canonical package version. Branch and pull-request candidate builds do not
require a tag. R1 validates this contract without creating one.
