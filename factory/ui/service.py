"""Everything the web UI needs, independent of HTTP: run threads and JSON views.

Runs execute in background threads (one per run). The orchestrator persists every
transition, so the UI only has to read the store and the event log. Live updates
arrive through the Broadcaster (SSE).
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from factory import __version__
from factory.config.loader import ConfigError, LiveConfig
from factory.core.artifacts import ArtifactError
from factory.core.orchestrator import Orchestrator
from factory.core.pipeline import PipelineError, PipelineSpec, StageSpec
from factory.core.store import ApprovalRecord, RunRecord
from factory.core.watcher import FileWatcher
from factory.isolation.sandboxes import SandboxError, select_backend
from factory.isolation.worktrees import GitError, RunRepo
from factory.ui.broadcast import Broadcaster

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class NotFound(LookupError):
    pass


class BadRequest(ValueError):
    pass


class RunManager:
    """One background thread per active run."""

    def __init__(self, orch: Orchestrator) -> None:
        self.orch = orch
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def start(self, run_id: str, *, resume: bool = False) -> bool:
        """Start (or continue) a run in the background. False if it is already active."""
        with self._lock:
            current = self._threads.get(run_id)
            if current is not None and current.is_alive():
                return False
            thread = threading.Thread(
                target=self._work, args=(run_id, resume), name=f"run-{run_id}", daemon=True
            )
            self._threads[run_id] = thread
            thread.start()
            return True

    def _work(self, run_id: str, resume: bool) -> None:
        try:
            if resume:
                self.orch.resume(run_id)
            else:
                self.orch.run(run_id)
        except Exception as exc:  # never let a run thread die silently
            message = f"internal error: {type(exc).__name__}: {exc}"
            self.orch.store.update_run(run_id, status="failed", error=message)
            self.orch.bus.emit(
                "run.status", run_id=run_id, data={"status": "failed", "error": message}
            )

    def active(self, run_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(run_id)
            return thread is not None and thread.is_alive()

    def active_runs(self) -> list[str]:
        with self._lock:
            return [rid for rid, t in self._threads.items() if t.is_alive()]

    def join(self, run_id: str, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._threads.get(run_id)
        if thread is not None:
            thread.join(timeout)

    def resume_interrupted(self) -> list[str]:
        """Continue runs a previous process left `running` (crash or Ctrl+C)."""
        resumed = [r.id for r in self.orch.store.list_runs(200) if r.status == "running"]
        for run_id in resumed:
            self.start(run_id)
        return resumed


class FactoryService:
    def __init__(self, live: LiveConfig, orch: Orchestrator | None = None) -> None:
        self.live = live
        self.orch = orch or Orchestrator(live)
        self.broadcaster = Broadcaster()
        self.orch.bus.subscribe(self.broadcaster.publish)
        self.runs = RunManager(self.orch)
        self._config_error: str | None = None
        live.subscribe(self._on_config)
        settings = live.settings
        self.watcher = FileWatcher(
            [
                (live.root / "config", "*.yaml"),
                (settings.path("pipelines"), "*.yaml"),
                (settings.path("agents"), "*/agent.md"),
                (settings.path("agents"), "*/*.json"),  # gate schemas
            ],
            self.on_files_changed,
        )

    def on_files_changed(self, changed: list[Path]) -> None:
        """Hot reload: apply edits and tell connected browsers (via the event bus)."""
        settings = self.live.settings
        config_dir = (self.live.root / "config").resolve()
        pipelines_dir = settings.path("pipelines").resolve()
        agents_dir = settings.path("agents").resolve()
        parents = {p.resolve().parent for p in changed}
        files = [p.resolve() for p in changed]
        if config_dir in parents:
            self.live.reload()  # emits config.reloaded / config.error via _on_config
        if pipelines_dir in parents or any(f.is_relative_to(agents_dir) for f in files):
            # Schemas live under agents/, and pipeline validity depends on agents.
            pipelines, errors = self.orch.pipelines()
            self.orch.bus.emit(
                "pipelines.reloaded",
                data={"pipelines": sorted(pipelines), "errors": errors},
            )
        if any(f.is_relative_to(agents_dir) for f in files):
            self.orch.agents.reload()
            self.orch.bus.emit(
                "agents.reloaded",
                data={
                    "agents": sorted(self.orch.agents.agents),
                    "errors": {e.name: e.message for e in self.orch.agents.errors},
                    "changed": [
                        str(f.relative_to(agents_dir))
                        for f in files
                        if f.is_relative_to(agents_dir)
                    ],
                },
            )

    def _on_config(self, _settings: Any, error: ConfigError | None) -> None:
        message = str(error) if error else None
        if error is None:
            self._config_error = None
            self.orch.bus.emit("config.reloaded", data={"error": None})
        elif message != self._config_error:  # report each distinct error once
            self._config_error = message
            self.orch.bus.emit("config.error", data={"error": message})

    # ------------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        self.live.refresh()  # picks up YAML edits even without the M6 watcher
        self.orch.agents.reload()
        _, pipeline_errors = self.orch.pipelines()
        try:
            selection = select_backend(self.live.settings.factory.sandbox)
            sandbox = {
                "backend": selection.backend.name,
                "reduced": selection.reduced_isolation,
                "message": selection.describe(),
            }
        except SandboxError as exc:
            sandbox = {"backend": None, "reduced": True, "message": str(exc)}
        error = self.live.error
        return {
            "version": __version__,
            "config_error": str(error) if error else None,
            "agent_errors": [
                {"name": e.name, "path": str(e.path), "message": e.message}
                for e in self.orch.agents.errors
            ],
            "pipeline_errors": [
                {"file": f, "message": m} for f, m in sorted(pipeline_errors.items())
            ],
            "sandbox": sandbox,
            "active_runs": self.runs.active_runs(),
            "pending_approvals": len(self.orch.pending_approvals()),
        }

    # ------------------------------------------------------------------- models

    def models(self) -> dict[str, Any]:
        self.live.refresh()
        settings = self.live.settings
        resolver = self.orch.resolver
        self.orch.agents.reload()
        agents = []
        for name, spec in sorted(self.orch.agents.agents.items()):
            resolved = resolver.resolve(agent=name, agent_default=spec.model)
            agents.append(
                {
                    "name": name,
                    "description": spec.description,
                    "isolation_level": spec.isolation_level,
                    "role": spec.role,
                    "agent_default": spec.model,
                    "override": settings.agent_overrides.get(name),
                    "effective": resolved.alias,
                    "source": resolved.source,
                }
            )
        return {
            "aliases": [
                {
                    "alias": alias,
                    "provider": m.provider,
                    "provider_type": settings.models.providers[m.provider].type,
                    "model": m.model,
                    "description": m.description,
                    "supports_tools": m.supports_tools,
                    "context_window": m.context_window,
                }
                for alias, m in settings.models.models.items()
            ],
            "default": settings.default_model,
            "default_source": "runtime" if settings.overrides.default_model else "factory.yaml",
            "config_default": settings.factory.default_model,
            "agents": agents,
        }

    def set_default_model(self, alias: str | None) -> dict[str, Any]:
        try:
            if alias:
                self.live.set_model(alias)
            else:
                self.live.clear_model()
        except ConfigError as exc:
            raise BadRequest(str(exc)) from exc
        self.orch.bus.emit("model.switched", data={"agent": None, "alias": alias})
        return self.models()

    def set_agent_model(self, agent: str, alias: str | None) -> dict[str, Any]:
        if agent not in self.orch.agents.agents:
            raise NotFound(f"agent {agent!r} not found")
        try:
            if alias:
                self.live.set_model(alias, agent=agent)
            else:
                self.live.clear_model(agent)
        except ConfigError as exc:
            raise BadRequest(str(exc)) from exc
        self.orch.bus.emit("model.switched", data={"agent": agent, "alias": alias})
        return self.models()

    # ---------------------------------------------------------------- pipelines

    def pipelines(self) -> list[dict[str, Any]]:
        pipelines, errors = self.orch.pipelines()
        out = []
        for name, spec in sorted(pipelines.items()):
            try:
                self.orch.check_runnable(spec)
                problem = None
            except PipelineError as exc:
                problem = str(exc)
            out.append(
                {
                    "name": name,
                    "description": spec.description,
                    "stages": [_stage_shape(s) for s in spec.stages],
                    "error": problem,
                }
            )
        out += [
            {"name": f, "description": "", "stages": [], "error": m}
            for f, m in sorted(errors.items())
        ]
        return out

    # --------------------------------------------------------------------- runs

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for run in self.orch.store.list_runs(limit):
            total = self.orch.store.usage_total(run.id)
            out.append(
                {
                    **_run_fields(run),
                    "active": self.runs.active(run.id),
                    "tokens": total.total_tokens,
                    "cost_usd": total.cost_usd,
                }
            )
        return out

    def create_run(
        self, request: str, pipeline: str, repo: str | None, model: str | None
    ) -> dict[str, Any]:
        try:
            run = self.orch.create_run(
                request, pipeline=pipeline, repo=repo or None, model_override=model or None
            )
        except (PipelineError, ConfigError) as exc:
            raise BadRequest(str(exc)) from exc
        self.runs.start(run.id)
        return self.run_detail(run.id)

    def run_detail(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        try:
            spec: PipelineSpec | None = self.orch.run_pipeline(run_id)
        except (OSError, PipelineError):
            spec = None
        records = {s.stage_id: s for s in self.orch.store.stages(run_id)}
        state = run.state_dict
        events = self.orch.event_log.read(run_id)
        stages = []
        for stage in spec.stages if spec else []:
            rec = records.get(stage.id)
            stage_usage = self.orch.store.usage_total(run_id, stage.id)
            stages.append(
                {
                    **_stage_shape(stage),
                    "status": rec.status if rec else "pending",
                    "attempts": rec.attempts if rec else 0,
                    "error": rec.error if rec else None,
                    "started_at": rec.started_at if rec else None,
                    "finished_at": rec.finished_at if rec else None,
                    "current": run.current_stage == stage.id,
                    "loops": state.get("loops", {}).get(stage.id, 0),
                    "retries": state.get("retries", {}).get(stage.id, 0),
                    "tokens": stage_usage.total_tokens,
                    "cost_usd": stage_usage.cost_usd,
                }
            )
        usage = [
            {
                "agent": u.agent,
                "calls": u.calls,
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cost_usd": u.cost_usd,
            }
            for u in self.orch.store.usage_by_agent(run_id)
        ]
        total = self.orch.store.usage_total(run_id)
        isolation = next(
            (e["data"] for e in reversed(events) if e["type"] == "isolation.status"), None
        )
        delivery = None
        store = self.orch.artifacts(run_id)
        if store.exists("delivery.json"):
            try:
                delivery = json.loads(store.read("delivery.json"))
            except ValueError:
                delivery = None
        return {
            **_run_fields(run),
            "active": self.runs.active(run_id),
            "stages": stages,
            "usage": usage,
            "total": {
                "calls": total.calls,
                "input_tokens": total.input_tokens,
                "output_tokens": total.output_tokens,
                "tokens": total.total_tokens,
                "cost_usd": total.cost_usd,
            },
            "approvals": [_approval(a) for a in self.orch.store.approvals(run_id)],
            "artifacts": self._artifact_index(run_id, events),
            "isolation": isolation,
            "delivery": delivery,
            "network": state.get("network"),
        }

    def events(self, run_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        self._run(run_id)
        return self.orch.event_log.read(run_id)[-limit:]

    def artifact(self, run_id: str, name: str) -> str:
        self._run(run_id)
        try:
            content = self.orch.artifacts(run_id).read_optional(name)
        except ArtifactError as exc:
            raise BadRequest(str(exc)) from exc
        if content is None:
            raise NotFound(f"artifact {name!r} not found")
        return content

    def decide(
        self, run_id: str, decision: str, comment: str | None, approval_id: int | None
    ) -> dict[str, Any]:
        self._run(run_id)
        if decision not in ("approved", "rejected", "changes_requested"):
            raise BadRequest(f"invalid decision {decision!r}")
        if decision == "changes_requested" and not (comment or "").strip():
            raise BadRequest("a comment is required when requesting changes")
        try:
            approval = self.orch.decide(run_id, decision, comment or None, approval_id=approval_id)
        except PipelineError as exc:
            raise BadRequest(str(exc)) from exc
        if approval.kind != "network":  # a network request is picked up by the waiting agent
            self.runs.start(run_id)
        return {"approval": _approval(approval), "run": self.run_detail(run_id)}

    def resume(self, run_id: str) -> dict[str, Any]:
        self._run(run_id)
        if not self.runs.start(run_id, resume=True):
            raise BadRequest("run is already active")
        return self.run_detail(run_id)

    def set_run_model(self, run_id: str, alias: str | None) -> dict[str, Any]:
        self._run(run_id)
        try:
            self.orch.set_run_model(run_id, alias or None)
        except ConfigError as exc:
            raise BadRequest(str(exc)) from exc
        return self.run_detail(run_id)

    # -------------------------------------------------------------------- changes

    def changes(self, run_id: str) -> dict[str, Any]:
        repo = self._repo(run_id)
        if repo is None:
            return {"available": False, "refs": [], "stages": []}
        try:
            refs = [{"ref": "integration", "label": "integration (all approved changes)"}]
            refs += [{"ref": f"agent:{a}", "label": f"{a} branch"} for a in repo.branches()]
            spec = self.orch.run_pipeline(run_id)
            stages = []
            for stage in spec.stages:
                commits = repo.stage_commits(stage.id)
                if commits:
                    stages.append({"stage": stage.id, "commits": commits})
            files = repo.files_on_integration()
        except (GitError, OSError, PipelineError) as exc:
            raise BadRequest(str(exc)) from exc
        return {"available": True, "refs": refs, "stages": stages, "files": files}

    def diff(self, run_id: str, ref: str) -> str:
        repo = self._repo(run_id)
        if repo is None:
            raise NotFound("this run has no workspace yet")
        try:
            if ref == "integration":
                return repo.diff_from_base(repo.integration) or "(no changes)"
            kind, _, value = ref.partition(":")
            if kind == "agent" and _NAME.match(value):
                return repo.diff_from_base(repo.branch(value)) or "(no changes)"
            if kind == "stage" and _NAME.match(value):
                commits = repo.stage_commits(value)
                return (
                    "\n".join(repo.show(c["sha"]) for c in commits) or "(no commits for this stage)"
                )
            if kind == "commit" and _SHA.match(value):
                return repo.show(value)
        except GitError as exc:
            raise BadRequest(str(exc)) from exc
        raise BadRequest(f"invalid ref {ref!r}")

    # -------------------------------------------------------------------- helpers

    def _run(self, run_id: str) -> RunRecord:
        run = self.orch.store.get_run(run_id)
        if run is None:
            raise NotFound(f"run {run_id!r} not found")
        return run

    def _repo(self, run_id: str) -> RunRepo | None:
        self._run(run_id)
        repo = RunRepo(self.live.settings.path("workspaces"), run_id)
        return repo if repo.exists else None

    def _artifact_index(self, run_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        origin: dict[str, dict[str, Any]] = {"request.md": {"stage": None, "agent": "user"}}
        for e in events:
            data = e.get("data") or {}
            if e["type"] == "artifact.written":
                origin[data["name"]] = {"stage": e.get("stage"), "agent": e.get("agent")}
            elif e["type"] == "gate.evaluated" and data.get("report"):
                origin[data["report"]] = {"stage": e.get("stage"), "agent": "gate"}
            elif e["type"] == "run.delivered":
                origin["delivery.json"] = {"stage": e.get("stage"), "agent": "factory"}
        store = self.orch.artifacts(run_id)
        out = []
        for name in store.list():
            path: Path = store.path(name)
            out.append(
                {
                    "name": name,
                    "size": path.stat().st_size,
                    **origin.get(name, {"stage": None, "agent": None}),
                }
            )
        return out


def _run_fields(run: RunRecord) -> dict[str, Any]:
    return {
        "id": run.id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "request": run.request,
        "pipeline": run.pipeline,
        "repo": run.repo,
        "status": run.status,
        "model_override": run.model_override,
        "current_stage": run.current_stage,
        "error": run.error,
    }


def _stage_shape(stage: StageSpec) -> dict[str, Any]:
    return {
        "id": stage.id,
        "type": stage.type,
        "agents": stage.agents,
        "gate": stage.gate.type,
        "gate_before": stage.approval_before,
        "on_fail": stage.on_fail.model_dump(exclude_defaults=True) if stage.on_fail else None,
        "when": [c.describe() for c in stage.when],
        "pass_when": [c.describe() for c in stage.pass_when],
    }


def _approval(a: ApprovalRecord) -> dict[str, Any]:
    return {
        "id": a.id,
        "stage": a.stage_id,
        "kind": a.kind,
        "status": a.status,
        "summary": a.summary,
        "details": a.details_dict,
        "comment": a.comment,
        "created_at": a.created_at,
        "decided_at": a.decided_at,
    }
