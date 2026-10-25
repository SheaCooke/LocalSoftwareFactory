from __future__ import annotations

from pathlib import Path

import pytest
from helpers import make_orchestrator, of_type

from factory.core.pipeline import PipelineError


def test_quick_pipeline_end_to_end(factory_root: Path) -> None:
    orch, fake, events = make_orchestrator(factory_root)
    run = orch.create_run("Build a CLI todo app", pipeline="quick", model_override="echo")
    assert run.status == "pending"
    run = orch.run(run.id)
    assert run.status == "passed", run.error
    assert [s.status for s in orch.store.stages(run.id)] == ["passed"] * 3
    store = orch.artifacts(run.id)
    assert store.list() == ["design.md", "plan.md", "request.md", "requirements.md"]
    assert [c.metadata["agent"] for c in fake.calls] == ["requirements", "architect", "planner"]
    # The architect saw requirements.md (its input), produced by the previous stage.
    assert store.read("requirements.md").strip() in fake.calls[1].messages[1].content
    usage = {u.agent: u for u in orch.store.usage_by_agent(run.id)}
    assert set(usage) == {"requirements", "architect", "planner"}
    assert all(u.calls == 1 and u.total_tokens > 0 for u in usage.values())
    transitions = [(e.stage, e.data["to"]) for e in of_type(events, "stage.transition")]
    assert transitions == [
        ("requirements", "running"),
        ("requirements", "passed"),
        ("design", "running"),
        ("design", "passed"),
        ("plan", "running"),
        ("plan", "passed"),
    ]
    logged_types = {e["type"] for e in orch.event_log.read(run.id)}
    assert {"run.created", "stage.transition", "llm.call", "agent.finished"} <= logged_types
    assert len(list((orch.run_dir(run.id) / "transcripts").glob("*.json"))) == 3


def test_resume_after_crash_skips_completed_stages(factory_root: Path) -> None:
    orch, _fake, _ = make_orchestrator(factory_root)
    run = orch.create_run("x", pipeline="quick", model_override="echo")
    # Simulate: requirements finished, then the process died while design was running.
    orch.artifacts(run.id).write("requirements.md", "REQS")
    orch.store.set_stage(run.id, "requirements", "passed")
    orch.store.set_stage(run.id, "design", "running")
    orch.store.update_run(run.id, status="running", current_stage="design")

    orch2, fake2, _ = make_orchestrator(factory_root)  # "restart"
    final = orch2.run(run.id)
    assert final.status == "passed"
    assert [c.metadata["agent"] for c in fake2.calls] == ["architect", "planner"]
    assert orch2.artifacts(run.id).read("requirements.md") == "REQS"
    assert orch2.store.stage(run.id, "design").attempts == 2  # type: ignore[union-attr]


def test_failed_stage_fails_run_and_resume_retries_it(factory_root: Path) -> None:
    orch, fake, _ = make_orchestrator(factory_root)
    run = orch.create_run("x", pipeline="quick", model_override="echo")
    agent_md = factory_root / "agents" / "architect" / "agent.md"
    good = agent_md.read_text("utf-8")
    agent_md.write_text("broken, no frontmatter", "utf-8")
    failed = orch.run(run.id)
    assert failed.status == "failed"
    assert "stage 'design'" in str(failed.error) and "frontmatter" in str(failed.error)
    assert orch.store.stage(run.id, "plan").status == "pending"  # type: ignore[union-attr]

    agent_md.write_text(good, "utf-8")  # fix the definition; no restart needed
    resumed = orch.resume(run.id)
    assert resumed.status == "passed"
    assert [c.metadata["agent"] for c in fake.calls] == ["requirements", "architect", "planner"]


def test_agent_edits_apply_on_next_invocation(factory_root: Path) -> None:
    orch, fake, _ = make_orchestrator(factory_root)
    run = orch.create_run("x", pipeline="quick", model_override="echo")
    planner = factory_root / "agents" / "planner" / "agent.md"
    planner.write_text(
        planner.read_text("utf-8").replace("# Role", "# Role\nEDITED-PROMPT-MARKER", 1), "utf-8"
    )
    orch.run(run.id)
    assert "EDITED-PROMPT-MARKER" in fake.calls[2].messages[0].content


def test_model_switch_between_stages_applies_to_next_call(factory_root: Path) -> None:
    orch, _fake, events = make_orchestrator(factory_root)
    models = factory_root / "config" / "models.yaml"
    models.write_text(
        models.read_text("utf-8") + "\n  echo-b:\n    provider: fake\n    model: echo-b\n", "utf-8"
    )
    orch.live.reload()
    # No per-run override: agents resolve via their agent.md default unless overridden.
    for agent in ("requirements", "architect", "planner"):
        orch.live.set_model("echo", agent=agent)
    run = orch.create_run("x", pipeline="quick")

    def switch(event: object) -> None:
        if (
            getattr(event, "type", "") == "agent.finished"
            and getattr(event, "agent", "") == "requirements"
        ):
            orch.live.set_model("echo-b", agent="architect")

    orch.bus.subscribe(switch)
    orch.run(run.id)
    used = [(e.agent, e.data["alias"]) for e in of_type(events, "llm.call")]
    assert used == [("requirements", "echo"), ("architect", "echo-b"), ("planner", "echo")]


def test_missing_schema_file_blocks_run(factory_root: Path) -> None:
    (factory_root / "agents" / "reviewer" / "review.schema.json").unlink()
    orch, _, _ = make_orchestrator(factory_root)
    with pytest.raises(PipelineError, match=r"schema 'agents/reviewer/review\.schema\.json'"):
        orch.create_run("x", pipeline="default")
    assert orch.store.list_runs() == []


def test_create_run_validation(factory_root: Path) -> None:
    orch, _, _ = make_orchestrator(factory_root)
    with pytest.raises(PipelineError, match="not found"):
        orch.create_run("x", pipeline="nope")
    with pytest.raises(PipelineError, match="empty"):
        orch.create_run("   ", pipeline="quick")
    (factory_root / "agents" / "planner" / "agent.md").write_text("bad", "utf-8")
    with pytest.raises(PipelineError, match="agent 'planner' is invalid"):
        orch.create_run("x", pipeline="quick")


def test_pipeline_snapshot_protects_running_runs(factory_root: Path) -> None:
    orch, fake, _ = make_orchestrator(factory_root)
    run = orch.create_run("x", pipeline="quick", model_override="echo")
    (factory_root / "config" / "pipelines" / "quick.yaml").write_text(
        "name: quick\nstages:\n  - {id: only, agent: planner}\n", "utf-8"
    )
    assert orch.run(run.id).status == "passed"
    assert len(fake.calls) == 3
