"""Run the R2 installed-artifact golden path.

The harness is intentionally an outer release test.  It imports no Neuro Code
modules: the product process under test is started only from a fresh virtual
environment containing the supplied wheel.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import venv
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

VERSION = "0.1.0a1"
WHEEL_NAME = f"neuro_code-{VERSION}-py3-none-any.whl"
TARGET_FILE = "miniapp.py"
VERIFICATION_COMMAND = "python -m pytest tests/test_miniapp.py"
FIXTURE_KEY = "r2-loopback-fixture-key"
PRODUCT_TIMEOUT_SECONDS = 90.0


class GoldenPathError(RuntimeError):
    """A bounded failure in the installed-artifact acceptance path."""


def _text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "".join(
            _text_content(item.get("text", "") if isinstance(item, Mapping) else item)
            for item in value
        )
    return ""


def _assistant_calls(messages: Sequence[object]) -> dict[str, tuple[str, dict[str, object]]]:
    calls: dict[str, tuple[str, dict[str, object]]] = {}
    for raw_message in messages:
        if not isinstance(raw_message, Mapping) or raw_message.get("role") != "assistant":
            continue
        raw_calls = raw_message.get("tool_calls")
        if not isinstance(raw_calls, Sequence):
            continue
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                continue
            call_id = raw_call.get("id")
            function = raw_call.get("function")
            if not isinstance(call_id, str) or not isinstance(function, Mapping):
                continue
            name = function.get("name")
            raw_arguments = function.get("arguments", "{}")
            if not isinstance(name, str) or not isinstance(raw_arguments, str):
                continue
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                continue
            if isinstance(arguments, dict):
                calls[call_id] = (name, arguments)
    return calls


def _completed_call(
    messages: Sequence[object],
    *,
    name: str,
    call_id: str,
) -> str:
    calls = _assistant_calls(messages)
    call = calls.get(call_id)
    if call is None or call[0] != name:
        raise GoldenPathError(f"provider did not receive the expected {name} call")
    for raw_message in messages:
        if not isinstance(raw_message, Mapping) or raw_message.get("role") != "tool":
            continue
        if raw_message.get("tool_call_id") == call_id:
            content = _text_content(raw_message.get("content"))
            if not content:
                raise GoldenPathError(f"{name} returned an empty tool result")
            return content
    raise GoldenPathError(f"provider did not receive the result for {name}")


def _sse_response(
    request_number: int,
    *,
    text: str | None = None,
    tool_name: str | None = None,
    tool_id: str | None = None,
    arguments: Mapping[str, object] | None = None,
) -> bytes:
    if (text is None) == (tool_name is None):
        raise ValueError("fixture response must be either text or a tool call")
    response_id = f"r2-fixture-{request_number}"
    if text is not None:
        delta: dict[str, object] = {"content": text}
        finish_reason = "stop"
    else:
        if tool_name is None or tool_id is None or arguments is None:
            raise ValueError("tool-call responses require a name, ID, and arguments")
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": tool_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(dict(arguments), separators=(",", ":")),
                    },
                }
            ]
        }
        finish_reason = "tool_calls"
    chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "r2-fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    finished = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "r2-fixture-model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    usage = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "r2-fixture-model",
        "choices": [],
        "usage": {"prompt_tokens": 32, "completion_tokens": 8, "total_tokens": 40},
    }
    lines = [
        f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n",
        f"data: {json.dumps(finished, separators=(',', ':'))}\n\n",
        f"data: {json.dumps(usage, separators=(',', ':'))}\n\n",
        "data: [DONE]\n\n",
    ]
    return "".join(lines).encode("utf-8")


@dataclass
class _FixtureState:
    workspace: Path
    requests: list[dict[str, object]] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    failure: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def handle(self, payload: Mapping[str, object], authorization: str | None) -> bytes:
        with self._lock:
            try:
                return self._handle(payload, authorization)
            except Exception as error:
                self.failure = f"{type(error).__name__}: {error}"
                raise

    def _handle(self, payload: Mapping[str, object], authorization: str | None) -> bytes:
        if authorization != f"Bearer {FIXTURE_KEY}":
            raise GoldenPathError("loopback provider did not receive the configured dummy key")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise GoldenPathError("provider request did not contain a message list")
        self.requests.append(dict(payload))
        request_number = len(self.requests)
        if request_number == 1:
            user_text = "\n".join(
                _text_content(message.get("content"))
                for message in messages
                if isinstance(message, Mapping) and message.get("role") == "user"
            )
            if TARGET_FILE not in user_text or "new" not in user_text:
                raise GoldenPathError("first model request lost the realistic coding task")
            tools = payload.get("tools")
            if not isinstance(tools, list) or not any(
                isinstance(tool, Mapping)
                and isinstance(tool.get("function"), Mapping)
                and tool["function"].get("name") == "git_inspect"
                for tool in tools
            ):
                raise GoldenPathError("first model request did not expose git_inspect")
            self.stages.append("model_request")
            self.stages.append("inspection_tool_call")
            return _sse_response(
                request_number,
                tool_name="git_inspect",
                tool_id="inspect-1",
                arguments={"view": "all"},
            )

        if request_number == 2:
            inspection = _completed_call(messages, name="git_inspect", call_id="inspect-1")
            if str(self.workspace) not in inspection or '"view": "all"' not in inspection:
                raise GoldenPathError("inspection result was not delivered to the model")
            self.stages.extend(("inspection_result", "edit_tool_call"))
            patch = """*** Begin Patch
*** Update File: miniapp.py
@@
-    return \"old\"
+    return \"new\"
*** End Patch
"""
            return _sse_response(
                request_number,
                tool_name="apply_patch",
                tool_id="edit-1",
                arguments={"patch": patch},
            )

        user_texts = [
            _text_content(message.get("content"))
            for message in messages
            if isinstance(message, Mapping) and message.get("role") == "user"
        ]
        resumed = any("after restart" in text.casefold() for text in user_texts)
        calls = _assistant_calls(messages)
        if resumed:
            _completed_call(messages, name="apply_patch", call_id="edit-1")
            verification_call_ids = [
                call_id
                for call_id, (name, arguments) in calls.items()
                if name == "bash" and arguments.get("command") == VERIFICATION_COMMAND
            ]
            if not verification_call_ids:
                raise GoldenPathError("resumed context lost the verification command")
            _completed_call(messages, name="bash", call_id=verification_call_ids[-1])
            if not payload.get("tools"):
                if not any(
                    isinstance(message, Mapping)
                    and message.get("role") == "system"
                    and "final response for the user" in _text_content(message.get("content"))
                    for message in messages
                ):
                    raise GoldenPathError("resumed finalizer lost its bounded instruction")
                self.stages.append("resumed_final_assistant_response")
            else:
                self.stages.append("resumed_model_response")
            return _sse_response(
                request_number,
                text="The completed edit and successful verification remain available after restart.",
            )

        _completed_call(messages, name="apply_patch", call_id="edit-1")
        if not payload.get("tools"):
            verification_call_ids = [
                call_id
                for call_id, (name, arguments) in calls.items()
                if name == "bash" and arguments.get("command") == VERIFICATION_COMMAND
            ]
            if not verification_call_ids:
                raise GoldenPathError(
                    "automatic verification command was not represented in finalizer context"
                )
            verification_result = _completed_call(
                messages, name="bash", call_id=verification_call_ids[-1]
            )
            if "1 passed" not in verification_result:
                raise GoldenPathError("automatic verification did not pass")
            if not any(
                isinstance(message, Mapping)
                and message.get("role") == "system"
                and "final response for the user" in _text_content(message.get("content"))
                for message in messages
            ):
                raise GoldenPathError("verification evidence did not reach the finalizer boundary")
            self.stages.extend(("verification_result", "final_assistant_response"))
            return _sse_response(
                request_number,
                text=(
                    'Changed miniapp.py so answer() returns "new". '
                    f"Verification: {VERIFICATION_COMMAND} (1 passed)."
                ),
            )

        self.stages.append("model_response")
        return _sse_response(
            request_number,
            text="The requested edit is applied; prepare the final verified response.",
        )


class _FixtureHTTPServer(http.server.ThreadingHTTPServer):
    fixture: _FixtureState


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    server: _FixtureHTTPServer

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(http.server.HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise GoldenPathError("provider request body was not a JSON object")
            response = self.server.fixture.handle(body, self.headers.get("Authorization"))
        except Exception as error:
            self.server.fixture.failure = f"{type(error).__name__}: {error}"
            self.send_error(http.server.HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
            return
        self.send_response(http.server.HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _LoopbackFixture:
    def __init__(self, workspace: Path) -> None:
        self.state = _FixtureState(workspace)
        self.server = _FixtureHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.server.fixture = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise GoldenPathError("loopback provider server thread did not stop")


def _run(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(command),
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    description: str,
) -> subprocess.CompletedProcess[str]:
    result = _run(command, cwd=cwd, env=env)
    if result.returncode != 0:
        raise GoldenPathError(
            f"{description} failed with exit {result.returncode}: {result.stderr.strip()[-2_000:]}"
        )
    return result


def _product_environment(*, home: Path, state: Path, venv_bin: Path) -> dict[str, str]:
    scrubbed = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "NEURO_CODE_CC_SWITCH_CONFIG",
        "NEURO_CODE_PROVIDER",
        "NEURO_CODE_MODEL",
        "NEURO_CODE_BASE_URL",
        "NEURO_CODE_SANDBOX",
    ):
        scrubbed.pop(name, None)
    scrubbed.update(
        {
            "HOME": str(home),
            "NEURO_CODE_HOME": str(state),
            "R2_FIXTURE_KEY": FIXTURE_KEY,
            "PATH": os.pathsep.join((str(venv_bin), scrubbed.get("PATH", ""))),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return scrubbed


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_bin(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _create_environment(venv_dir: Path, wheel: Path, *, repo: Path, env: Mapping[str, str]) -> Path:
    uv = shutil.which("uv")
    if uv is not None:
        _run_checked(
            (uv, "venv", "--python", sys.executable, str(venv_dir)),
            cwd=repo,
            env=env,
            description="create external virtual environment",
        )
        _run_checked(
            (
                uv,
                "pip",
                "install",
                "--python",
                str(_venv_python(venv_dir)),
                str(wheel),
                "pytest",
            ),
            cwd=repo,
            env=env,
            description="install candidate wheel and pytest",
        )
    else:
        builder = venv.EnvBuilder(with_pip=True, clear=True)
        builder.create(venv_dir)
        _run_checked(
            (_venv_python(venv_dir), "-m", "pip", "install", str(wheel), "pytest"),
            cwd=repo,
            env=env,
            description="install candidate wheel and pytest",
        )
    product_python = _venv_python(venv_dir)
    if not product_python.is_file():
        raise GoldenPathError(f"external environment Python was not created: {product_python}")
    return product_python


def _write_config(state: Path, base_url: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "config.toml").write_text(
        f"""[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
service_id = "generic-openai-compatible"
model = "r2-fixture-model"
base_url = "{base_url}"
auth = "env"
api_key_env = "R2_FIXTURE_KEY"
proxy_mode = "direct"
context_window_tokens = 131072
max_output_tokens = 512
""",
        encoding="utf-8",
    )


def _initialize_repository(repo: Path, env: Mapping[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / TARGET_FILE).write_text(
        'def answer() -> str:\n    return "old"\n',
        encoding="utf-8",
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_miniapp.py").write_text(
        'from miniapp import answer\n\n\ndef test_answer() -> None:\n    assert answer() == "new"\n',
        encoding="utf-8",
    )
    _run_checked(
        ("git", "init", "--initial-branch=main"),
        cwd=repo,
        env=env,
        description="init Git repository",
    )
    _run_checked(
        ("git", "config", "user.name", "R2 Golden Path"),
        cwd=repo,
        env=env,
        description="configure Git user",
    )
    _run_checked(
        ("git", "config", "user.email", "r2-golden-path@example.invalid"),
        cwd=repo,
        env=env,
        description="configure Git email",
    )
    _run_checked(
        ("git", "add", TARGET_FILE, "tests/test_miniapp.py"),
        cwd=repo,
        env=env,
        description="stage golden repository",
    )
    _run_checked(
        ("git", "commit", "-m", "fixture: add golden path project"),
        cwd=repo,
        env=env,
        description="commit golden repository",
    )
    status = _run_checked(
        ("git", "status", "--porcelain"), cwd=repo, env=env, description="check initial Git status"
    )
    if status.stdout.strip():
        raise GoldenPathError("golden repository was not clean before the agent turn")


def _import_proof(
    product_python: Path, *, repo: Path, env: Mapping[str, str], checkout: Path
) -> dict[str, object]:
    code = (
        "import importlib.metadata, json, pathlib, sys, neuro_code; "
        "distribution = importlib.metadata.distribution('neuro-code'); "
        "files = distribution.files or (); "
        "editable = any("
        "json.loads(distribution.locate_file(path).read_text()).get('dir_info', {}).get('editable') "
        "is True for path in files if str(path).endswith('direct_url.json')); "
        "print(json.dumps({'version': neuro_code.__version__, 'metadata_version': distribution.version, "
        "'file': str(pathlib.Path(neuro_code.__file__).resolve()), "
        "'sys_path': [str(pathlib.Path(item).resolve()) for item in sys.path if item], "
        "'editable': editable}, "
        "sort_keys=True))"
    )
    result = _run_checked(
        (product_python, "-c", code), cwd=repo, env=env, description="prove installed import"
    )
    try:
        proof = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GoldenPathError("installed import proof was not JSON") from error
    if proof.get("version") != VERSION or proof.get("metadata_version") != VERSION:
        raise GoldenPathError(f"installed package version mismatch: {proof}")
    product_file = Path(cast(str, proof.get("file"))).resolve()
    if product_file.is_relative_to(checkout) or not product_file.is_relative_to(
        product_python.parent.parent
    ):
        raise GoldenPathError(f"installed package resolves into the checkout: {product_file}")
    source_path = (checkout / "src").resolve()
    resolved_sys_path = tuple(Path(item).resolve() for item in cast(list[str], proof["sys_path"]))
    if any(item == source_path or source_path in item.parents for item in resolved_sys_path):
        raise GoldenPathError("source checkout src/ leaked onto the installed sys.path")
    if proof.get("editable"):
        raise GoldenPathError("candidate wheel appears to be an editable installation")
    return {
        "package_file": str(product_file),
        "metadata_version": proof["metadata_version"],
        "source_path_on_sys_path": False,
        "editable_install": False,
        "product_python": str(product_python.resolve()),
    }


def _product_executable(venv_dir: Path) -> Path:
    name = "neuro.exe" if os.name == "nt" else "neuro"
    executable = _venv_bin(venv_dir) / name
    if not executable.is_file():
        raise GoldenPathError(f"installed Neuro Code executable was not created: {executable}")
    return executable


def _run_product(
    executable: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> tuple[dict[str, object], str, str]:
    process = subprocess.Popen(
        (str(executable), *arguments),
        cwd=cwd,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=PRODUCT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            stdout, stderr = process.communicate()
        raise GoldenPathError(
            f"installed CLI timed out: {error}; stderr={stderr[-2_000:]}"
        ) from error
    if process.returncode != 0:
        raise GoldenPathError(
            f"installed CLI failed with exit {process.returncode}: {stderr.strip()[-4_000:]}"
        )
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise GoldenPathError(f"installed CLI did not emit exactly one JSON result: {stdout!r}")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise GoldenPathError(f"installed CLI output was not JSON: {stdout!r}") from error
    if not isinstance(payload, dict):
        raise GoldenPathError("installed CLI JSON result was not an object")
    return payload, stdout, stderr


def _run_public_json_command(
    executable: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> object:
    result = _run_checked(
        (executable, *arguments), cwd=cwd, env=env, description="public CLI command"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GoldenPathError(
            f"public CLI command did not return JSON: {result.stdout!r}"
        ) from error


def _assert_first_result(payload: Mapping[str, object]) -> str:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise GoldenPathError("first CLI result did not return a session identity")
    if payload.get("response") != (
        'Changed miniapp.py so answer() returns "new". '
        f"Verification: {VERIFICATION_COMMAND} (1 passed)."
    ):
        raise GoldenPathError(
            "first CLI final response did not come from the fixture finalizer: "
            f"response={payload.get('response')!r}, steps={payload.get('steps')!r}, "
            f"events={[event.get('kind') for event in payload.get('events', []) if isinstance(event, Mapping)]!r}"
        )
    outcome = payload.get("outcome")
    if outcome is not None and (
        not isinstance(outcome, Mapping) or outcome.get("status") != "completed"
    ):
        raise GoldenPathError(
            f"first CLI turn was not completed: outcome={outcome!r}, "
            f"response={payload.get('response')!r}, steps={payload.get('steps')!r}"
        )
    events = payload.get("events")
    if (
        not isinstance(events, list)
        or sum(
            isinstance(event, Mapping) and event.get("kind") == "turn_completed" for event in events
        )
        != 1
    ):
        raise GoldenPathError("first CLI result did not contain exactly one committed turn")
    return session_id


def _assert_second_result(payload: Mapping[str, object], session_id: str) -> None:
    if payload.get("session_id") != session_id:
        raise GoldenPathError("resume created a different session")
    if payload.get("response") != (
        "The completed edit and successful verification remain available after restart."
    ):
        raise GoldenPathError("second CLI response did not prove durable continuity")
    events = payload.get("events")
    if (
        not isinstance(events, list)
        or sum(
            isinstance(event, Mapping) and event.get("kind") == "turn_completed" for event in events
        )
        != 1
    ):
        raise GoldenPathError("second CLI result did not contain exactly one committed turn")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_workspace(repo: Path, env: Mapping[str, str]) -> dict[str, object]:
    target = (repo / TARGET_FILE).read_text(encoding="utf-8")
    expected = 'def answer() -> str:\n    return "new"\n'
    if target != expected:
        raise GoldenPathError("agent did not apply the expected source edit")
    diff = _run_checked(
        ("git", "diff", "--", TARGET_FILE), cwd=repo, env=env, description="inspect final Git diff"
    )
    expected_lines = ('-    return "old"', '+    return "new"')
    if any(line not in diff.stdout.splitlines() for line in expected_lines):
        raise GoldenPathError(f"final Git diff is not the expected mutation: {diff.stdout!r}")
    status = _run_checked(
        ("git", "status", "--porcelain"), cwd=repo, env=env, description="inspect final Git status"
    )
    if status.stdout.splitlines() != [" M miniapp.py"]:
        raise GoldenPathError(f"unrelated repository mutation detected: {status.stdout!r}")
    return {"status": status.stdout.strip(), "diff": diff.stdout}


def _write_report(path: Path | None, payload: Mapping[str, object]) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def run_golden_path(wheel: Path, *, report_path: Path | None = None) -> dict[str, object]:
    checkout = Path(__file__).resolve().parents[1]
    wheel = wheel.expanduser().resolve()
    if wheel.name != WHEEL_NAME:
        raise GoldenPathError(f"expected the R1 wheel {WHEEL_NAME}, got {wheel.name}")
    if not wheel.is_file():
        raise GoldenPathError(f"candidate wheel does not exist: {wheel}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            if not any(name.endswith("neuro_code/__init__.py") for name in archive.namelist()):
                raise GoldenPathError("candidate wheel does not contain neuro_code")
    except zipfile.BadZipFile as error:
        raise GoldenPathError("candidate wheel is not a valid ZIP archive") from error

    root = Path(tempfile.mkdtemp(prefix="neuro-code-r2-"))
    cleanup_errors: list[str] = []
    primary_error: BaseException | None = None
    fixture: _LoopbackFixture | None = None
    report: dict[str, object] = {
        "version": VERSION,
        "wheel": wheel.name,
        "wheel_sha256": _sha256(wheel),
        "build_backend_drift_note": (
            "R1 observed Hatchling 1.32.0 to 1.32.3 metadata drift; R2 records this "
            "as release-readiness hardening and does not pin the backend."
        ),
    }
    try:
        home = root / "home"
        state = root / "state"
        repo = root / "golden-repository"
        venv_dir = root / "external-venv"
        home.mkdir()
        repo.mkdir()
        env = _product_environment(home=home, state=state, venv_bin=_venv_bin(venv_dir))
        _initialize_repository(repo, env)
        fixture = _LoopbackFixture(repo)
        fixture.start()
        _write_config(state, fixture.base_url)
        product_python = _create_environment(venv_dir, wheel, repo=repo, env=env)
        env = _product_environment(home=home, state=state, venv_bin=_venv_bin(venv_dir))
        report["source_shadowing_proof"] = _import_proof(
            product_python,
            repo=repo,
            env=env,
            checkout=checkout,
        )
        executable = _product_executable(venv_dir)
        report["product_executable"] = str(executable.resolve())
        if not executable.resolve().is_relative_to(venv_dir.resolve()):
            raise GoldenPathError("product executable does not resolve inside the external venv")
        report["subprocess_cwd"] = str(repo.resolve())
        report["user_task"] = (
            "Inspect miniapp.py, change answer() from old to new, and report the verified result."
        )
        first_payload, _first_stdout, _first_stderr = _run_product(
            executable,
            (
                "-p",
                report["user_task"],
                "--cwd",
                str(repo),
                "--provider",
                "fixture",
                "--sandbox",
                "off",
                "--always-approve",
                "--max-steps",
                "8",
                "--output-format",
                "json",
                "--verify-command",
                VERIFICATION_COMMAND,
            ),
            cwd=repo,
            env=env,
        )
        session_id = _assert_first_result(first_payload)
        report["session_id"] = session_id
        report["first_process_exited"] = True
        listed = _run_public_json_command(
            executable,
            ("sessions", "list", "--cwd", str(repo), "--json"),
            cwd=repo,
            env=env,
        )
        if not isinstance(listed, list) or not any(
            isinstance(row, Mapping) and row.get("id") == session_id for row in listed
        ):
            raise GoldenPathError(
                "completed session was not visible through the public session list"
            )
        recovery = _run_public_json_command(
            executable,
            ("sessions", "recover", session_id, "--cwd", str(repo), "--json"),
            cwd=repo,
            env=env,
        )
        if recovery != []:
            raise GoldenPathError(
                f"completed first turn left an unfinished recovery attempt: {recovery}"
            )
        exported = _run_public_json_command(
            executable,
            ("export", session_id, "--format", "json", "--cwd", str(repo)),
            cwd=repo,
            env=env,
        )
        if not isinstance(exported, Mapping) or VERIFICATION_COMMAND not in json.dumps(exported):
            raise GoldenPathError("public session export did not retain verification history")
        second_payload, _second_stdout, _second_stderr = _run_product(
            executable,
            (
                "-p",
                "Confirm the completed edit and successful verification remain available after restart.",
                "--cwd",
                str(repo),
                "--provider",
                "fixture",
                "--sandbox",
                "off",
                "--always-approve",
                "--max-steps",
                "4",
                "--output-format",
                "json",
                "--resume",
                session_id,
            ),
            cwd=repo,
            env=env,
        )
        _assert_second_result(second_payload, session_id)
        report["second_process_exited"] = True
        report["provider_sequence"] = list(fixture.state.stages)
        report["provider_request_count"] = len(fixture.state.requests)
        if fixture.state.failure is not None:
            raise GoldenPathError(f"loopback fixture failed: {fixture.state.failure}")
        report["workspace"] = _assert_workspace(repo, env)
        report["verification_command"] = VERIFICATION_COMMAND
        report["verification_result"] = "1 passed"
        report["final_response_contract"] = {
            "first_exactly_one_committed_turn": True,
            "second_exactly_one_committed_turn": True,
            "duplicate_side_effect": False,
            "unfinished_recovery": False,
        }
        report["cleanup"] = {
            "provider_closed": False,
            "product_processes_exited": True,
            "temporary_paths_deleted": False,
        }
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if fixture is not None:
            try:
                fixture.close()
                report.setdefault("cleanup", {})["provider_closed"] = True
            except BaseException as error:
                cleanup_errors.append(f"provider cleanup: {type(error).__name__}: {error}")
        try:
            shutil.rmtree(root)
            report.setdefault("cleanup", {})["temporary_paths_deleted"] = True
        except BaseException as error:
            cleanup_errors.append(f"temporary cleanup: {type(error).__name__}: {error}")
        try:
            _write_report(report_path, report)
        except BaseException as error:
            cleanup_errors.append(f"report write: {type(error).__name__}: {error}")
        if cleanup_errors and primary_error is None:
            raise GoldenPathError("; ".join(cleanup_errors))
        if cleanup_errors:
            print(
                "R2 cleanup warning (primary failure preserved): " + "; ".join(cleanup_errors),
                file=sys.stderr,
            )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel", type=Path, required=True, help="exact candidate wheel to install"
    )
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_golden_path(args.wheel, report_path=args.report)
    except (GoldenPathError, OSError, subprocess.SubprocessError) as error:
        print(f"R2 installed-artifact golden path failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
