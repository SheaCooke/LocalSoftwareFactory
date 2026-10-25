"""Web UI: API, security middleware, live model switching, SSE, launch helpers."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import write_agent
from typer.testing import CliRunner

from factory.cli import app as cli_app
from factory.config.loader import LiveConfig
from factory.isolation.sandboxes.docker_backend import DockerSandbox
from factory.isolation.worktrees import git_available
from factory.ui import launch
from factory.ui.app import create_app
from factory.ui.service import FactoryService

BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerSandbox, "availability", classmethod(lambda cls, c: "disabled"))


@pytest.fixture
def service(factory_root: Path) -> FactoryService:
    return FactoryService(LiveConfig(factory_root))


@pytest.fixture
def client(service: FactoryService) -> Iterator[TestClient]:
    with TestClient(create_app(service.live, service=service), base_url=BASE) as c:
        yield c


def wait_for(service: FactoryService, run_id: str) -> dict[str, Any]:
    service.runs.join(run_id, timeout=30)
    return service.run_detail(run_id)


def fake(service: FactoryService) -> Any:
    return service.orch.resolver.provider_for(service.orch.resolver.resolve(run_override="echo"))


# ------------------------------------------------------------------ frontend & security


def test_frontend_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Software Factory" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert "--accent" in client.get("/static/app.css").text


def test_non_loopback_host_is_rejected(service: FactoryService) -> None:
    app = create_app(service.live, service=service)
    with TestClient(app, base_url="http://evil.example:8765") as c:
        assert c.get("/api/status").status_code == 403  # DNS rebinding


def test_state_changes_need_json_and_loopback_origin(client: TestClient) -> None:
    form = client.post("/api/models/default", data={"alias": "echo"})
    assert form.status_code == 415  # a cross-site HTML form cannot send JSON
    cross = client.post(
        "/api/models/default", json={"alias": "echo"}, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    ok = client.post(
        "/api/models/default", json={"alias": "echo"}, headers={"Origin": "http://localhost:8765"}
    )
    assert ok.status_code == 200


def test_status_reports_errors_and_reduced_isolation(
    client: TestClient, factory_root: Path
) -> None:
    (factory_root / "agents" / "broken").mkdir()
    (factory_root / "agents" / "broken" / "agent.md").write_text("no frontmatter", "utf-8")
    status = client.get("/api/status").json()
    assert (
        status["sandbox"]["reduced"] is True and "REDUCED ISOLATION" in status["sandbox"]["message"]
    )
    assert [e["name"] for e in status["agent_errors"]] == ["broken"]
    (factory_root / "config" / "models.yaml").write_text("models: [", "utf-8")
    status = client.get("/api/status").json()
    assert "YAML syntax error" in status["config_error"]


# ------------------------------------------------------------------------- models


def test_model_selector_global_and_per_agent(client: TestClient, factory_root: Path) -> None:
    models = client.get("/api/models").json()
    assert models["default"] == "echo" and models["default_source"] == "factory.yaml"
    planner = next(a for a in models["agents"] if a["name"] == "planner")
    assert planner["effective"] == "fast" and planner["source"] == "agent_default"

    models = client.post("/api/models/agents/planner", json={"alias": "local-coder"}).json()
    planner = next(a for a in models["agents"] if a["name"] == "planner")
    assert (planner["effective"], planner["source"]) == ("local-coder", "agent_override")
    models = client.post("/api/models/default", json={"alias": "smart"}).json()
    assert models["default"] == "smart" and models["default_source"] == "runtime"
    assert "planner: local-coder" in (factory_root / "config" / "overrides.yaml").read_text("utf-8")

    models = client.post("/api/models/agents/planner", json={"alias": None}).json()
    assert next(a for a in models["agents"] if a["name"] == "planner")["source"] == "agent_default"
    assert client.post("/api/models/default", json={"alias": None}).json()["default"] == "echo"
    assert client.post("/api/models/default", json={"alias": "gpt-99"}).status_code == 400
    assert client.post("/api/models/agents/nobody", json={"alias": "echo"}).status_code == 404


# ------------------------------------------------------------------------- runs


def test_start_run_and_inspect(client: TestClient, service: FactoryService) -> None:
    pipelines = {p["name"]: p for p in client.get("/api/pipelines").json()}
    assert pipelines["default"]["error"] is None
    assert [s["id"] for s in pipelines["quick"]["stages"]] == ["requirements", "design", "plan"]

    created = client.post(
        "/api/runs", json={"request": "todo app", "pipeline": "quick", "model": "echo"}
    )
    assert created.status_code == 201
    run_id = created.json()["id"]
    wait_for(service, run_id)
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "passed" and detail["active"] is False
    assert [s["status"] for s in detail["stages"]] == ["passed"] * 3
    assert {u["agent"] for u in detail["usage"]} == {"requirements", "architect", "planner"}
    assert detail["total"]["tokens"] > 0
    origin = {a["name"]: (a["stage"], a["agent"]) for a in detail["artifacts"]}
    assert origin["plan.md"] == ("plan", "planner") and origin["request.md"] == (None, "user")

    assert client.get("/api/runs").json()[0]["id"] == run_id
    events = client.get(f"/api/runs/{run_id}/events").json()
    assert {"run.created", "llm.call", "stage.transition"} <= {e["type"] for e in events}
    assert "[echo]" in client.get(f"/api/runs/{run_id}/artifacts/plan.md").text
    assert client.get(
        f"/api/runs/{run_id}/artifacts/..%2F..%2Fconfig%2Fmodels.yaml"
    ).status_code in (400, 404)
    assert client.get(f"/api/runs/{run_id}/artifacts/missing.md").status_code == 404
    assert client.get("/api/runs/nope").status_code == 404


def test_invalid_start_is_a_400(client: TestClient) -> None:
    bad = client.post("/api/runs", json={"request": "x", "pipeline": "nope"})
    assert bad.status_code == 400 and "not found" in bad.json()["error"]
    assert client.post("/api/runs", json={"request": ""}).status_code == 422


def test_gate_controls(client: TestClient, service: FactoryService) -> None:
    run_id = client.post("/api/runs", json={"request": "todo", "model": "echo"}).json()["id"]
    detail = wait_for(service, run_id)
    assert detail["status"] == "waiting_approval"
    [pending] = [a for a in detail["approvals"] if a["status"] == "pending"]
    assert pending["stage"] == "requirements" and pending["details"]["artifacts"] == [
        "requirements.md"
    ]
    current = [s for s in detail["stages"] if s["current"]]
    assert [s["id"] for s in current] == ["requirements"]

    no_comment = client.post(f"/api/runs/{run_id}/decision", json={"decision": "changes_requested"})
    assert no_comment.status_code == 400 and "comment is required" in no_comment.json()["error"]
    client.post(
        f"/api/runs/{run_id}/decision",
        json={"decision": "changes_requested", "comment": "use JSON storage"},
    )
    wait_for(service, run_id)
    asks = [
        c.messages[1].content for c in fake(service).calls if c.metadata["agent"] == "requirements"
    ]
    assert "use JSON storage" in asks[-1]

    client.post(f"/api/runs/{run_id}/decision", json={"decision": "approved"})
    detail = wait_for(service, run_id)
    assert detail["current_stage"] == "design" and detail["status"] == "waiting_approval"
    client.post(f"/api/runs/{run_id}/decision", json={"decision": "rejected", "comment": "stop"})
    detail = wait_for(service, run_id)
    assert detail["status"] == "failed" and "stop" in detail["error"]
    again = client.post(f"/api/runs/{run_id}/decision", json={"decision": "approved"})
    assert again.status_code == 400


def test_switch_models_while_run_waits(
    client: TestClient, service: FactoryService, factory_root: Path
) -> None:
    models = factory_root / "config" / "models.yaml"
    models.write_text(
        models.read_text("utf-8") + "\n  echo-local:\n    provider: fake\n    model: echo-local\n",
        "utf-8",
    )
    run_id = client.post("/api/runs", json={"request": "todo", "model": "echo"}).json()["id"]
    wait_for(service, run_id)  # waiting at the requirements gate
    # The run's own override beats agent overrides, so switching it is what matters here.
    detail = client.post(f"/api/runs/{run_id}/model", json={"alias": "echo-local"}).json()
    assert detail["model_override"] == "echo-local"
    client.post(f"/api/runs/{run_id}/decision", json={"decision": "approved"})
    wait_for(service, run_id)
    used = [
        (e["agent"], e["data"]["alias"])
        for e in client.get(f"/api/runs/{run_id}/events").json()
        if e["type"] == "llm.call"
    ]
    assert used == [("requirements", "echo"), ("architect", "echo-local")]

    # Clearing it hands control to per-agent overrides, which also apply on the next call.
    client.post(f"/api/runs/{run_id}/model", json={"alias": None})
    client.post("/api/models/agents/planner", json={"alias": "echo-local"})
    client.post("/api/models/agents/architect", json={"alias": "echo"})
    client.post(f"/api/runs/{run_id}/decision", json={"decision": "approved"})
    wait_for(service, run_id)
    used = [
        (e["agent"], e["data"]["alias"])
        for e in client.get(f"/api/runs/{run_id}/events").json()
        if e["type"] == "llm.call"
    ]
    assert used[2] == ("planner", "echo-local")
    assert client.post(f"/api/runs/{run_id}/model", json={"alias": "nope"}).status_code == 400


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_changes_and_diffs(client: TestClient, service: FactoryService, factory_root: Path) -> None:
    write_agent(
        factory_root, "writer",
        "model: echo\nisolation_level: L2\ntools: [fs.write]\n"
        'fs_scope:\n  read: ["{workspace}/**"]\n  write: ["{workspace}/**"]\n',
    )  # fmt: skip
    (factory_root / "config" / "pipelines" / "w.yaml").write_text(
        "name: w\nstages:\n  - {id: code, agent: writer}\n", "utf-8"
    )
    fake(service).add_script(
        [
            {
                "match": {"agent": "writer"},
                "response": {
                    "tool_calls": [
                        {
                            "name": "fs.write",
                            "arguments": {"path": "hello.py", "content": "print('hi')\n"},
                        }
                    ]
                },
            },
            {"match": {"agent": "writer"}, "response": {"content": "done"}},
        ]
    )
    run_id = client.post(
        "/api/runs", json={"request": "hi", "pipeline": "w", "model": "echo"}
    ).json()["id"]
    assert wait_for(service, run_id)["status"] == "passed"
    changes = client.get(f"/api/runs/{run_id}/changes").json()
    assert changes["available"] and "hello.py" in changes["files"]
    assert {r["ref"] for r in changes["refs"]} == {"integration", "agent:writer"}
    assert changes["stages"][0]["stage"] == "code"
    for ref in (
        "integration",
        "agent:writer",
        "stage:code",
        f"commit:{changes['stages'][0]['commits'][0]['sha']}",
    ):
        diff = client.get(f"/api/runs/{run_id}/diff", params={"ref": ref}).text
        assert "+print('hi')" in diff, ref
    assert (
        client.get(f"/api/runs/{run_id}/diff", params={"ref": "agent:../../x"}).status_code == 400
    )
    assert (
        client.get(f"/api/runs/{run_id}/diff", params={"ref": "commit:HEAD;rm"}).status_code == 400
    )


def test_resume_endpoint(client: TestClient, service: FactoryService, factory_root: Path) -> None:
    run_id = client.post(
        "/api/runs", json={"request": "x", "pipeline": "quick", "model": "echo"}
    ).json()["id"]
    assert wait_for(service, run_id)["status"] == "passed"
    # A finished run resumes to the same state; an active one is refused.
    assert client.post(f"/api/runs/{run_id}/resume", json={}).status_code == 200
    wait_for(service, run_id)


def test_interrupted_runs_resume_on_startup(service: FactoryService) -> None:
    run = service.orch.create_run("x", pipeline="quick", model_override="echo")
    service.orch.store.update_run(run.id, status="running")  # left behind by a crash
    app = create_app(service.live, service=service, resume_interrupted=True)
    with TestClient(app, base_url=BASE):
        assert wait_for(service, run.id)["status"] == "passed"


# --------------------------------------------------------------------------- SSE


def test_sse_stream_delivers_live_events(client: TestClient, service: FactoryService) -> None:
    def emit_when_connected() -> None:
        deadline = time.monotonic() + 10
        while service.broadcaster.client_count == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        service.orch.bus.emit("tool.call", run_id="other-run", data={"x": 0})  # filtered out
        service.orch.bus.emit("tool.call", run_id="r-sse", data={"tool": "fs.read"})
        service.orch.bus.emit("config.reloaded", data={})  # global events reach every client

    threading.Thread(target=emit_when_connected, daemon=True).start()
    with client.stream("GET", "/api/stream", params={"run": "r-sse", "limit": 2}) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
    received = [json.loads(f) for f in frames]
    assert [(e["type"], e["run_id"]) for e in received] == [
        ("tool.call", "r-sse"),
        ("config.reloaded", None),
    ]
    assert service.broadcaster.client_count == 0


def test_sse_payloads_are_redacted(service: FactoryService) -> None:
    from factory.secrets import redactor

    redactor.add("sk-ui-secret-123456")
    seen: list[str] = []
    service.broadcaster._clients.clear()

    class _Loop:
        def call_soon_threadsafe(self, fn: Any, queue: Any, data: str) -> None:
            seen.append(data)

    from factory.ui.broadcast import _Client

    service.broadcaster._clients.add(_Client(queue=None, loop=_Loop(), run_id=None))  # type: ignore[arg-type]
    service.orch.bus.emit("agent.message", run_id="r", data={"content": "key sk-ui-secret-123456"})
    assert seen and "sk-ui-secret-123456" not in seen[0] and "[REDACTED]" in seen[0]


# ------------------------------------------------------------------------- launch


def test_open_browser_prefers_windows_side_in_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(launch, "is_wsl", lambda: True)
    monkeypatch.setattr(
        launch.shutil, "which", lambda c: f"/usr/bin/{c}" if c == "explorer.exe" else None
    )
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    assert launch.open_browser("http://localhost:8765") == "explorer.exe"
    assert launched == [["explorer.exe", "http://localhost:8765"]]


def test_open_browser_falls_back_to_webbrowser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launch, "is_wsl", lambda: False)
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: True)
    assert launch.open_browser("http://localhost:1") == "webbrowser"


def test_up_refuses_busy_port(factory_root: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        result = CliRunner().invoke(cli_app, ["up", "--port", str(port), "--no-open"])
    assert result.exit_code == 1 and "already in use" in result.stderr
