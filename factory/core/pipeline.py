"""Pipeline definitions (config/pipelines/*.yaml): data the orchestrator interprets.

A stage runs its body (one agent, parallel agents, or a builtin), then:

1. An automated gate, if any: `check` (a sandboxed command) or `artifact_valid`
   (JSON Schema).
2. `pass_when` conditions against JSON artifacts (e.g. a reviewer's `verdict`).
   Any false condition fails the stage.
3. A `human_approval` gate, last, so people only review work that passed the
   automated checks. For `builtin.merge` the human gate is evaluated *before* the
   body, so nothing is delivered without approval.
4. On failure, `on_fail` decides: `retry: N` re-runs the stage with the failure as
   feedback. `then: <later stage>` continues there once retries are spent.
   `goto: <earlier stage>` loops back, at most `max_loops` times. With no
   `on_fail`, the run fails.

`when` conditions skip a stage entirely when false.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_ID = re.compile(r"^[a-z][a-z0-9_-]*$")

StageType = Literal["agent", "parallel", "builtin.merge", "builtin.lint", "builtin.shell"]
GateType = Literal["none", "human_approval", "check", "artifact_valid"]
BUILTIN_TYPES: frozenset[str] = frozenset({"builtin.merge", "builtin.lint", "builtin.shell"})
DEFAULT_LINT_COMMAND = "ruff check ."


class PipelineError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ArtifactSchema(_Strict):
    artifact: str
    schema_path: str = Field(alias="schema")  # JSON Schema file, relative to the factory root


class GateSpec(_Strict):
    type: GateType = "none"
    # check
    command: str | None = None
    must_pass: bool = True
    report: str | None = None  # artifact for the check result (default <stage>_check.json)
    timeout_s: int | None = None
    # artifact_valid: either `artifact` + `schema`, or a list under `artifacts`
    artifact: str | None = None
    schema_path: str | None = Field(default=None, alias="schema")
    artifacts: list[ArtifactSchema] = Field(default_factory=list)
    # human_approval: evaluate before the stage body (default: only for builtin.merge)
    before: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        return {"type": data} if isinstance(data, str) else data

    @model_validator(mode="after")
    def _required_fields(self) -> GateSpec:
        if self.type == "check" and not self.command:
            raise ValueError("check gate requires `command`")
        if self.type == "artifact_valid" and not self.schemas:
            raise ValueError("artifact_valid gate requires `artifact` + `schema` or `artifacts`")
        if bool(self.artifact) != bool(self.schema_path):
            raise ValueError("`artifact` and `schema` must be given together")
        if self.before and self.type != "human_approval":
            raise ValueError("`before: true` is only valid for human_approval gates")
        return self

    @property
    def schemas(self) -> list[ArtifactSchema]:
        single = (
            [ArtifactSchema(artifact=self.artifact, schema_path=self.schema_path)]
            if self.artifact and self.schema_path
            else []
        )
        return single + list(self.artifacts)


class Condition(_Strict):
    """`{exists: plan.md}` or `{artifact: review.json, path: verdict, equals: pass}`.

    `path` is a dotted path into the artifact's JSON (`findings.0.severity`); an empty
    path means the whole document. Exactly one of `equals`, `not_equals`, `in` applies.
    """

    exists: str | None = None
    artifact: str | None = None
    path: str = ""
    equals: Any = None
    not_equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")

    @model_validator(mode="after")
    def _shape(self) -> Condition:
        if bool(self.exists) == bool(self.artifact):
            raise ValueError("a condition needs exactly one of `exists` or `artifact`")
        if self.artifact:
            ops = {"equals", "not_equals", "in_"} & self.model_fields_set
            if len(ops) != 1:
                raise ValueError("an artifact condition needs exactly one of equals/not_equals/in")
        return self

    def describe(self) -> str:
        if self.exists:
            return f"{self.exists} exists"
        where = f"{self.artifact}{':' + self.path if self.path else ''}"
        if "equals" in self.model_fields_set:
            return f"{where} == {self.equals!r}"
        if "not_equals" in self.model_fields_set:
            return f"{where} != {self.not_equals!r}"
        return f"{where} in {self.in_!r}"


class OnFail(_Strict):
    retry: int = Field(default=0, ge=0, le=20)
    then: str | None = None  # later stage to continue at once retries are exhausted
    goto: str | None = None  # earlier (or same) stage to loop back to
    max_loops: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def _exclusive(self) -> OnFail:
        if self.then and self.goto:
            raise ValueError("on_fail may set `then` or `goto`, not both")
        return self


class Budget(_Strict):
    tokens: int | None = Field(default=None, gt=0)
    cost_usd: float | None = Field(default=None, gt=0)


class Branch(_Strict):
    agent: str
    model: str | None = None


class StageSpec(_Strict):
    id: str
    type: StageType = "agent"
    agent: str | None = None
    parallel: list[Branch] | None = None
    model: str | None = None  # replaces the agent.md default model for this stage
    command: str | None = None  # builtin.shell / builtin.lint
    when: list[Condition] = Field(default_factory=list)
    pass_when: list[Condition] = Field(default_factory=list)
    gate: GateSpec = Field(default_factory=GateSpec)
    on_fail: OnFail | None = None
    budget: Budget | None = None

    @model_validator(mode="before")
    @classmethod
    def _infer_type(cls, data: Any) -> Any:
        if isinstance(data, dict) and "type" not in data:
            data = {**data, "type": "parallel" if "parallel" in data else "agent"}
        if isinstance(data, dict):
            for key in ("when", "pass_when"):
                if isinstance(data.get(key), dict):  # allow a single condition
                    data = {**data, key: [data[key]]}
        return data

    @model_validator(mode="after")
    def _shape(self) -> StageSpec:
        if not _ID.match(self.id):
            raise ValueError(f"stage id {self.id!r} must be lowercase letters, digits, '-' or '_'")
        if self.type == "agent" and not self.agent:
            raise ValueError(f"stage {self.id!r}: agent stage requires `agent`")
        if self.type == "parallel" and not self.parallel:
            raise ValueError(
                f"stage {self.id!r}: parallel stage requires a non-empty `parallel` list"
            )
        if self.type != "agent" and self.agent:
            raise ValueError(f"stage {self.id!r}: `agent` is only valid on agent stages")
        if self.type == "builtin.shell" and not self.command:
            raise ValueError(f"stage {self.id!r}: builtin.shell requires `command`")
        if self.command and self.type not in ("builtin.shell", "builtin.lint"):
            raise ValueError(f"stage {self.id!r}: `command` is only valid on shell/lint stages")
        if self.parallel:
            names = [b.agent for b in self.parallel]
            if len(set(names)) != len(names):
                raise ValueError(f"stage {self.id!r}: an agent may appear only once in `parallel`")
        return self

    @property
    def agents(self) -> list[str]:
        if self.agent:
            return [self.agent]
        return [b.agent for b in self.parallel or ()]

    @property
    def branches(self) -> list[Branch]:
        if self.agent:
            return [Branch(agent=self.agent, model=self.model)]
        return [Branch(agent=b.agent, model=b.model or self.model) for b in self.parallel or ()]

    @property
    def is_builtin(self) -> bool:
        return self.type in BUILTIN_TYPES

    @property
    def approval_before(self) -> bool:
        if self.gate.type != "human_approval":
            return False
        return self.gate.before if self.gate.before is not None else self.type == "builtin.merge"

    @property
    def shell_command(self) -> str | None:
        if self.type == "builtin.lint":
            return self.command or DEFAULT_LINT_COMMAND
        return self.command if self.type == "builtin.shell" else None


class PipelineSpec(_Strict):
    name: str
    description: str = ""
    budget: Budget | None = None  # per-run limits (override config budgets)
    stages: list[StageSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _references(self) -> PipelineSpec:
        ids = [s.id for s in self.stages]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate stage id(s): {', '.join(dupes)}")
        for index, stage in enumerate(self.stages):
            if not stage.on_fail:
                continue
            then, goto = stage.on_fail.then, stage.on_fail.goto
            if then and then not in ids:
                raise ValueError(f"stage {stage.id!r}: on_fail.then target {then!r} not found")
            if goto and goto not in ids:
                raise ValueError(f"stage {stage.id!r}: on_fail.goto target {goto!r} not found")
            # `then` only moves forward and `goto` only backward, so every loop is bounded.
            if then and ids.index(then) <= index:
                raise ValueError(f"stage {stage.id!r}: on_fail.then must point to a later stage")
            if goto and ids.index(goto) > index:
                raise ValueError(
                    f"stage {stage.id!r}: on_fail.goto must point to this or an earlier stage"
                )
        return self

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.stages]

    def stage(self, stage_id: str) -> StageSpec:
        for s in self.stages:
            if s.id == stage_id:
                return s
        raise KeyError(stage_id)

    def next_id(self, stage_id: str) -> str | None:
        ids = self.ids
        i = ids.index(stage_id)
        return ids[i + 1] if i + 1 < len(ids) else None

    def between(self, first: str, last: str) -> list[str]:
        """Stage ids from `first` to `last`, inclusive."""
        ids = self.ids
        return ids[ids.index(first) : ids.index(last) + 1]

    def referenced_agents(self) -> set[str]:
        return {a for s in self.stages for a in s.agents}

    def schema_files(self) -> set[str]:
        return {s.schema_path for st in self.stages for s in st.gate.schemas}


def parse_pipeline(text: str, source: str = "<pipeline>") -> PipelineSpec:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PipelineError(f"{source}: YAML syntax error: {exc}") from exc
    if not isinstance(raw, dict):
        raise PipelineError(f"{source}: top level must be a mapping")
    try:
        return PipelineSpec.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
        )
        raise PipelineError(f"{source}: {details}") from exc


def load_pipelines(directory: Path) -> tuple[dict[str, PipelineSpec], dict[str, str]]:
    """All pipelines in `directory`, plus errors keyed by file name."""
    pipelines: dict[str, PipelineSpec] = {}
    errors: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
        try:
            spec = parse_pipeline(path.read_text(encoding="utf-8"), path.name)
        except (PipelineError, OSError) as exc:
            errors[path.name] = str(exc)
            continue
        if spec.name != path.stem:
            errors[path.name] = f"{path.name}: name {spec.name!r} must match the file name"
            continue
        pipelines[spec.name] = spec
    return pipelines, errors
