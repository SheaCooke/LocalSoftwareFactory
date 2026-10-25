"""Pipeline definitions (config/pipelines/*.yaml): data the orchestrator interprets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_ID = re.compile(r"^[a-z][a-z0-9_-]*$")

StageType = Literal["agent", "parallel", "builtin.merge", "builtin.lint", "builtin.shell"]
GateType = Literal["none", "human_approval", "check", "artifact_valid"]


class PipelineError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class GateSpec(_Strict):
    type: GateType = "none"
    command: str | None = None  # check
    must_pass: bool = True  # check
    artifact: str | None = None  # artifact_valid
    schema_path: str | None = Field(default=None, alias="schema")  # artifact_valid

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        return {"type": data} if isinstance(data, str) else data

    @model_validator(mode="after")
    def _required_fields(self) -> GateSpec:
        if self.type == "check" and not self.command:
            raise ValueError("check gate requires `command`")
        if self.type == "artifact_valid" and not (self.artifact and self.schema_path):
            raise ValueError("artifact_valid gate requires `artifact` and `schema`")
        return self


class OnFail(_Strict):
    retry: int = Field(default=0, ge=0, le=20)
    then: str | None = None  # stage id to continue at once retries are exhausted
    goto: str | None = None  # stage id to loop back to
    max_loops: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def _exclusive(self) -> OnFail:
        if self.then and self.goto:
            raise ValueError("on_fail may set `then` or `goto`, not both")
        return self


class Branch(_Strict):
    agent: str
    model: str | None = None


class StageSpec(_Strict):
    id: str
    type: StageType = "agent"
    agent: str | None = None
    parallel: list[Branch] | None = None
    model: str | None = None  # model alias for this stage (beats agent overrides)
    command: str | None = None  # builtin.shell / builtin.lint
    gate: GateSpec = Field(default_factory=GateSpec)
    on_fail: OnFail | None = None

    @model_validator(mode="before")
    @classmethod
    def _infer_type(cls, data: Any) -> Any:
        if isinstance(data, dict) and "type" not in data:
            data = {**data, "type": "parallel" if "parallel" in data else "agent"}
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
        return self

    @property
    def agents(self) -> list[str]:
        if self.agent:
            return [self.agent]
        return [b.agent for b in self.parallel or ()]


class PipelineSpec(_Strict):
    name: str
    description: str = ""
    stages: list[StageSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _references(self) -> PipelineSpec:
        ids = [s.id for s in self.stages]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate stage id(s): {', '.join(dupes)}")
        for stage in self.stages:
            if stage.on_fail:
                for target in (stage.on_fail.then, stage.on_fail.goto):
                    if target and target not in ids:
                        raise ValueError(f"stage {stage.id!r}: on_fail target {target!r} not found")
        return self

    def stage(self, stage_id: str) -> StageSpec:
        for s in self.stages:
            if s.id == stage_id:
                return s
        raise KeyError(stage_id)

    def referenced_agents(self) -> set[str]:
        return {a for s in self.stages for a in s.agents}


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
