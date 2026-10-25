"""Orchestrator: interprets a pipeline for one run, stage by stage, persisting state.

Milestone 2 scope: sequential `agent` stages with no gates. Pipelines that use
parallel stages, builtin stages, gates or on_fail are rejected up front with a clear
message until milestone 4 adds them. A run can be resumed at any time: completed
stages are skipped, and a stage interrupted mid-flight is re-run.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from factory.agents.context import InvocationContext
from factory.agents.loader import AgentRegistry
from factory.agents.runtime import AgentRuntime
from factory.config.loader import ConfigError, LiveConfig
from factory.core.artifacts import ArtifactStore
from factory.core.events import Event, EventBus, JsonlEventLog
from factory.core.pipeline import (
    PipelineError,
    PipelineSpec,
    StageSpec,
    load_pipelines,
    parse_pipeline,
)
from factory.core.store import TERMINAL_RUN, RunRecord, RunStore, StageStatus
from factory.providers.resolver import ModelResolver
from factory.tools.registry import ToolRegistry

PIPELINE_SNAPSHOT = "pipeline.yaml"


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def unsupported_features(spec: PipelineSpec) -> list[str]:
    """Features the milestone-2 orchestrator cannot execute yet."""
    problems = []
    for stage in spec.stages:
        if stage.type != "agent":
            problems.append(f"stage {stage.id!r}: stage type {stage.type!r}")
        if stage.gate.type != "none":
            problems.append(f"stage {stage.id!r}: gate {stage.gate.type!r}")
        if stage.on_fail is not None:
            problems.append(f"stage {stage.id!r}: on_fail")
    return problems


class Orchestrator:
    def __init__(
        self,
        live: LiveConfig,
        *,
        bus: EventBus | None = None,
        resolver: ModelResolver | None = None,
    ) -> None:
        self.live = live
        settings = live.settings
        self.runs_dir = settings.path("runs")
        self.bus = bus or EventBus()
        self.event_log = JsonlEventLog(self.runs_dir)
        self.bus.subscribe(self.event_log)
        self.store = RunStore(self.runs_dir / "factory.db")
        self.bus.subscribe(self._record_usage)
        self.agents = AgentRegistry(settings.path("agents"))
        self.tools = ToolRegistry()
        self.resolver = resolver or ModelResolver(live)
        self.runtime = AgentRuntime(self.resolver, self.tools, self.bus, self.runs_dir)

    # ------------------------------------------------------------------------- queries

    def pipelines(self) -> tuple[dict[str, PipelineSpec], dict[str, str]]:
        return load_pipelines(self.live.settings.path("pipelines"))

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def artifacts(self, run_id: str) -> ArtifactStore:
        return ArtifactStore(self.run_dir(run_id) / "artifacts")

    def run_pipeline(self, run_id: str) -> PipelineSpec:
        """The pipeline snapshot taken when the run was created."""
        path = self.run_dir(run_id) / PIPELINE_SNAPSHOT
        return parse_pipeline(path.read_text(encoding="utf-8"), f"{run_id}/{PIPELINE_SNAPSHOT}")

    def check_runnable(self, spec: PipelineSpec) -> None:
        problems = [f"{p} is not supported until milestone 4" for p in unsupported_features(spec)]
        self.agents.reload()
        for name in sorted(spec.referenced_agents()):
            if name in self.agents.agents:
                continue
            err = self.agents.error_for(name)
            problems.append(
                f"agent {name!r} is invalid: {err.message}" if err else f"agent {name!r} not found"
            )
        if problems:
            raise PipelineError(
                f"pipeline {spec.name!r} cannot run:\n  - " + "\n  - ".join(problems)
            )

    # ------------------------------------------------------------------------ commands

    def create_run(
        self,
        request: str,
        *,
        pipeline: str = "default",
        repo: str | None = None,
        model_override: str | None = None,
    ) -> RunRecord:
        if not request.strip():
            raise PipelineError("request text is empty")
        if model_override and model_override not in self.live.settings.models.models:
            raise ConfigError(f"unknown model alias {model_override!r}")
        pipelines, errors = self.pipelines()
        spec = pipelines.get(pipeline)
        if spec is None:
            detail = errors.get(f"{pipeline}.yaml")
            raise PipelineError(
                detail
                or f"pipeline {pipeline!r} not found; available: "
                f"{', '.join(sorted(pipelines)) or '(none)'}"
            )
        self.check_runnable(spec)

        run_id = new_run_id()
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True)
        source = self.live.settings.path("pipelines") / f"{pipeline}.yaml"
        (run_dir / PIPELINE_SNAPSHOT).write_text(source.read_text("utf-8"), encoding="utf-8")
        self.artifacts(run_id).write("request.md", request.strip() + "\n")
        run = self.store.create_run(
            run_id,
            request=request.strip(),
            pipeline=pipeline,
            stage_ids=[s.id for s in spec.stages],
            repo=repo,
            model_override=model_override,
        )
        self.bus.emit(
            "run.created",
            run_id=run_id,
            data={"pipeline": pipeline, "stages": [s.id for s in spec.stages], "repo": repo},
        )
        return run

    def run(self, run_id: str) -> RunRecord:
        """Execute (or resume) a run until it finishes or fails."""
        run = self._get(run_id)
        if run.status in TERMINAL_RUN:
            return run
        spec = self.run_pipeline(run_id)
        interrupted = self.store.reset_interrupted(run_id)
        self._set_run(run_id, "running", resumed_stages=interrupted)
        for stage in spec.stages:
            record = self.store.stage(run_id, stage.id)
            if record is None or record.status in ("passed", "skipped"):
                continue
            error = self._run_stage(run, stage)
            if error is not None:
                self._set_run(run_id, "failed", error=f"stage {stage.id!r}: {error}")
                return self._get(run_id)
        self.store.update_run(run_id, current_stage=None)
        self._set_run(run_id, "passed")
        return self._get(run_id)

    def resume(self, run_id: str) -> RunRecord:
        """Continue a run. Failed stages get another attempt; passed stages are kept."""
        run = self._get(run_id)
        if run.status == "failed":
            for st in self.store.stages(run_id):
                if st.status == "failed":
                    self._set_stage(run_id, st.stage_id, "pending")
            self.store.update_run(run_id, status="pending", error=None)
        return self.run(run_id)

    # ------------------------------------------------------------------------ internals

    def _run_stage(self, run: RunRecord, stage: StageSpec) -> str | None:
        """Run one stage. Returns None on success, else the error message."""
        assert stage.agent is not None
        self._set_stage(run.id, stage.id, "running")
        self.store.update_run(run.id, current_stage=stage.id)
        agent = self.agents.get(stage.agent)  # re-reads agent.md: edits apply next invocation
        if agent is None:
            err = self.agents.error_for(stage.agent)
            missing = f"agent {stage.agent!r} " + (
                f"is invalid: {err.message}" if err else "not found"
            )
            self._set_stage(run.id, stage.id, "failed", missing)
            return missing
        attempt = (rec.attempts if (rec := self.store.stage(run.id, stage.id)) else 1) or 1
        ctx = InvocationContext(
            run_id=run.id,
            stage_id=stage.id,
            request=run.request,
            artifacts=self.artifacts(run.id),
            model_override=run.model_override,
            model_default=stage.model,
            attempt=attempt,
        )
        try:
            result = self.runtime.invoke(agent, ctx)
            error = None if result.status == "passed" else result.error or "agent failed"
        except Exception as exc:
            error = f"internal error: {type(exc).__name__}: {exc}"
        self._set_stage(run.id, stage.id, "failed" if error else "passed", error)
        return error

    def _get(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"run {run_id!r} not found")
        return run

    def _set_run(
        self, run_id: str, status: str, *, error: str | None = None, **data: object
    ) -> None:
        self.store.update_run(run_id, status=status, error=error)
        self.bus.emit("run.status", run_id=run_id, data={"status": status, "error": error, **data})

    def _set_stage(
        self, run_id: str, stage_id: str, status: StageStatus, error: str | None = None
    ) -> None:
        previous = self.store.stage(run_id, stage_id)
        self.store.set_stage(run_id, stage_id, status, error)
        self.bus.emit(
            "stage.transition",
            run_id=run_id,
            stage=stage_id,
            data={"from": previous.status if previous else None, "to": status, "error": error},
        )

    def _record_usage(self, event: Event) -> None:
        if event.type != "llm.call" or not event.run_id:
            return
        d = event.data
        self.store.record_usage(
            event.run_id,
            stage_id=event.stage,
            agent=event.agent,
            model_alias=str(d["alias"]),
            model_id=str(d["model"]),
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            cost_usd=float(d["cost_usd"]),
            latency_ms=float(d["latency_ms"]),
        )
