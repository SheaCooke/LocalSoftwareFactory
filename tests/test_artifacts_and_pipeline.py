from __future__ import annotations

from pathlib import Path

import pytest

from factory.core.artifacts import ArtifactError, ArtifactStore
from factory.core.events import EventBus, JsonlEventLog
from factory.core.pipeline import PipelineError, load_pipelines, parse_pipeline
from factory.secrets import redactor

REPO_PIPELINES = Path(__file__).resolve().parents[1] / "config" / "pipelines"


# ------------------------------------------------------------------------- artifacts


def test_artifact_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "a")
    store.write("plan.md", "# Plan\n")
    store.write("reports/lint.json", "{}")
    assert store.read("plan.md") == "# Plan\n"
    assert store.list() == ["plan.md", "reports/lint.json"]
    assert store.read_optional("missing.md") is None


@pytest.mark.parametrize(
    "name", ["../x.md", "a/../../x.md", "/etc/passwd", "C:\\Windows\\x", "a\\b.md", "", "./x"]
)
def test_artifact_names_cannot_escape(tmp_path: Path, name: str) -> None:
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path / "a").write(name, "x")


# ---------------------------------------------------------------------------- events


def test_event_log_is_redacted_jsonl(tmp_path: Path) -> None:
    redactor.add("sk-live-abcdef123456")
    bus = EventBus()
    log = JsonlEventLog(tmp_path)
    bus.subscribe(log)
    bus.subscribe(lambda e: 1 / 0)  # a broken subscriber must not break publishing
    bus.emit("tool.call", run_id="r1", data={"arguments": {"key": "sk-live-abcdef123456"}})
    bus.emit("config.reloaded")  # no run_id: not written
    [event] = log.read("r1")
    assert event["type"] == "tool.call"
    assert event["data"]["arguments"]["key"] == "[REDACTED]"


# -------------------------------------------------------------------------- pipelines


def test_shipped_pipelines_parse() -> None:
    pipelines, errors = load_pipelines(REPO_PIPELINES)
    assert errors == {}
    default = pipelines["default"]
    assert [s.id for s in default.stages][:3] == ["requirements", "design", "plan"]
    review = default.stage("review")
    assert review.type == "parallel" and review.agents == ["reviewer", "security"]
    assert review.on_fail is not None and review.on_fail.goto == "implement"
    implement = default.stage("implement")
    assert implement.gate.type == "check" and implement.gate.command == "pytest -q"
    assert default.stage("requirements").gate.type == "human_approval"
    assert default.stage("integrate").type == "builtin.merge"
    assert default.referenced_agents() >= {"tester", "implementer", "security"}


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("name: p\nstages: []\n", "stages"),
        ("name: p\nstages:\n  - id: a\n", "requires `agent`"),
        ("name: p\nstages:\n  - {id: a, agent: x}\n  - {id: a, agent: y}\n", "duplicate stage"),
        ("name: p\nstages:\n  - {id: a, agent: x, on_fail: {goto: nope}}\n", "not found"),
        ("name: p\nstages:\n  - {id: a, agent: x, gate: {type: check}}\n", "requires `command`"),
        ("name: p\nstages:\n  - {id: a, agent: x, gate: maybe}\n", "gate"),
        ("name: p\nstages:\n  - {id: a, agent: x, on_fail: {then: a, goto: a}}\n", "not both"),
        ("name: p\nstages:\n  - {id: A!, agent: x}\n", "stage id"),
        ("- just a list\n", "mapping"),
    ],
)
def test_invalid_pipelines(yaml_text: str, message: str) -> None:
    with pytest.raises(PipelineError, match=message):
        parse_pipeline(yaml_text)


def test_pipeline_name_must_match_file(tmp_path: Path) -> None:
    (tmp_path / "mine.yaml").write_text("name: other\nstages: [{id: a, agent: x}]\n", "utf-8")
    pipelines, errors = load_pipelines(tmp_path)
    assert pipelines == {} and "must match the file name" in errors["mine.yaml"]
