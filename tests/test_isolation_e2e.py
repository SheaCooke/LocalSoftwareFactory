"""End-to-end isolation: real worktrees, permission layer and sandbox, scripted LLM."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from helpers import make_orchestrator, of_type

from factory.config.schema import SandboxConfig
from factory.isolation.sandboxes.docker_backend import DockerSandbox
from factory.isolation.worktrees import RunRepo, git_available, run_git

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")

TEST_FILE = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calc import add


def test_add():
    assert add(2, 3) == 5
"""

PIPELINE = """\
name: iso
stages:
  - {id: tests, agent: tester}
  - {id: implement, agent: implementer}
  - {id: review, agent: reviewer}
  - {id: docs, agent: docs}
"""


def call(agent: str, tool: str, **arguments: Any) -> dict[str, Any]:
    return {
        "match": {"agent": agent},
        "response": {"tool_calls": [{"name": tool, "arguments": arguments}]},
    }


def final(agent: str, text: str = "done") -> dict[str, Any]:
    return {"match": {"agent": agent}, "response": {"content": text}}


SCRIPT = [
    # tester: writes tests, is blocked from src/
    call("tester", "fs.write", path="tests/test_calc.py", content=TEST_FILE),
    call("tester", "fs.write", path="src/calc.py", content="def add(a, b): return 0\n"),
    call("tester", "artifact.write", name="test_plan.md", content="test_add -> AC-1"),
    final("tester"),
    # implementer: reads tests, cannot edit them or escape, writes src, runs pytest
    call("implementer", "fs.read", path="tests/test_calc.py"),
    call("implementer", "fs.write", path="tests/test_calc.py", content="def test_add(): pass\n"),
    call("implementer", "fs.read", path="../tester/tests/test_calc.py"),
    call("implementer", "fs.read", path=".git"),
    call(
        "implementer", "fs.write", path="src/calc.py", content="def add(a, b):\n    return a + b\n"
    ),
    call("implementer", "shell.run_sandboxed", command="python -m pytest -q"),
    call("implementer", "shell.run_sandboxed", command="cmd /c whoami"),
    call("implementer", "shell.run_sandboxed", command="python -V", network=True),
    call("implementer", "artifact.write", name="implementation_summary.md", content="add()"),
    final("implementer"),
    # reviewer: read-only
    call("reviewer", "git.diff"),
    call("reviewer", "fs.write", path="src/calc.py", content="sabotage"),
    call("reviewer", "artifact.write", name="review.json", content='{"verdict": "pass"}'),
    final("reviewer"),
    # docs: README only
    call("docs", "fs.write", path="README.md", content="# calc\n"),
    call("docs", "fs.write", path="src/calc.py", content="oops"),
    call("docs", "artifact.write", name="docs_summary.md", content="README"),
    final("docs"),
]


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        DockerSandbox, "availability", classmethod(lambda cls, c: "disabled in test")
    )


def _tool_results(fake: Any, agent: str) -> list[str]:
    last = [c for c in fake.calls if c.metadata.get("agent") == agent][-1]
    return [m.content for m in last.messages if m.role == "tool"]


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
        monkeypatch.setattr(
            DockerSandbox, "availability", classmethod(lambda cls, c: "disabled in test")
        )
    return str(request.param)


def test_isolation_end_to_end(factory_root: Path, backend: str) -> None:
    (factory_root / "config" / "pipelines" / "iso.yaml").write_text(PIPELINE, "utf-8")
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script(SCRIPT)
    run = orch.create_run("calculator", pipeline="iso", model_override="echo")
    run = orch.run(run.id)
    assert run.status == "passed", run.error

    # The sandbox in use was announced; the subprocess fallback as reduced isolation.
    [status] = of_type(events, "isolation.status")
    assert status.data["backend"] == backend
    assert status.data["reduced"] is (backend == "subprocess")

    # Every out-of-scope attempt was blocked and logged, attributed to the right agent.
    denied = [
        (e.agent, e.data["tool"], e.data["target"])
        for e in of_type(events, "permission.decision")
        if e.data["decision"] == "denied"
    ]
    expected = [
        ("tester", "fs.write", "src/calc.py"),
        ("implementer", "fs.write", "tests/test_calc.py"),
        ("implementer", "fs.read", "../tester/tests/test_calc.py"),
        ("implementer", "fs.read", ".git"),
        ("implementer", "shell.run_sandboxed", "cmd /c whoami"),  # allowlist (subprocess only)
        ("implementer", "shell.run_sandboxed", "python -V"),
        ("reviewer", "fs.write", "src/calc.py"),
        ("docs", "fs.write", "src/calc.py"),
    ]
    if backend == "docker":
        expected.remove(("implementer", "shell.run_sandboxed", "cmd /c whoami"))
    assert denied == expected
    logged = [e for e in orch.event_log.read(run.id) if e["type"] == "permission.decision"]
    assert sum(e["data"]["decision"] == "denied" for e in logged) == len(denied)

    impl = _tool_results(fake, "implementer")
    assert "from calc import add" in impl[0]  # may read the tests
    assert impl[1].startswith("ERROR: permission denied")
    assert "exit code: 0" in impl[5] and "1 passed" in impl[5]  # tests ran in the sandbox
    if backend == "docker":
        assert "exit code: 127" in impl[6]  # no allowlist, but no Windows `cmd` in the container
    else:
        assert impl[6].startswith("ERROR: permission denied")
    assert "network access requires isolation level L3" in impl[7]
    review = _tool_results(fake, "reviewer")
    assert "src/calc.py" in review[0] and "tests/test_calc.py" in review[0]
    assert "not available to agent 'reviewer'" in review[1]

    # Approved work landed on the integration branch; denied writes did not.
    repo = RunRepo(factory_root / "workspaces", run.id)
    assert set(repo.files_on_integration()) == {"README.md", "src/calc.py", "tests/test_calc.py"}
    src = run_git(["show", f"{repo.integration}:src/calc.py"], repo.repo)
    assert "return a + b" in src
    tests = run_git(["show", f"{repo.integration}:tests/test_calc.py"], repo.repo)
    assert tests == TEST_FILE
    # Each writing agent worked in its own worktree.
    assert not (repo.worktree_path("tester") / "src").exists()
    landed = {e.agent: e.data for e in of_type(events, "workspace.landed")}
    assert set(landed) == {"tester", "implementer", "docs"}  # reviewer is read-only
    assert all(d["merged_to_integration"] for d in landed.values())


def test_target_repo_is_cloned_not_modified(
    factory_root: Path, tmp_path: Path, no_docker: None
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    run_git(["init", "-q", "-b", "main"], target)
    (target / "LICENSE").write_text("MIT\n", "utf-8")
    run_git(["add", "."], target)
    run_git(["commit", "-q", "-m", "init"], target)
    (factory_root / "config" / "pipelines" / "iso.yaml").write_text(
        "name: iso\nstages:\n  - {id: docs, agent: docs}\n", "utf-8"
    )
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([s for s in SCRIPT if s["match"]["agent"] == "docs"])
    run = orch.run(
        orch.create_run("docs", pipeline="iso", repo=str(target), model_override="echo").id
    )
    assert run.status == "passed", run.error
    repo = RunRepo(factory_root / "workspaces", run.id)
    assert set(repo.files_on_integration()) == {"LICENSE", "README.md"}
    assert not (target / "README.md").exists()
    assert run_git(["branch", "--list"], target).split() == ["*", "main"]


def test_repo_must_be_a_git_repo(factory_root: Path, tmp_path: Path) -> None:
    from factory.core.pipeline import PipelineError

    orch, _, _ = make_orchestrator(factory_root)
    with pytest.raises(PipelineError, match="must be a git repository"):
        orch.create_run("x", pipeline="quick", repo=str(tmp_path))


def test_explicit_docker_backend_unavailable_fails_stage(
    factory_root: Path, no_docker: None
) -> None:
    factory_yaml = factory_root / "config" / "factory.yaml"
    factory_yaml.write_text(
        factory_yaml.read_text("utf-8").replace("backend: auto", "backend: docker"), "utf-8"
    )
    (factory_root / "config" / "pipelines" / "iso.yaml").write_text(
        "name: iso\nstages:\n  - {id: docs, agent: docs}\n", "utf-8"
    )
    orch, _, _ = make_orchestrator(factory_root)
    run = orch.run(orch.create_run("x", pipeline="iso", model_override="echo").id)
    assert run.status == "failed" and "sandbox backend 'docker' unavailable" in str(run.error)
