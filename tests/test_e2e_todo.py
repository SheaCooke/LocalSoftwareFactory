"""Milestone 7 end-to-end test: the default pipeline builds a CLI todo app from a
one-line request, using the Fake provider (no API keys), real git worktrees, the
permission layer and a real sandbox. The delivered code is then checked out and run.

The scripted transcript lives in examples/todo-app/script.yaml (also the `demo`
model alias). It deliberately exercises the failure paths: a failing check gate
that is retried, a review that loops back to the implementer, and agents trying to
write outside their scope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import make_orchestrator, of_type
from typer.testing import CliRunner

from factory.cli import app
from factory.config.schema import SandboxConfig
from factory.core.orchestrator import Orchestrator
from factory.isolation.sandboxes.docker_backend import DockerSandbox
from factory.isolation.worktrees import RunRepo, git_available, run_git

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")

REPO = Path(__file__).resolve().parents[1]
REQUEST = "A CLI todo app with add, list and done commands"
DOCKER_READY = DockerSandbox.availability(SandboxConfig()) is None


@pytest.fixture(
    params=[
        "subprocess",
        pytest.param(
            "docker",
            marks=pytest.mark.skipif(not DOCKER_READY, reason="Docker sandbox not available"),
        ),
    ]
)
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "subprocess":
        monkeypatch.setattr(DockerSandbox, "availability", classmethod(lambda cls, c: "disabled"))
    return str(request.param)


@pytest.fixture
def root(factory_root: Path) -> Path:
    shutil.copytree(REPO / "examples", factory_root / "examples")
    return factory_root


def target_repo(path: Path) -> Path:
    path.mkdir()
    run_git(["init", "-q", "-b", "main"], path)
    (path / "LICENSE").write_text("MIT License\n", "utf-8")
    run_git(["add", "."], path)
    run_git(["commit", "-q", "-m", "initial commit"], path)
    return path


def drive(orch: Orchestrator, run_id: str) -> tuple[str, list[tuple[str | None, str]]]:
    """Run to completion, approving every gate the way a human would in the UI."""
    approved: list[tuple[str | None, str]] = []
    for _ in range(10):
        run = orch.run(run_id)
        if run.status != "waiting_approval":
            return run.status, approved
        [pending] = orch.pending_approvals(run_id)
        approved.append((pending.stage_id, str(pending.details_dict.get("phase"))))
        orch.decide(run_id, "approved", "looks good")
    raise AssertionError("run did not finish")


def test_default_pipeline_builds_todo_app(root: Path, tmp_path: Path, backend: str) -> None:
    target = target_repo(tmp_path / "todo-project")
    orch, _, events = make_orchestrator(root)
    run = orch.create_run(REQUEST, pipeline="default", repo=str(target), model_override="demo")
    status, approved = drive(orch, run.id)
    run = orch.store.get_run(run.id)  # type: ignore[assignment]
    assert status == "passed", run.error

    # ---- human gates: requirements, design (after the agent), integrate (before merge)
    assert approved == [("requirements", "after"), ("design", "after"), ("integrate", "before")]

    # ---- every stage passed; the scenic route was taken
    stages = {s.stage_id: s for s in orch.store.stages(run.id)}
    assert all(s.status == "passed" for s in stages.values()), stages
    assert [e.data["retry"] for e in of_type(events, "stage.retry")] == [1]  # failing check
    [loop] = of_type(events, "stage.loop")  # review sent the work back once
    assert loop.stage == "review" and loop.data["goto"] == "implement"
    checks = [
        e.data["passed"] for e in of_type(events, "gate.evaluated") if e.data["gate"] == "check"
    ]
    assert checks == [False, True, True]
    verdicts = [
        e.data["passed"] for e in of_type(events, "gate.evaluated") if e.data["gate"] == "pass_when"
    ]
    assert verdicts == [False, True]

    # ---- isolation held: every out-of-scope write was blocked and attributed
    denied = [
        (e.agent, e.data["tool"], e.data["target"])
        for e in of_type(events, "permission.decision")
        if e.data["decision"] == "denied"
    ]
    assert denied == [
        ("tester", "fs.write", "src/todo/cli.py"),
        ("implementer", "fs.write", "tests/test_todo.py"),
        ("reviewer", "fs.write", "src/todo/cli.py"),
        ("docs", "fs.write", "src/todo/cli.py"),
    ]
    [isolation] = of_type(events, "isolation.status")
    assert isolation.data["backend"] == backend

    # ---- artifacts: every agent's output, plus gate reports and the delivery note
    store = orch.artifacts(run.id)
    for name in (
        "request.md", "requirements.md", "design.md", "plan.md", "test_plan.md",
        "implementation_summary.md", "review.json", "security_report.json",
        "docs_summary.md", "test_report.json", "delivery.json",
    ):  # fmt: skip
        assert store.exists(name), name
    report = json.loads(store.read("test_report.json"))
    assert report["passed"] and "4 passed" in report["stdout_tail"]
    assert json.loads(store.read("review.json"))["verdict"] == "pass"

    # ---- observability: tallies per agent, transcripts, JSONL log
    agents = {u.agent for u in orch.store.usage_by_agent(run.id)}
    assert agents == {
        "requirements", "architect", "planner", "tester",
        "implementer", "reviewer", "security", "docs",
    }  # fmt: skip
    transcripts = list((orch.run_dir(run.id) / "transcripts").glob("*.json"))
    assert len(transcripts) == 12  # 8 agents + implementer retry + review loop (impl, rev, sec)
    assert len(orch.event_log.read(run.id)) > 100

    # ---- delivery: a branch in the target repo; its checked-out branch untouched
    delivery = json.loads(store.read("delivery.json"))
    branch = f"factory/{run.id}"
    assert delivery["branch"] == branch
    files = set(run_git(["ls-tree", "-r", "--name-only", branch], target).split())
    assert files == {
        "LICENSE", "README.md", "pyproject.toml", "tests/test_todo.py",
        "src/todo/__init__.py", "src/todo/__main__.py", "src/todo/cli.py",
    }  # fmt: skip
    assert run_git(["branch", "--show-current"], target).strip() == "main"
    assert sorted(p.name for p in target.iterdir() if p.name != ".git") == ["LICENSE"]
    assert RunRepo(root / "workspaces", run.id).exists

    # ---- the delivered app actually works
    checkout = tmp_path / "checkout"
    run_git(["clone", "-q", "--branch", branch, str(target), str(checkout)], tmp_path)
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=checkout, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert tests.returncode == 0 and "4 passed" in tests.stdout, tests.stdout + tests.stderr

    env = {
        **os.environ,
        "PYTHONPATH": str(checkout / "src"),
        "TODO_FILE": str(tmp_path / "todos.json"),
    }

    def todo(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "todo", *args],
            cwd=checkout, env=env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip

    assert todo("list").stdout.strip() == "no todos"
    assert todo("add", "buy milk").returncode == 0
    assert todo("add", "walk dog").returncode == 0
    assert todo("done", "1").returncode == 0
    assert todo("list").stdout.splitlines() == ["1 [x] buy milk", "2 [ ] walk dog"]
    missing = todo("done", "99")  # AC-3.2, the bug the reviewer caught
    assert missing.returncode == 1 and "error: no todo with id 99" in missing.stderr


def test_todo_app_through_the_cli(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same run, driven the way a person would from a terminal (new project, no --repo)."""
    monkeypatch.setattr(DockerSandbox, "availability", classmethod(lambda cls, c: "disabled"))
    runner = CliRunner()
    started = runner.invoke(app, ["run", "start", REQUEST, "--model", "demo"])
    assert started.exit_code == 0, started.output
    assert "approval #1 (gate)" in started.stdout
    run_id = started.stdout.split()[1]
    for expected in ("stage design: waiting_approval", "stage integrate: waiting_approval"):
        step = runner.invoke(app, ["runs", "approve", run_id])
        assert expected in step.stdout, step.stdout
    final = runner.invoke(app, ["runs", "approve", run_id])
    assert final.exit_code == 0, final.output
    assert f"run {run_id}: passed" in final.stdout
    assert "delivered" in final.stdout and "git clone --branch" in final.stdout
    shown = runner.invoke(app, ["runs", "show", run_id])
    assert "integrate      passed" in shown.stdout and "loops: {'review': 1}" in shown.stdout


def test_demo_replays_for_every_run(root: Path) -> None:
    """The `demo` alias works for any number of runs in one process."""
    orch, _, _ = make_orchestrator(root)
    for _ in range(2):
        run = orch.create_run(REQUEST, pipeline="quick", model_override="demo")
        assert orch.run(run.id).status == "passed"
        assert (
            orch.artifacts(run.id)
            .read("requirements.md")
            .startswith("# Requirements: CLI todo list")
        )
