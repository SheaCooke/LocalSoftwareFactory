"""Milestone 4: gates, retries, loops, parallel stages, conditions, budgets, resume."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from helpers import make_orchestrator, of_type, write_agent

from factory.core.events import Event
from factory.core.pipeline import PipelineError
from factory.isolation.sandboxes.docker_backend import DockerSandbox
from factory.isolation.worktrees import RunRepo, git_available, run_git
from factory.providers.fake import FakeProvider

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")

WRITER = (
    "model: echo\nisolation_level: L2\ntools: [fs.write, fs.read, artifact.write]\n"
    'fs_scope:\n  read: ["{workspace}/**"]\n  write: ["{workspace}/**"]\n'
    "outputs: [note.md]\n"
)
JUDGE = "model: echo\noutputs: [verdict.json]\n"
CHECK_OK = (
    'python -c "import pathlib, sys; '
    "sys.exit(0 if pathlib.Path('ok.txt').read_text() == 'yes' else 1)\""
)


CHECK_GATE = f"    gate: {{type: check, command: {json.dumps(CHECK_OK)}}}\n"


def call(agent: str, tool: str, **arguments: Any) -> dict[str, Any]:
    return {
        "match": {"agent": agent},
        "response": {"tool_calls": [{"name": tool, "arguments": arguments}]},
    }


def say(agent: str, text: str = "done") -> dict[str, Any]:
    return {"match": {"agent": agent}, "response": {"content": text}}


def verdict(agent: str, value: str) -> dict[str, Any]:
    return say(agent, json.dumps({"verdict": value, "summary": "s", "findings": []}))


@pytest.fixture(autouse=True)
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerSandbox, "availability", classmethod(lambda cls, c: "disabled"))


def pipeline(root: Path, name: str, stages: str, extra: str = "") -> None:
    (root / "config" / "pipelines" / f"{name}.yaml").write_text(
        f"name: {name}\n{extra}stages:\n{stages}", "utf-8"
    )


def user_messages(fake: FakeProvider, agent: str) -> list[str]:
    """The task message of each *invocation* (an invocation may make several LLM calls)."""
    tasks = [c.messages[1].content for c in fake.calls if c.metadata.get("agent") == agent]
    return list(dict.fromkeys(tasks))


# ------------------------------------------------------------------- human gates


def test_human_gate_approve_request_changes_reject(factory_root: Path) -> None:
    write_agent(factory_root, "judge", JUDGE)
    pipeline(factory_root, "g", "  - {id: a, agent: judge, gate: human_approval}\n")
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script([say("judge", '{"v": 1}'), say("judge", '{"v": 2}')])
    run = orch.run(orch.create_run("x", pipeline="g").id)
    assert run.status == "waiting_approval"
    assert orch.store.stage(run.id, "a").status == "waiting_approval"  # type: ignore[union-attr]
    [pending] = orch.pending_approvals(run.id)
    assert pending.kind == "gate" and pending.details_dict["artifacts"] == ["verdict.json"]
    assert orch.run(run.id).status == "waiting_approval"  # no decision yet: nothing happens
    assert len(fake.calls) == 1

    orch.decide(run.id, "changes_requested", "please add field w")
    run = orch.run(run.id)
    assert run.status == "waiting_approval" and len(fake.calls) == 2
    second = user_messages(fake, "judge")[1]
    assert "please add field w" in second and '{"v": 1}' in second  # comment + previous output

    orch.decide(run.id, "approved", "lgtm")
    assert orch.run(run.id).status == "passed"
    kinds = [e.data["decision"] for e in of_type(events, "approval.decided")]
    assert kinds == ["changes_requested", "approved"]

    # A rejection fails the run.
    fake.add_script([say("judge", "{}")])
    run2 = orch.run(orch.create_run("y", pipeline="g").id)
    orch.decide(run2.id, "rejected", "wrong direction")
    run2 = orch.run(run2.id)
    assert run2.status == "failed" and "wrong direction" in str(run2.error)


def test_decide_errors(factory_root: Path) -> None:
    write_agent(factory_root, "judge", JUDGE)
    pipeline(factory_root, "g", "  - {id: a, agent: judge}\n")
    orch, _, _ = make_orchestrator(factory_root)
    run = orch.create_run("x", pipeline="g")
    with pytest.raises(PipelineError, match="no pending approval"):
        orch.decide(run.id, "approved")
    orch.store.create_approval(run.id, stage_id="a", kind="gate", summary="1")
    orch.store.create_approval(run.id, stage_id="a", kind="gate", summary="2")
    with pytest.raises(PipelineError, match="several pending"):
        orch.decide(run.id, "approved")
    first = orch.pending_approvals(run.id)[0]
    assert orch.decide(run.id, "approved", approval_id=first.id).status == "approved"


# -------------------------------------------------------------------- check gate


def test_check_gate_retries_with_feedback_then_merges(factory_root: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    pipeline(
        factory_root, "c",
        f"  - id: work\n    agent: writer\n{CHECK_GATE}"
        "    on_fail: {retry: 2}\n",
    )  # fmt: skip
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script(
        [
            call("writer", "fs.write", path="ok.txt", content="no"),
            say("writer"),
            call("writer", "fs.write", path="ok.txt", content="yes"),
            say("writer"),
        ]
    )
    run = orch.run(orch.create_run("x", pipeline="c", model_override="echo").id)
    assert run.status == "passed", run.error
    assert [e.data["retry"] for e in of_type(events, "stage.retry")] == [1]
    gates = [e.data["passed"] for e in of_type(events, "gate.evaluated")]
    assert gates == [False, True]
    retry_prompt = user_messages(fake, "writer")[1]
    assert "previous attempt failed" in retry_prompt and "exited 1" in retry_prompt
    report = json.loads(orch.artifacts(run.id).read("work_check.json"))
    assert report["passed"] is True and report["backend"] == "subprocess"
    repo = RunRepo(factory_root / "workspaces", run.id)
    assert run_git(["show", f"{repo.integration}:ok.txt"], repo.repo) == "yes"
    assert len([e for e in of_type(events, "workspace.landed")]) == 1  # merged once, after pass


def test_failing_check_never_merges_and_fails_run(factory_root: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    pipeline(
        factory_root, "c",
        f"  - id: work\n    agent: writer\n{CHECK_GATE}",
    )  # fmt: skip
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([call("writer", "fs.write", path="ok.txt", content="no"), say("writer")])
    run = orch.run(orch.create_run("x", pipeline="c", model_override="echo").id)
    assert run.status == "failed" and "exited 1" in str(run.error)
    repo = RunRepo(factory_root / "workspaces", run.id)
    assert "ok.txt" not in repo.files_on_integration()


def test_retry_exhausted_continues_with_then(factory_root: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    write_agent(factory_root, "judge", JUDGE)
    pipeline(
        factory_root, "t",
        f"  - id: work\n    agent: writer\n{CHECK_GATE}"
        "    on_fail: {retry: 1, then: judge}\n"
        "  - {id: skipped_one, agent: judge}\n"
        "  - {id: judge, agent: judge}\n",
    )  # fmt: skip
    orch, fake, events = make_orchestrator(factory_root)
    attempt = [call("writer", "fs.write", path="ok.txt", content="no"), say("writer")]
    fake.add_script([*attempt, *attempt, say("judge", "{}")])  # first try + 1 retry
    run = orch.run(orch.create_run("x", pipeline="t", model_override="echo").id)
    assert run.status == "passed", run.error
    statuses = {s.stage_id: s.status for s in orch.store.stages(run.id)}
    assert statuses == {"work": "failed", "skipped_one": "pending", "judge": "passed"}
    assert of_type(events, "stage.continue")[0].data["then"] == "judge"
    repo = RunRepo(factory_root / "workspaces", run.id)
    assert "ok.txt" in repo.files_on_integration()  # carried forward for later stages to see


# ------------------------------------------------------------- loops & conditions


def _loop_pipeline(root: Path, max_loops: int) -> None:
    write_agent(root, "maker", "model: echo\noutputs: [work.md]\n")
    write_agent(root, "judge", "model: echo\ninputs: [work.md]\noutputs: [verdict.json]\n")
    pipeline(
        root, "loop",
        "  - {id: make, agent: maker}\n"
        "  - id: judge\n    agent: judge\n"
        "    pass_when: {artifact: verdict.json, path: verdict, equals: pass}\n"
        f"    on_fail: {{goto: make, max_loops: {max_loops}}}\n",
    )  # fmt: skip


def test_goto_loop_until_pass(factory_root: Path) -> None:
    _loop_pipeline(factory_root, max_loops=2)
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script(
        [say("maker", "v1"), verdict("judge", "fail"), say("maker", "v2"), verdict("judge", "pass")]
    )
    run = orch.run(orch.create_run("x", pipeline="loop").id)
    assert run.status == "passed", run.error
    [loop] = of_type(events, "stage.loop")
    assert loop.data == {"goto": "make", "loop": 1, "max_loops": 2}
    second_make = user_messages(fake, "maker")[1]
    assert "sent work back" in second_make and "verdict.json:verdict == 'pass'" in second_make
    assert orch.store.run_state(run.id)["loops"] == {"judge": 1}


def test_goto_loop_is_bounded(factory_root: Path) -> None:
    _loop_pipeline(factory_root, max_loops=1)
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([{**verdict("judge", "fail"), "repeat": True}])
    run = orch.run(orch.create_run("x", pipeline="loop").id)
    assert run.status == "failed"
    assert "still failing after 1 loop(s) back to 'make'" in str(run.error)
    assert len(user_messages(fake, "judge")) == 2


def test_when_skips_stage(factory_root: Path) -> None:
    write_agent(factory_root, "judge", JUDGE)
    pipeline(
        factory_root, "w",
        "  - {id: a, agent: judge}\n"
        "  - {id: b, agent: judge, when: {exists: nothing.md}}\n"
        "  - {id: c, agent: judge, when: {artifact: verdict.json, path: ok, equals: true}}\n",
    )  # fmt: skip
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script([say("judge", '{"ok": true}'), say("judge", '{"ok": true}')])
    run = orch.run(orch.create_run("x", pipeline="w").id)
    assert run.status == "passed"
    statuses = [s.status for s in orch.store.stages(run.id)]
    assert statuses == ["passed", "skipped", "passed"]
    assert "nothing.md exists: no" in of_type(events, "stage.skipped")[0].data["why"][0]


# ---------------------------------------------------------------- artifact_valid


def test_artifact_valid_gate(factory_root: Path) -> None:
    write_agent(factory_root, "reviewer2", "model: echo\noutputs: [review.json]\n")
    pipeline(
        factory_root, "v",
        "  - id: r\n    agent: reviewer2\n"
        "    gate:\n      type: artifact_valid\n      artifact: review.json\n"
        "      schema: agents/reviewer/review.schema.json\n"
        "    on_fail: {retry: 1}\n",
    )  # fmt: skip
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script(
        [
            say("reviewer2", '{"verdict": "maybe", "summary": "", "findings": []}'),
            say("reviewer2", '```json\n{"verdict": "pass", "summary": "ok", "findings": []}\n```'),
        ]
    )
    run = orch.run(orch.create_run("x", pipeline="v").id)
    assert run.status == "passed", run.error
    first = of_type(events, "gate.evaluated")[0].data
    assert first["passed"] is False
    assert any("'maybe' is not one of" in d for d in first["detail"])
    assert any("shorter than 1" in d for d in first["detail"])
    assert "invalid artifact" in user_messages(fake, "reviewer2")[1]


# ---------------------------------------------------------------------- parallel


def test_parallel_branches_run_concurrently(
    factory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_agent(factory_root, "left", "model: echo\noutputs: [left.md]\n")
    write_agent(factory_root, "right", "model: echo\noutputs: [right.md]\n")
    pipeline(
        factory_root,
        "p",
        "  - id: both\n    parallel:\n      - agent: left\n      - agent: right\n",
    )
    original = FakeProvider.complete
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow(self: FakeProvider, *a: Any, **k: Any) -> Any:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.4)
        with lock:
            active[0] -= 1
        return original(self, *a, **k)

    monkeypatch.setattr(FakeProvider, "complete", slow)
    orch, _, events = make_orchestrator(factory_root)
    run = orch.run(orch.create_run("x", pipeline="p").id)
    assert run.status == "passed", run.error
    assert peak[0] == 2  # both branches were in flight at the same time
    assert {e.agent for e in of_type(events, "agent.finished")} == {"left", "right"}
    assert set(orch.artifacts(run.id).list()) >= {"left.md", "right.md"}


def test_parallel_failure_fails_stage(factory_root: Path) -> None:
    write_agent(factory_root, "left", "model: echo\noutputs: [left.md]\n")
    write_agent(factory_root, "right", "model: echo\noutputs: [right.md, extra.md]\n")
    pipeline(
        factory_root,
        "p",
        "  - id: both\n    parallel:\n      - agent: left\n      - agent: right\n",
    )
    orch, _, _ = make_orchestrator(factory_root)
    run = orch.run(orch.create_run("x", pipeline="p").id)
    assert run.status == "failed" and "right: required output(s) not produced" in str(run.error)


# ----------------------------------------------------------------------- budgets


def test_budget_pauses_and_approval_extends(factory_root: Path) -> None:
    write_agent(factory_root, "judge", JUDGE)
    pipeline(
        factory_root, "b",
        "  - {id: a, agent: judge}\n  - {id: b, agent: judge}\n  - {id: c, agent: judge}\n",
        extra="budget: {tokens: 600}\n",
    )  # fmt: skip
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script([{**say("judge", "x " * 250), "repeat": True}])  # ~300 tokens per call
    run = orch.run(orch.create_run("x", pipeline="b").id)
    assert run.status == "waiting_approval"
    [approval] = orch.pending_approvals(run.id)
    assert approval.kind == "budget" and approval.details_dict["scope"] == "run"
    assert "limit 600" in approval.summary
    calls_before = len(fake.calls)
    orch.decide(run.id, "approved")
    run = orch.run(run.id)
    assert len(fake.calls) > calls_before
    assert orch.store.run_state(run.id)["allowance"] == {"run": 1}
    if run.status == "waiting_approval":  # second allowance exhausted too
        orch.decide(run.id, "rejected", "enough")
        run = orch.run(run.id)
        assert run.status == "failed" and "enough" in str(run.error)
    assert of_type(events, "budget.exceeded")


def test_stage_budget_stops_agent_mid_invocation(factory_root: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    pipeline(factory_root, "b", "  - {id: a, agent: writer, budget: {tokens: 50}}\n")
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([{**call("writer", "fs.read", path="nothing.txt"), "repeat": True}])
    run = orch.run(orch.create_run("x", pipeline="b", model_override="echo").id)
    assert run.status == "waiting_approval"
    [approval] = orch.pending_approvals(run.id)
    assert approval.details_dict["scope"] == "stage:a"
    assert "stage 'a' used" in approval.summary
    # The agent was stopped before its next LLM call, not after max_iterations.
    assert len(fake.calls) < 5


# ------------------------------------------------------------------ L3 network


NET = (
    "model: echo\nisolation_level: L3\ntools: [shell.run_sandboxed]\n"
    'fs_scope:\n  read: ["{workspace}/**"]\n'
)


def _approve_when_requested(orch: Any, run_id_box: list[str], decision: str) -> threading.Thread:
    def worker() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if run_id_box:
                pending = [a for a in orch.pending_approvals(run_id_box[0]) if a.kind == "network"]
                if pending:
                    orch.decide(run_id_box[0], decision, "ok", approval_id=pending[0].id)
                    return
            time.sleep(0.05)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def test_network_needs_human_approval_once_per_run(factory_root: Path) -> None:
    write_agent(factory_root, "net", NET)
    pipeline(factory_root, "n", "  - {id: fetch, agent: net}\n")
    orch, fake, _events = make_orchestrator(factory_root)
    fake.add_script(
        [
            call("net", "shell.run_sandboxed", command="python -V", network=True),
            call("net", "shell.run_sandboxed", command="python -V", network=True),
            say("net"),
        ]
    )
    run = orch.create_run("x", pipeline="n", model_override="echo")
    thread = _approve_when_requested(orch, [run.id], "approved")
    run = orch.run(run.id)
    thread.join(5)
    assert run.status == "passed", run.error
    network = [a for a in orch.store.approvals(run.id) if a.kind == "network"]
    assert len(network) == 1 and network[0].status == "approved"  # asked only once
    assert orch.store.run_state(run.id)["network"] == "approved"
    # Approved, but the subprocess fallback still cannot isolate network: refused.
    results = [m.content for m in fake.calls[-1].messages if m.role == "tool"]
    assert all("networked commands require the Docker backend" in r for r in results)


def test_network_request_times_out(factory_root: Path) -> None:
    yaml_path = factory_root / "config" / "factory.yaml"
    yaml_path.write_text(
        yaml_path.read_text("utf-8").replace(
            "network_approval_timeout_s: 600", "network_approval_timeout_s: 1"
        ),
        "utf-8",
    )
    write_agent(factory_root, "net", NET)
    pipeline(factory_root, "n", "  - {id: fetch, agent: net}\n")
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script(
        [call("net", "shell.run_sandboxed", command="pip install x", network=True), say("net")]
    )
    run = orch.run(orch.create_run("x", pipeline="n", model_override="echo").id)
    assert run.status == "passed"
    [approval] = orch.store.approvals(run.id)
    assert approval.status == "rejected" and "timed out" in str(approval.comment)
    [result] = [m.content for m in fake.calls[-1].messages if m.role == "tool"]
    assert "network access was not approved" in result


# ------------------------------------------------------------------ builtins


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    run_git(["init", "-q", "-b", "main"], target)
    (target / "README.md").write_text("# target\n", "utf-8")
    run_git(["add", "."], target)
    run_git(["commit", "-q", "-m", "init"], target)
    return target


def test_merge_waits_for_approval_then_delivers_branch(factory_root: Path, tmp_path: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    pipeline(
        factory_root, "m",
        "  - {id: work, agent: writer}\n"
        "  - {id: integrate, type: builtin.merge, gate: human_approval}\n",
    )  # fmt: skip
    target = _target(tmp_path)
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script(
        [call("writer", "fs.write", path="app.py", content="print(1)\n"), say("writer")]
    )
    run = orch.run(orch.create_run("x", pipeline="m", repo=str(target), model_override="echo").id)
    assert run.status == "waiting_approval"
    [approval] = orch.pending_approvals(run.id)
    assert approval.details_dict["phase"] == "before"
    assert not of_type(events, "run.delivered")  # nothing delivered before approval
    orch.decide(run.id, "approved")
    run = orch.run(run.id)
    assert run.status == "passed", run.error
    delivery = json.loads(orch.artifacts(run.id).read("delivery.json"))
    assert delivery["branch"] == f"factory/{run.id}"
    assert run_git(["show", f"factory/{run.id}:app.py"], target) == "print(1)\n"
    # The target's checked-out branch and working tree are untouched.
    assert run_git(["branch", "--show-current"], target).strip() == "main"
    assert not (target / "app.py").exists()


def test_merge_new_project_tags_delivery(factory_root: Path) -> None:
    pipeline(factory_root, "m", "  - {id: integrate, type: builtin.merge}\n")
    orch, _, _ = make_orchestrator(factory_root)
    run = orch.run(orch.create_run("x", pipeline="m").id)
    assert run.status == "passed", run.error
    delivery = json.loads(orch.artifacts(run.id).read("delivery.json"))
    assert "git clone" in delivery["next_step"]


def test_builtin_shell_and_lint(factory_root: Path) -> None:
    pipeline(
        factory_root, "s",
        "  - {id: ok, type: builtin.shell, command: 'python -c \"print(42)\"'}\n"
        "  - {id: bad, type: builtin.shell, command: 'python -c \"raise SystemExit(3)\"'}\n",
    )  # fmt: skip
    orch, _, _ = make_orchestrator(factory_root)
    run = orch.run(orch.create_run("x", pipeline="s").id)
    assert run.status == "failed" and "exited 3" in str(run.error)
    report = json.loads(orch.artifacts(run.id).read("ok.json"))
    assert report["passed"] and "42" in report["stdout_tail"]


# --------------------------------------------------------------------- resume


def test_crash_mid_pipeline_resumes_from_last_completed_stage(factory_root: Path) -> None:
    for name in ("one", "two", "three"):
        write_agent(factory_root, name, f"model: echo\noutputs: [{name}.md]\n")
    pipeline(
        factory_root, "r",
        "  - {id: s1, agent: one}\n  - {id: s2, agent: two}\n  - {id: s3, agent: three}\n",
    )  # fmt: skip
    orch, _fake, _ = make_orchestrator(factory_root)
    run = orch.create_run("x", pipeline="r")

    def crash(event: Event) -> None:
        if event.type == "agent.finished" and event.agent == "two":
            raise KeyboardInterrupt  # simulates the process dying mid-stage

    orch.bus.subscribe(crash)
    with pytest.raises(KeyboardInterrupt):
        orch.run(run.id)
    assert orch.store.stage(run.id, "s2").status == "running"  # type: ignore[union-attr]

    orch2, fake2, _ = make_orchestrator(factory_root)  # "restart"
    final = orch2.run(run.id)
    assert final.status == "passed"
    assert [c.metadata["agent"] for c in fake2.calls] == ["two", "three"]


def test_restart_while_waiting_keeps_waiting(factory_root: Path) -> None:
    write_agent(factory_root, "judge", JUDGE)
    pipeline(factory_root, "g", "  - {id: a, agent: judge, gate: human_approval}\n")
    orch, _, _ = make_orchestrator(factory_root)
    run = orch.run(orch.create_run("x", pipeline="g").id)
    orch2, fake2, _ = make_orchestrator(factory_root)
    assert orch2.run(run.id).status == "waiting_approval"
    assert fake2.calls == []
    orch2.decide(run.id, "approved")
    assert orch2.run(run.id).status == "passed"


def test_default_pipeline_is_runnable(factory_root: Path) -> None:
    orch, _, _ = make_orchestrator(factory_root)
    pipelines, errors = orch.pipelines()
    assert errors == {}
    for spec in pipelines.values():
        orch.check_runnable(spec)  # raises if anything is missing (agents, schemas, git)
