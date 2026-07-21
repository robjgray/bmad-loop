"""Tests for the Goose ACP stdio adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bmad_loop.adapters.base import SessionSpec
from bmad_loop.adapters.goose_acp import GooseAcpAdapter
from bmad_loop.adapters.profile import CLIProfile, HookSpec


def _make_policy():
    """Minimal policy double."""

    class Limits:
        teardown_grace_s = 1.0

    class Policy:
        limits = Limits()

    return Policy()


def _make_profile(binary: Path) -> CLIProfile:
    # In tests the fake script is run via sys.executable, so launch_args holds
    # the script path. Tests monkeypatch _build_argv to [sys.executable, script].
    return CLIProfile(
        name="goose",
        binary=sys.executable,
        prompt_template="{prompt}",
        launch_args=[str(binary)],
        bypass_args=[],
        model_flag="--model",
        usage_parser="none",
        hooks=HookSpec(dialect="none", config_path="", events={}),
        transport="stdio-jsonrpc",
    )


def _fake_acp_script() -> str:
    """Return a Python script that acts as a minimal ``goose acp`` server."""
    return r"""
import json
import sys

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

sessions = {}

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": 0}})
    elif method == "session/new":
        sid = "session-" + str(len(sessions) + 1)
        sessions[sid] = True
        send({"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": sid}})
    elif method == "session/prompt":
        sid = req["params"]["sessionId"]
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "Hello, "},
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "world!"},
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "stopReason": "endTurn",
                "usage": {
                    "totalTokens": 42,
                    "inputTokens": 10,
                    "outputTokens": 32,
                },
            },
        })
    elif method == "session/close":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
"""


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch) -> GooseAcpAdapter:
    script = tmp_path / "fake_acp.py"
    script.write_text(_fake_acp_script(), encoding="utf-8")
    profile = _make_profile(script)
    monkeypatch.setattr(GooseAcpAdapter, "_build_argv", lambda self: [sys.executable, str(script)])
    return GooseAcpAdapter(
        run_dir=tmp_path,
        policy=_make_policy(),
        profile=profile,
    )


def test_adapter_start_and_complete(adapter: GooseAcpAdapter, tmp_path: Path):
    script = tmp_path / "fake_acp.py"
    adapter._build_argv = lambda: [sys.executable, str(script)]
    spec = SessionSpec(
        task_id="task-1",
        role="dev",
        prompt="say hello",
        cwd=tmp_path,
        env={},
        timeout_s=30.0,
    )

    handle = adapter.start_session(spec)
    assert handle.task_id == "task-1"
    assert handle.native_id.startswith("session-")

    result = adapter.wait_for_completion(handle, spec)
    assert result.status == "completed"
    assert result.result_json is not None
    assert result.result_json["stopReason"] == "endTurn"
    assert result.result_json["response"] == "Hello, world!"
    assert result.result_json["usage"]["totalTokens"] == 42

    # Result file should be persisted.
    result_path = tmp_path / "tasks" / "task-1" / "result.json"
    assert result_path.is_file()
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data["response"] == "Hello, world!"

    # Clean shutdown should work.
    adapter.kill(handle)


def test_adapter_crashes_on_acp_error(tmp_path: Path):
    script = tmp_path / "fake_acp_error.py"
    script.write_text(
        r"""
import json
import sys
for raw_line in sys.stdin:
    req = json.loads(raw_line.strip())
    if req.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": 0}}) + "\n")
        sys.stdout.flush()
    elif req.get("method") == "session/new":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32600, "message": "boom"}}) + "\n")
        sys.stdout.flush()
""",
        encoding="utf-8",
    )
    profile = _make_profile(script)
    adapter = GooseAcpAdapter(
        run_dir=tmp_path,
        policy=_make_policy(),
        profile=profile,
    )
    adapter._build_argv = lambda: [sys.executable, str(script)]
    spec = SessionSpec(
        task_id="task-2",
        role="dev",
        prompt="explode",
        cwd=tmp_path,
        env={},
        timeout_s=30.0,
    )

    with pytest.raises(Exception):  # AcpError
        adapter.start_session(spec)
