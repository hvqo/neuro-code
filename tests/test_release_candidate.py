from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import tomllib
import unittest
import zipfile
from importlib.metadata import version as installed_version
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from packaging.version import Version
from scripts.release_candidate import (
    audit_artifact,
    create_manifest,
    sha256_file,
    validate_pep440_version,
    validate_tag_version,
    write_manifest,
)

import neuro_code
from neuro_code.interfaces.acp.negotiation import AcpConnectionState
from neuro_code.interfaces.cli.inspection import run_version_command


class ReleaseCandidateTests(unittest.TestCase):
    def test_one_canonical_pep440_version_reaches_package_metadata_and_cli(self) -> None:
        self.assertEqual(neuro_code.__version__, "0.1.0a1")
        self.assertEqual(str(Version(neuro_code.__version__)), "0.1.0a1")
        self.assertEqual(installed_version("neuro-code"), neuro_code.__version__)

        root = Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["dynamic"], ["version"])
        version_source = (root / "src/neuro_code/_version.py").read_text(encoding="utf-8")
        init_source = (root / "src/neuro_code/__init__.py").read_text(encoding="utf-8")
        self.assertEqual(version_source.count("__version__"), 1)
        self.assertNotIn("__version__:", init_source)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_version_command(argparse.Namespace(json=True)), 0)
        self.assertEqual(json.loads(output.getvalue())["version"], neuro_code.__version__)

    def test_acp_metadata_uses_canonical_version(self) -> None:
        async def initialize() -> str | None:
            state = AcpConnectionState(MagicMock())
            response = await state.initialize(1)
            return response.agent_info.version if response.agent_info is not None else None

        self.assertEqual(asyncio.run(initialize()), neuro_code.__version__)

    def test_manifest_contains_exact_hashes_and_commit(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "neuro_code-0.1.0a1-py3-none-any.whl"
            sdist = root / "neuro_code-0.1.0a1.tar.gz"
            wheel.write_bytes(b"wheel bytes")
            sdist.write_bytes(b"sdist bytes")
            manifest = create_manifest(
                version="0.1.0a1",
                source_commit_sha="a" * 40,
                requires_python=">=3.12",
                wheel=wheel,
                sdist=sdist,
            )
            manifest_path = root / "release-manifest.json"
            write_manifest(manifest_path, manifest)
            written = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(written["source_commit_sha"], "a" * 40)
            self.assertEqual(written["wheel"]["sha256"], sha256_file(wheel))
            self.assertEqual(written["sdist"]["sha256"], sha256_file(sdist))

    def test_artifact_audit_rejects_local_files_and_redacts_secret_matches(self) -> None:
        with TemporaryDirectory() as raw:
            archive_path = Path(raw) / "candidate.whl"
            secret = "sk-" + "A" * 24
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "outside the archive root")
                archive.writestr("neuro_code-0.1.0a1/.github/workflows/test.yml", "name: test\n")
                archive.writestr("neuro_code-0.1.0a1/.claude/settings.local.json", "{}\n")
                archive.writestr("neuro_code-0.1.0a1/.env", f"API_KEY={secret}\n")
                archive.writestr(
                    "neuro_code-0.1.0a1/keys/private.pem",
                    "-----BEGIN PRIVATE KEY-----\n" + "A" * 40 + "\n-----END PRIVATE KEY-----\n",
                )
                archive.writestr(
                    "neuro_code-0.1.0a1/package/config.py",
                    "Generated from /home/tester/.neuro-code/config.toml\n",
                )
                archive.writestr(
                    "neuro_code-0.1.0a1/docs/security.md", "secret and token are concepts"
                )
            findings = audit_artifact(archive_path)

        rules = {finding.rule for finding in findings}
        self.assertIn("unsafe-member-path", rules)
        self.assertIn("forbidden-local-file", rules)
        self.assertIn("forbidden-local-path", rules)
        self.assertIn("openai-token", rules)
        self.assertIn("pem-private-key", rules)
        self.assertIn("user-home-path", rules)
        self.assertNotIn(secret, repr(findings))

    def test_legitimate_security_words_do_not_trigger_secret_scan(self) -> None:
        with TemporaryDirectory() as raw:
            archive_path = Path(raw) / "candidate.whl"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "neuro_code-0.1.0a1/README.md",
                    "This documentation discusses secret and token boundaries.",
                )
                archive.writestr(
                    "neuro_code-0.1.0a1/tests/test_fixture.py",
                    'TOKEN = "sk-examplecredential123"\n',
                )
            findings = audit_artifact(archive_path)
        self.assertEqual(findings, ())

    def test_pep440_and_future_tag_contract(self) -> None:
        self.assertEqual(validate_pep440_version("0.1.0a1"), "0.1.0a1")
        with self.assertRaises(ValueError):
            validate_pep440_version("preview")
        validate_tag_version("v0.1.0a1", "0.1.0a1")
        with self.assertRaises(ValueError):
            validate_tag_version("0.1.0a1", "0.1.0a1")
