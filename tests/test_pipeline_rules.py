"""Pipeline schema rules, conditions, the JSON Schema subset, and store migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from factory.core.artifacts import ArtifactStore
from factory.core.conditions import evaluate
from factory.core.jsonschema_lite import validate
from factory.core.pipeline import Condition, PipelineError, parse_pipeline
from factory.core.store import RunStore

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("stages", "message"),
    [
        ("  - {id: a, agent: x, on_fail: {then: a}}\n", "later stage"),
        ("  - {id: a, agent: x}\n  - {id: b, agent: x, on_fail: {then: a}}\n", "later stage"),
        ("  - {id: a, agent: x, on_fail: {goto: b}}\n  - {id: b, agent: x}\n", "earlier stage"),
        ("  - {id: a, agent: x, gate: {type: check, before: true, command: c}}\n", "only valid"),
        ("  - {id: a, agent: x, gate: {type: artifact_valid, artifact: r.json}}\n", "requires"),
        ("  - {id: a, agent: x, gate: {artifact: r.json}}\n", "together"),
        ("  - {id: a, agent: x, when: {artifact: r.json}}\n", "exactly one of equals"),
        ("  - {id: a, agent: x, when: {exists: a, artifact: b, equals: 1}}\n", "exactly one of"),
        ("  - {id: a, parallel: [{agent: x}, {agent: x}]}\n", "only once"),
        ("  - {id: a, agent: x, command: ls}\n", "only valid on shell"),
    ],
)
def test_invalid_pipelines(stages: str, message: str) -> None:
    with pytest.raises(PipelineError, match=message):
        parse_pipeline(f"name: p\nstages:\n{stages}")


def test_stage_helpers() -> None:
    spec = parse_pipeline(
        "name: p\nstages:\n"
        "  - {id: a, agent: x, model: fast}\n"
        "  - {id: b, parallel: [{agent: y}, {agent: z, model: smart}], model: echo}\n"
        "  - {id: c, type: builtin.lint}\n"
        "  - {id: d, type: builtin.merge, gate: human_approval}\n"
        "  - {id: e, agent: x, gate: {type: human_approval, before: true}}\n"
    )
    assert [(b.agent, b.model) for b in spec.stage("a").branches] == [("x", "fast")]
    assert [(b.agent, b.model) for b in spec.stage("b").branches] == [("y", "echo"), ("z", "smart")]
    assert spec.stage("c").shell_command == "ruff check ."
    assert spec.stage("d").approval_before and spec.stage("e").approval_before
    assert spec.between("b", "d") == ["b", "c", "d"] and spec.next_id("e") is None


def test_default_pipeline_shape() -> None:
    spec = parse_pipeline((REPO / "config" / "pipelines" / "default.yaml").read_text("utf-8"))
    review = spec.stage("review")
    assert [c.describe() for c in review.pass_when] == [
        "review.json:verdict == 'pass'",
        "security_report.json:verdict == 'pass'",
    ]
    assert {s.artifact for s in review.gate.schemas} == {"review.json", "security_report.json"}
    assert spec.stage("implement").gate.report == "test_report.json"
    assert spec.stage("integrate").approval_before


# --------------------------------------------------------------------- conditions


def test_conditions(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.write("r.json", json.dumps({"verdict": "pass", "findings": [{"severity": "minor"}]}))
    store.write("bad.json", "not json")
    cases = [
        (Condition(exists="r.json"), True),
        (Condition(exists="nope.md"), False),
        (Condition(artifact="r.json", path="verdict", equals="pass"), True),
        (Condition(artifact="r.json", path="verdict", not_equals="pass"), False),
        (Condition(artifact="r.json", path="findings.0.severity", in_=["minor", "nit"]), True),
        (Condition(artifact="r.json", path="findings.5.severity", equals="x"), False),
        (Condition(artifact="bad.json", path="x", equals=1), False),
        (Condition(artifact="missing.json", path="x", equals=1), False),
    ]
    for cond, expected in cases:
        holds, why = evaluate(cond, store)
        assert holds is expected, why
    assert "not valid JSON" in evaluate(Condition(artifact="bad.json", equals=1), store)[1]


# --------------------------------------------------------------- JSON Schema lite


SCHEMA = {
    "type": "object",
    "required": ["verdict", "items"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"enum": ["pass", "fail"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "note": {"type": ["string", "null"], "maxLength": 5},
        "items": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["id"]}},
        "tag": {"type": "string", "pattern": "^[a-z]+$"},
        "either": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
    },
}


def test_schema_accepts_valid() -> None:
    doc = {
        "verdict": "pass",
        "score": 3,
        "note": None,
        "items": [{"id": 1}],
        "tag": "ok",
        "either": 1,
    }
    assert validate(doc, SCHEMA) == []


@pytest.mark.parametrize(
    ("doc", "error"),
    [
        ({"items": [{"id": 1}]}, "missing required property 'verdict'"),
        ({"verdict": "meh", "items": [{"id": 1}]}, "is not one of"),
        ({"verdict": "pass", "items": []}, "fewer than 1 items"),
        ({"verdict": "pass", "items": [{}]}, "$.items[0]: missing required property 'id'"),
        ({"verdict": "pass", "items": [{"id": 1}], "score": 11}, "greater than maximum"),
        ({"verdict": "pass", "items": [{"id": 1}], "score": True}, "expected integer"),
        ({"verdict": "pass", "items": [{"id": 1}], "note": "toolong"}, "longer than 5"),
        ({"verdict": "pass", "items": [{"id": 1}], "tag": "BAD"}, "does not match pattern"),
        ({"verdict": "pass", "items": [{"id": 1}], "extra": 1}, "unexpected property 'extra'"),
        ({"verdict": "pass", "items": [{"id": 1}], "either": []}, "anyOf"),
        ([], "expected object"),
    ],
)
def test_schema_rejects(doc: object, error: str) -> None:
    errors = validate(doc, SCHEMA)
    assert any(error in e for e in errors), errors


@pytest.mark.parametrize(
    "schema", ["agents/reviewer/review.schema.json", "agents/security/security_report.schema.json"]
)
def test_shipped_report_schemas(schema: str) -> None:
    loaded = json.loads((REPO / schema).read_text("utf-8"))
    assert validate({"verdict": "pass", "summary": "fine", "findings": []}, loaded) == []
    assert validate({"verdict": "pass", "summary": "", "findings": []}, loaded)
    assert validate({"verdict": "ok", "summary": "x", "findings": []}, loaded)


# ------------------------------------------------------------------ migration


def test_store_migrates_old_database(tmp_path: Path) -> None:
    db_path = tmp_path / "factory.db"
    with sqlite3.connect(db_path) as db:  # the milestone-2 schema: no `state` column
        db.execute(
            "CREATE TABLE runs (id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL"
            " NOT NULL, request TEXT NOT NULL, pipeline TEXT NOT NULL, repo TEXT, status TEXT"
            " NOT NULL, model_override TEXT, current_stage TEXT, error TEXT)"
        )
        db.execute(
            "INSERT INTO runs VALUES ('old', 1, 1, 'r', 'quick', NULL, 'passed', NULL, NULL, NULL)"
        )
    store = RunStore(db_path)
    old = store.get_run("old")
    assert old is not None and old.state_dict == {}
    store.save_state("old", {"cursor": None, "loops": {"a": 1}})
    assert store.run_state("old")["loops"] == {"a": 1}
    assert store.approvals("old") == []
