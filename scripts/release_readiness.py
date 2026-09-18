"""Prove that two independent release-candidate builds are byte-identical."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_candidate(source_root: Path, output_dir: Path) -> dict[str, object]:
    command = (
        sys.executable,
        str(source_root / "scripts" / "release_candidate.py"),
        "--output-dir",
        str(output_dir),
    )
    result = subprocess.run(
        command,
        cwd=source_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stdout + "\n" + result.stderr).strip()[-4000:]
        raise RuntimeError(f"release candidate build failed ({result.returncode}):\n{detail}")
    manifest_path = output_dir / "release-manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"release candidate did not write {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("release candidate manifest is not an object")
    return manifest


def _verify_manifest_files(output_dir: Path, manifest: Mapping[str, object]) -> None:
    expected_names = {"release-manifest.json"}
    for artifact_name in ("wheel", "sdist"):
        artifact = manifest.get(artifact_name)
        if not isinstance(artifact, dict):
            raise RuntimeError(f"manifest is missing {artifact_name} metadata")
        filename = artifact.get("filename")
        expected_hash = artifact.get("sha256")
        if not isinstance(filename, str) or not isinstance(expected_hash, str):
            raise RuntimeError(f"manifest has invalid {artifact_name} metadata")
        expected_names.add(filename)
        path = output_dir / filename
        if not path.is_file() or _sha256(path) != expected_hash:
            raise RuntimeError(f"{artifact_name} bytes do not match its manifest hash")

    actual_names = {path.name for path in output_dir.iterdir() if path.is_file()}
    if actual_names != expected_names:
        raise RuntimeError(f"candidate output contains unexpected files: {sorted(actual_names)}")


def _require_equal(first: Mapping[str, object], second: Mapping[str, object], key: str) -> None:
    if first.get(key) != second.get(key):
        raise RuntimeError(f"candidate manifests differ in {key}")


def compare_candidates(
    first_output: Path,
    first_manifest: Mapping[str, object],
    second_output: Path,
    second_manifest: Mapping[str, object],
) -> dict[str, object]:
    _verify_manifest_files(first_output, first_manifest)
    _verify_manifest_files(second_output, second_manifest)
    for key in ("project", "version", "source_commit_sha", "requires_python", "build_backend"):
        _require_equal(first_manifest, second_manifest, key)
    for key in ("wheel", "sdist"):
        _require_equal(first_manifest, second_manifest, key)

    wheel = first_manifest["wheel"]
    sdist = first_manifest["sdist"]
    if not isinstance(wheel, dict) or not isinstance(sdist, dict):
        raise RuntimeError("candidate manifest artifact metadata is invalid")
    return {
        "builds": 2,
        "source_commit_sha": first_manifest["source_commit_sha"],
        "version": first_manifest["version"],
        "build_backend": first_manifest["build_backend"],
        "wheel": {"filename": wheel["filename"], "sha256": wheel["sha256"]},
        "sdist": {"filename": sdist["filename"], "sha256": sdist["sha256"]},
        "byte_identical": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write the reproducibility report here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="neuro-code-release-readiness-") as raw:
        temporary_root = Path(raw)
        first_output = temporary_root / "candidate-one"
        second_output = temporary_root / "candidate-two"
        first_manifest = _run_candidate(source_root, first_output)
        second_manifest = _run_candidate(source_root, second_output)
        report = compare_candidates(first_output, first_manifest, second_output, second_manifest)

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
