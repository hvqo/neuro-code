"""Build and verify a local Neuro Code preview release candidate.

The command deliberately stops at local/CI artifact validation.  It does not
create tags, publish packages, or inspect user state directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

PROJECT_NAME = "neuro-code"
PACKAGE_NAME = "neuro_code"
WHEEL_PREFIX = "neuro_code-"
REQUIRED_MODULES = (
    "neuro_code/__init__.py",
    "neuro_code/_version.py",
    "neuro_code/__main__.py",
    "neuro_code/bootstrap/entrypoints.py",
)
REMOVED_MODULES = (
    "neuro_code/runtime",
    "neuro_code/ports",
    "neuro_code/errors",
    "neuro_code/async_utils",
    "neuro_code/redaction",
    "neuro_code/providers",
)
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".github",
        ".claude",
        ".idea",
        ".neuro-code",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".vscode",
        "benchmark-results",
        "build",
        "dist",
        "htmlcov",
        "__pycache__",
    }
)
PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----\s+"
    r"[A-Za-z0-9+/=\r\n]{32,}\s+"
    r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+"
    r"[A-Za-z0-9+/=\r\n]{32,}\s+"
    r"-----END CERTIFICATE-----"
)
USER_HOME_PATH = re.compile(
    r"(?:/(?:home|Users)/[^/\s]+/[^/\s]+|"
    r"[A-Za-z]:[\\/](?:Users|home)[\\/][^\\/\s]+[\\/][^\\/\s]+)",
    re.IGNORECASE,
)
TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai-token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
)
PLACEHOLDER_TOKEN_MARKERS = (
    "dummy",
    "example",
    "fake",
    "placeholder",
    "sample",
    "test",
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)"
    r"\s*[:=]\s*[\"']?([^\s\"'#]{12,})"
)
CREDENTIAL_PARTS = frozenset({"credential", "credentials", "secret", "secrets"})


@dataclass(frozen=True)
class ArtifactFinding:
    """A redacted artifact safety finding."""

    artifact: str
    member: str
    rule: str


@dataclass(frozen=True)
class ArtifactMetadata:
    """Metadata extracted from one wheel or source archive."""

    name: str
    version: str
    requires_python: str | None
    members: tuple[str, ...]


def canonical_version(source_root: Path) -> str:
    """Read the version authority without importing the source package."""

    namespace = {}
    version_path = source_root / "src" / PACKAGE_NAME / "_version.py"
    exec(compile(version_path.read_bytes(), str(version_path), "exec"), namespace)
    value = namespace.get("__version__")
    if not isinstance(value, str):
        raise RuntimeError("canonical version module does not define __version__")
    return value


def validate_pep440_version(value: str) -> str:
    """Return a canonical PEP 440 version or raise a bounded error."""

    try:
        parsed = Version(value)
    except InvalidVersion as error:
        raise ValueError(f"invalid PEP 440 version: {value!r}") from error
    normalized = str(parsed)
    if normalized != value:
        raise ValueError(f"version is not canonical PEP 440 spelling: {value!r}")
    return normalized


def validate_tag_version(tag: str, version: str) -> None:
    """Enforce the future release tag convention without creating a tag."""

    expected = f"v{version}"
    if tag != expected:
        raise ValueError(f"tag {tag!r} does not match expected release tag {expected!r}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_members(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                name.replace("\\", "/"): archive.read(name)
                for name in archive.namelist()
                if not name.endswith("/")
            }
    if path.name.endswith(".tar.gz"):
        members: dict[str, bytes] = {}
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = member.name.replace("\\", "/")
                if member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        members[name] = extracted.read()
                else:
                    members[name] = b""
        return members
    raise ValueError(f"unsupported release artifact: {path.name}")


def _is_credential_member(member: str) -> bool:
    path = PurePosixPath(member)
    lowered_parts = {part.lower() for part in path.parts}
    basename = path.name.lower()
    return bool(
        lowered_parts & CREDENTIAL_PARTS
        or basename in {".env", "credentials.json", "credentials.toml", "secrets.toml"}
    )


def scan_member(artifact: str, member: str, content: bytes) -> tuple[ArtifactFinding, ...]:
    """Find high-confidence secrets without returning matched values."""

    text = content.decode("utf-8", errors="ignore")
    findings: list[ArtifactFinding] = []
    if PEM_PRIVATE_KEY.search(text):
        findings.append(ArtifactFinding(artifact, member, "pem-private-key"))
    if PEM_CERTIFICATE.search(text):
        findings.append(ArtifactFinding(artifact, member, "pem-certificate"))
    archive_parts = PurePosixPath(member).parts
    if USER_HOME_PATH.search(text) and not {"docs", "tests"}.intersection(archive_parts):
        findings.append(ArtifactFinding(artifact, member, "user-home-path"))
    for rule, pattern in TOKEN_PATTERNS:
        if any(
            not any(marker in match.group(0).lower() for marker in PLACEHOLDER_TOKEN_MARKERS)
            for match in pattern.finditer(text)
        ):
            findings.append(ArtifactFinding(artifact, member, rule))
    if _is_credential_member(member) and CREDENTIAL_ASSIGNMENT.search(text):
        findings.append(ArtifactFinding(artifact, member, "credential-assignment"))
    return tuple(findings)


def audit_artifact(path: Path) -> tuple[ArtifactFinding, ...]:
    """Audit actual archive members for forbidden local content and secrets."""

    findings: list[ArtifactFinding] = []
    for member, content in _archive_members(path).items():
        normalized = member.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        basename = PurePosixPath(normalized).name.lower()
        if normalized.startswith("/") or ".." in parts:
            findings.append(ArtifactFinding(path.name, normalized, "unsafe-member-path"))
        if FORBIDDEN_PARTS.intersection(parts):
            findings.append(ArtifactFinding(path.name, normalized, "forbidden-local-path"))
        if (
            basename == ".env"
            or basename.startswith(".env.")
            or basename in {"credentials.json", "credentials.toml", "secrets.toml"}
            or basename in {".coverage", "coverage.xml"}
            or basename.endswith(
                (".db", ".sqlite", ".sqlite3", ".pyc", ".tmp", ".swp", ".log", "~")
            )
            or basename in {"junit.xml", "test-results.xml"}
        ):
            findings.append(ArtifactFinding(path.name, normalized, "forbidden-local-file"))
        findings.extend(scan_member(path.name, normalized, content))
    return tuple(findings)


def _member_ending(members: Sequence[str], suffix: str) -> str | None:
    matches = [member for member in members if member.endswith(suffix)]
    if len(matches) != 1:
        return None
    return matches[0]


def _module_present(members: Sequence[str], module: str) -> bool:
    module_path = module.replace(".", "/")
    return any(
        member == f"{module_path}.py"
        or member.endswith((f"/{module_path}.py", f"/{module_path}/__init__.py"))
        for member in members
    )


def _required_module_present(members: Sequence[str], module_path: str) -> bool:
    return any(
        member == module_path or member.endswith(f"/src/{module_path}") for member in members
    )


def _artifact_metadata(path: Path) -> ArtifactMetadata:
    members = _archive_members(path)
    metadata_name = _member_ending(tuple(members), ".dist-info/METADATA")
    if metadata_name is None:
        metadata_name = _member_ending(tuple(members), "PKG-INFO")
    if metadata_name is None:
        raise ValueError(f"{path.name} does not contain package metadata")
    metadata = Parser().parsestr(members[metadata_name].decode("utf-8"))
    return ArtifactMetadata(
        name=metadata.get("Name", ""),
        version=metadata.get("Version", ""),
        requires_python=metadata.get("Requires-Python"),
        members=tuple(members),
    )


def validate_artifacts(
    wheel: Path,
    sdist: Path,
    *,
    version: str,
    requires_python: str,
) -> tuple[ArtifactMetadata, ArtifactMetadata]:
    """Validate metadata, runtime modules, entry points, and license files."""

    if not wheel.name.startswith(f"{WHEEL_PREFIX}{version}-") or not wheel.name.endswith(".whl"):
        raise ValueError(f"wheel filename does not contain version {version}: {wheel.name}")
    if not sdist.name.startswith(f"{WHEEL_PREFIX}{version}") or not sdist.name.endswith(".tar.gz"):
        raise ValueError(f"sdist filename does not contain version {version}: {sdist.name}")

    wheel_metadata = _artifact_metadata(wheel)
    sdist_metadata = _artifact_metadata(sdist)
    if (wheel_metadata.name, wheel_metadata.version) != (PROJECT_NAME, version):
        raise ValueError(f"wheel metadata mismatch: {wheel_metadata}")
    if (sdist_metadata.name, sdist_metadata.version) != (PROJECT_NAME, version):
        raise ValueError(f"sdist metadata mismatch: {sdist_metadata}")
    if wheel_metadata.requires_python != requires_python:
        raise ValueError("wheel Requires-Python does not match pyproject.toml")
    if sdist_metadata.requires_python != requires_python:
        raise ValueError("sdist Requires-Python does not match pyproject.toml")

    for metadata in (wheel_metadata, sdist_metadata):
        for module in REQUIRED_MODULES:
            if not _required_module_present(metadata.members, module):
                raise ValueError(f"{metadata.name} is missing required module {module}")
        for removed in REMOVED_MODULES:
            if _module_present(metadata.members, removed):
                raise ValueError(f"{metadata.name} contains removed compatibility module {removed}")
        if not any(PurePosixPath(member).name == "LICENSE" for member in metadata.members):
            raise ValueError(f"{metadata.name} is missing LICENSE")
        if not any(PurePosixPath(member).name == "NOTICE" for member in metadata.members):
            raise ValueError(f"{metadata.name} is missing NOTICE")

    entry_points_name = _member_ending(wheel_metadata.members, ".dist-info/entry_points.txt")
    if entry_points_name is None:
        raise ValueError("wheel is missing console-script entry points")
    entry_text = _archive_members(wheel)[entry_points_name].decode("utf-8")
    required_entry_points = {
        "neuro = neuro_code.bootstrap.entrypoints:main",
        "neuro-code = neuro_code.bootstrap.entrypoints:main",
    }
    if not required_entry_points.issubset({line.strip() for line in entry_text.splitlines()}):
        raise ValueError("wheel console-script entry points are incomplete")
    return wheel_metadata, sdist_metadata


def create_manifest(
    *,
    version: str,
    source_commit_sha: str,
    requires_python: str,
    wheel: Path,
    sdist: Path,
    build_backend: Mapping[str, str],
) -> dict[str, object]:
    """Create the deterministic candidate provenance manifest."""

    backend_name = build_backend.get("name")
    backend_version = build_backend.get("version")
    if not backend_name or not backend_version:
        raise ValueError("build backend provenance must include name and version")
    return {
        "project": PROJECT_NAME,
        "version": version,
        "source_commit_sha": source_commit_sha,
        "requires_python": requires_python,
        "build_backend": {"name": backend_name, "version": backend_version},
        "wheel": {"filename": wheel.name, "sha256": sha256_file(wheel)},
        "sdist": {"filename": sdist.name, "sha256": sha256_file(sdist)},
    }


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(command: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _capture(command: Sequence[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stdout + "\n" + result.stderr).strip()[-4000:]
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result.stdout


def _clean_install_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _venv_python(venv: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return venv / directory / executable


def verify_clean_install(artifact: Path, *, source_root: Path, version: str) -> None:
    """Install one candidate outside the checkout and exercise all entrypoints."""

    environment = _clean_install_environment()
    with tempfile.TemporaryDirectory(prefix="neuro-code-release-install-") as raw:
        temporary_root = Path(raw).resolve()
        outside = temporary_root / "outside-source"
        outside.mkdir()
        venv = temporary_root / "venv"
        _run(("uv", "venv", "--python", sys.executable, str(venv)), cwd=outside, env=environment)
        python = _venv_python(venv)
        _run(
            ("uv", "pip", "install", "--python", str(python), str(artifact)),
            cwd=outside,
            env=environment,
        )

        probe = _capture(
            (
                str(python),
                "-c",
                "import importlib.metadata as m, json, neuro_code, sys; "
                "print(json.dumps({'version': m.version('neuro-code'), "
                "'module': neuro_code.__file__, 'prefix': sys.prefix}))",
            ),
            cwd=outside,
            env=environment,
        )
        details = json.loads(probe)
        if details["version"] != version:
            raise RuntimeError(f"installed metadata version mismatch for {artifact.name}")
        imported_path = Path(details["module"]).resolve()
        if source_root.resolve() == imported_path or source_root.resolve() in imported_path.parents:
            raise RuntimeError(f"installed smoke imported source checkout: {imported_path}")
        if Path(details["prefix"]).resolve() != venv.resolve():
            raise RuntimeError(f"installed smoke used the wrong environment: {details['prefix']}")

        _capture((str(python), "-m", "neuro_code", "--help"), cwd=outside, env=environment)
        version_output = _capture(
            (str(python), "-m", "neuro_code", "--version"), cwd=outside, env=environment
        )
        if version not in version_output:
            raise RuntimeError(f"module --version output mismatch: {version_output!r}")
        scripts_directory = python.parent
        suffix = ".exe" if os.name == "nt" else ""
        for script_name in ("neuro", "neuro-code"):
            script = scripts_directory / f"{script_name}{suffix}"
            _capture((str(script), "--help"), cwd=outside, env=environment)
            script_version = _capture((str(script), "--version"), cwd=outside, env=environment)
            if version not in script_version:
                raise RuntimeError(f"{script_name} --version output mismatch: {script_version!r}")


def _source_commit(source_root: Path) -> str:
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=normal"),
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        raise RuntimeError("release candidate requires a clean tracked working tree")
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _project_metadata(source_root: Path) -> tuple[str, str]:
    with (source_root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return str(project["requires-python"]), str(project["name"])


def read_build_backend(source_root: Path) -> dict[str, str]:
    """Read and validate the exact Hatchling pin used for release builds."""

    with (source_root / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    build_system = document.get("build-system")
    if not isinstance(build_system, dict):
        raise ValueError("pyproject.toml has no build-system table")
    if build_system.get("build-backend") != "hatchling.build":
        raise ValueError("release candidates require the hatchling.build backend")

    raw_requirements = build_system.get("requires")
    if not isinstance(raw_requirements, list) or not all(
        isinstance(requirement, str) for requirement in raw_requirements
    ):
        raise ValueError("build-system.requires must be a list of requirement strings")

    hatchling_requirements: list[Requirement] = []
    for raw_requirement in raw_requirements:
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement as error:
            raise ValueError(f"invalid build-system requirement: {raw_requirement!r}") from error
        if requirement.name.lower() == "hatchling":
            hatchling_requirements.append(requirement)

    if len(hatchling_requirements) != 1:
        raise ValueError("release candidates require exactly one Hatchling requirement")
    requirement = hatchling_requirements[0]
    if requirement.extras or requirement.marker is not None:
        raise ValueError("Hatchling release provenance does not support extras or markers")

    specifiers = tuple(requirement.specifier)
    if len(specifiers) != 1 or specifiers[0].operator != "==":
        raise ValueError("release candidates require an exact Hatchling pin")
    version = specifiers[0].version
    validate_pep440_version(version)
    return {"name": requirement.name.lower(), "version": version}


def _build_artifacts(source_root: Path, output_dir: Path, version: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="neuro-code-release-build-") as raw:
        build_dir = Path(raw)
        _run(("uv", "build", "--out-dir", str(build_dir)), cwd=source_root)
        wheels = sorted(build_dir.glob(f"{WHEEL_PREFIX}{version}-*.whl"))
        sdists = sorted(build_dir.glob(f"{WHEEL_PREFIX}{version}.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise RuntimeError(
                f"expected one wheel and one sdist, found {len(wheels)} wheels and {len(sdists)} sdists"
            )
        wheel = output_dir / wheels[0].name
        sdist = output_dir / sdists[0].name
        shutil.copy2(wheels[0], wheel)
        shutil.copy2(sdists[0], sdist)
        return wheel, sdist


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--tag", help="validate a future release tag without creating it")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_root = Path(__file__).resolve().parents[1]
    version = validate_pep440_version(canonical_version(source_root))
    requires_python, project_name = _project_metadata(source_root)
    build_backend = read_build_backend(source_root)
    if project_name != PROJECT_NAME:
        raise RuntimeError(f"unexpected project name: {project_name}")
    tag = args.tag
    if tag is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")
    if tag is not None:
        validate_tag_version(tag, version)
    source_commit_sha = _source_commit(source_root)
    output_dir = args.output_dir.resolve()
    wheel, sdist = _build_artifacts(source_root, output_dir, version)
    for artifact in (wheel, sdist):
        findings = audit_artifact(artifact)
        if findings:
            details = "; ".join(
                f"{finding.artifact}:{finding.member}:{finding.rule}" for finding in findings
            )
            raise RuntimeError(f"artifact safety audit failed: {details}")
    validate_artifacts(
        wheel,
        sdist,
        version=version,
        requires_python=requires_python,
    )
    verify_clean_install(wheel, source_root=source_root, version=version)
    verify_clean_install(sdist, source_root=source_root, version=version)
    manifest = create_manifest(
        version=version,
        source_commit_sha=source_commit_sha,
        requires_python=requires_python,
        wheel=wheel,
        sdist=sdist,
        build_backend=build_backend,
    )
    manifest_path = output_dir / "release-manifest.json"
    write_manifest(manifest_path, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"release manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
