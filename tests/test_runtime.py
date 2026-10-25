from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import context, make_orchestrator, of_type, write_agent

WRITER = (
    "model: echo\nisolation_level: L1\ntools: [artifact.write, artifact.read]\n"
    "inputs: [plan.md]\noutputs: [out.md]\n"
)
TAGGED = '<artifact name="a.md">A</artifact>\n<artifact name="b.json">{}</artifact>'


def _call(tool: str, /, **arguments: object) -> dict[str, object]:
    return {"response": {"tool_calls": [{"name": tool, "arguments": arguments}]}}


def test_l0_final_reply_becomes_the_artifact(factory_root: Path) -> None:
    write_agent(factory_root, "solo", "model: echo\noutputs: [answer.md]\n")
    orch, fake, events = make_orchestrator(factory_root)
    fake.add_script([{"response": {"content": "```markdown\n# Answer\n42\n```"}}])
    result = orch.runtime.invoke(orch.agents.agents["solo"], context(orch))
    assert result.status == "passed", result.error
    assert orch.artifacts("r1").read("answer.md") == "# Answer\n42\n"  # outer fence stripped
    assert fake.calls[0].tools == []  # L0: no tools offered
    [llm] = of_type(events, "llm.call")
    assert llm.data["alias"] == "echo" and llm.agent == "solo"
    transcript = json.loads(Path(str(result.transcript)).read_text("utf-8"))
    assert transcript["status"] == "passed" and transcript["messages"][0]["role"] == "system"


def test_l0_multiple_outputs_from_tags(factory_root: Path) -> None:
    write_agent(factory_root, "multi", "model: echo\noutputs: [a.md, b.json]\n")
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([{"response": {"content": TAGGED}}])
    result = orch.runtime.invoke(orch.agents.agents["multi"], context(orch))
    assert result.status == "passed", result.error
    assert orch.artifacts("r1").read("b.json") == "{}\n"


def test_tool_loop_writes_output(factory_root: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    orch, fake, events = make_orchestrator(factory_root)
    orch.artifacts("r1").write("plan.md", "the plan")
    fake.add_script(
        [
            _call("artifact.read", name="plan.md"),
            _call("artifact.write", name="out.md", content="done"),
            {"response": {"content": "Summary: wrote out.md"}},
        ]
    )
    result = orch.runtime.invoke(orch.agents.agents["writer"], context(orch))
    assert result.status == "passed", result.error
    assert result.iterations == 3
    assert orch.artifacts("r1").read("out.md") == "done"  # not overwritten by the final summary
    assert {t.name for t in fake.calls[0].tools} == {"artifact.read", "artifact.write"}
    tool_msgs = [m for m in fake.calls[1].messages if m.role == "tool"]
    assert tool_msgs[0].content == "the plan"
    assert all(e.data["ok"] for e in of_type(events, "tool.call"))


def test_out_of_scope_artifact_access_is_denied_and_logged(factory_root: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    orch, fake, events = make_orchestrator(factory_root)
    orch.artifacts("r1").write("secret_plan.md", "not yours")
    fake.add_script(
        [
            _call("artifact.read", name="secret_plan.md"),
            _call("artifact.write", name="requirements.md", content="overwrite!"),
            _call("fs.write", path="x", content="y"),
            _call("artifact.write", name="out.md", content="ok"),
            {"response": {"content": "done"}},
        ]
    )
    result = orch.runtime.invoke(orch.agents.agents["writer"], context(orch))
    assert result.status == "passed"
    denials = of_type(events, "permission.decision")
    assert [d.data["tool"] for d in denials] == ["artifact.read", "artifact.write", "fs.write"]
    assert all(d.data["decision"] == "denied" and d.agent == "writer" for d in denials)
    replies = [m.content for m in fake.calls[-1].messages if m.role == "tool"]
    assert replies[0].startswith("ERROR: permission denied")
    assert not orch.artifacts("r1").exists("requirements.md")
    # Denials land in the run's JSONL log too.
    logged = [e for e in orch.event_log.read("r1") if e["type"] == "permission.decision"]
    assert len(logged) == 3


def test_bad_arguments_are_reported_to_the_model(factory_root: Path) -> None:
    write_agent(factory_root, "writer", WRITER)
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script(
        [
            _call("artifact.write", name="out.md"),  # missing content
            _call("artifact.write", name="out.md", content=5),  # wrong type
            _call("artifact.write", name="out.md", content="fine"),
            {"response": {"content": "done"}},
        ]
    )
    result = orch.runtime.invoke(orch.agents.agents["writer"], context(orch))
    assert result.status == "passed"
    replies = [m.content for m in fake.calls[-1].messages if m.role == "tool"]
    assert "missing required argument(s): content" in replies[0]
    assert "must be of type string" in replies[1]


def test_max_iterations(factory_root: Path) -> None:
    write_agent(factory_root, "looper", WRITER.replace("outputs", "max_iterations: 3\noutputs"))
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([{**_call("artifact.list"), "repeat": True}])
    result = orch.runtime.invoke(orch.agents.agents["looper"], context(orch))
    assert result.status == "failed" and "max_iterations=3" in str(result.error)


def test_token_budget(factory_root: Path) -> None:
    write_agent(factory_root, "greedy", "model: echo\nmax_tokens_budget: 5\noutputs: [o.md]\n")
    orch, _, _ = make_orchestrator(factory_root)
    result = orch.runtime.invoke(orch.agents.agents["greedy"], context(orch))
    assert result.status == "failed" and "token budget exceeded" in str(result.error)


def test_missing_outputs_fail(factory_root: Path) -> None:
    write_agent(factory_root, "lazy", WRITER.replace("[out.md]", "[out.md, extra.md]"))
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([{"response": {"content": "I did nothing"}}])
    result = orch.runtime.invoke(orch.agents.agents["lazy"], context(orch))
    assert result.status == "failed"
    assert "not produced: out.md, extra.md" in str(result.error)


def test_unregistered_tool_fails_cleanly(factory_root: Path) -> None:
    write_agent(factory_root, "future", "isolation_level: L2\ntools: [nonexistent.tool]\n")
    orch, _, _ = make_orchestrator(factory_root)
    result = orch.runtime.invoke(orch.agents.agents["future"], context(orch))
    assert result.status == "failed" and "unknown tool 'nonexistent.tool'" in str(result.error)


def test_context_contains_only_declared_inputs(factory_root: Path) -> None:
    write_agent(factory_root, "reader", "model: echo\ninputs: [plan.md]\noutputs: [o.md]\n")
    orch, fake, _ = make_orchestrator(factory_root)
    store = orch.artifacts("r1")
    store.write("plan.md", "PLAN-CONTENT")
    store.write("design.md", "DESIGN-CONTENT")
    orch.runtime.invoke(orch.agents.agents["reader"], context(orch))
    [call] = fake.calls
    assert len(call.messages) == 2  # fresh conversation: system + task
    text = "\n".join(m.content for m in call.messages)
    assert "PLAN-CONTENT" in text and "DESIGN-CONTENT" not in text
    assert "Build a thing" in text


def test_transcripts_are_redacted(factory_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from factory.secrets import SecretStore

    monkeypatch.setenv("LEAKY_KEY", "sk-very-secret-value-1")
    SecretStore(factory_root).get("LEAKY_KEY")
    write_agent(factory_root, "leaker", "model: echo\noutputs: [o.md]\n")
    orch, fake, _ = make_orchestrator(factory_root)
    fake.add_script([{"response": {"content": "key is sk-very-secret-value-1"}}])
    result = orch.runtime.invoke(orch.agents.agents["leaker"], context(orch))
    raw = Path(str(result.transcript)).read_text("utf-8")
    assert "sk-very-secret-value-1" not in raw and "[REDACTED]" in raw
    events_raw = (orch.run_dir("r1") / "events.jsonl").read_text("utf-8")
    assert "sk-very-secret-value-1" not in events_raw
