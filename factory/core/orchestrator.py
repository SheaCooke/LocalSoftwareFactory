"""Orchestrator: interprets a pipeline for one run, stage by stage, persisting state.

The interpreter walks the pipeline with a persisted cursor (runs.state). For each
stage: `when` may skip it; the body runs (agent, parallel agents, or a builtin);
`pass_when` conditions and the gate decide pass/fail; `on_fail` retries, jumps
forward (`then`) or loops back (`goto`, bounded by `max_loops`). Human gates,
budget overruns and L3 network requests create approvals and pause the run in
`waiting_approval`. `decide()` records the human's answer, and the next `run()`
continues from there.

Every transition is committed to SQLite before the next step, so a crash or
restart resumes from the last completed stage: an interrupted stage simply runs
again.

Isolation per stage: L0 agents get no workspace. L1+ agents get their own git
worktree of the run repo (read-only agents a fresh checkout each time), a
PathPolicy built from their fs_scope, and, at L2+, the sandbox backend. A writing
agent's worktree is committed after every invocation and merged into the run's
integration branch when its stage passes (or is carried forward with `then`).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.agents.context import AgentIsolation, InvocationContext
from factory.agents.loader import AgentRegistry
from factory.agents.runtime import AgentResult, AgentRuntime
from factory.agents.spec import READ_ONLY_ROLES, AgentSpec, IsolationLevel
from factory.config.loader import ConfigError, LiveConfig
from factory.core.artifacts import ArtifactStore
from factory.core.conditions import evaluate_all, load_json_artifact
from factory.core.events import Event, EventBus, JsonlEventLog
from factory.core.jsonschema_lite import validate
from factory.core.pipeline import (
    Branch,
    PipelineError,
    PipelineSpec,
    StageSpec,
    load_pipelines,
    parse_pipeline,
)
from factory.core.store import (
    TERMINAL_RUN,
    ApprovalKind,
    ApprovalRecord,
    RunRecord,
    RunStore,
    StageStatus,
)
from factory.isolation.sandboxes import SandboxError, SandboxSelection, select_backend
from factory.isolation.worktrees import GitError, RunRepo, git_available, run_git
from factory.providers.resolver import ModelResolver
from factory.tools.registry import ToolRegistry

PIPELINE_SNAPSHOT = "pipeline.yaml"
BUILTIN_WORKTREE = "_builtin"  # read-only checkout of integration for checks and builtins
_OUTPUT_TAIL = 4000


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


class RunFailed(Exception):
    """Raised inside the interpreter to end the run as failed."""


@dataclass
class Outcome:
    passed: bool
    reason: str | None = None
    approval: ApprovalRecord | None = None  # set when the stage now waits for a human
    skipped: bool = False


@dataclass
class _Branch:
    agent: AgentSpec | None
    result: AgentResult | None = None
    error: str | None = None
    writable: bool = False


@dataclass
class RunState:
    """Interpreter state persisted in runs.state (JSON)."""

    cursor: str | None = None
    loops: dict[str, int] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    feedback: dict[str, str] = field(default_factory=dict)
    preapproved: dict[str, bool] = field(default_factory=dict)
    allowance: dict[str, int] = field(default_factory=dict)  # extra budget multiples
    network: str | None = None  # None | "approved" | "denied"

    @classmethod
    def load(cls, raw: dict[str, Any]) -> RunState:
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def dump(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


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
        self._repos: dict[str, RunRepo] = {}
        self._sandbox: SandboxSelection | None = None
        self._isolation_reported: set[str] = set()
        self._state_lock = threading.Lock()
        self._network_lock = threading.Lock()

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
        problems: list[str] = []
        self.agents.reload()
        for name in sorted(spec.referenced_agents()):
            if name in self.agents.agents:
                continue
            err = self.agents.error_for(name)
            problems.append(
                f"agent {name!r} is invalid: {err.message}" if err else f"agent {name!r} not found"
            )
        for schema in sorted(spec.schema_files()):
            path = self.live.root / schema
            try:
                json.loads(path.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(f"schema {schema!r} cannot be loaded: {exc}")
        for stage in spec.stages:
            for alias in [b.model for b in stage.branches if b.model]:
                if alias not in self.live.settings.models.models:
                    problems.append(f"stage {stage.id!r}: unknown model alias {alias!r}")
        needs_git = any(s.is_builtin or s.gate.type == "check" for s in spec.stages) or any(
            a.level >= IsolationLevel.L1
            for n, a in self.agents.agents.items()
            if n in spec.referenced_agents()
        )
        if needs_git and not git_available():
            problems.append("git is required for agent worktrees but is not on PATH")
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
        if repo is not None:
            repo = str(Path(repo).resolve())
            try:
                run_git(["rev-parse", "--verify", "HEAD"], Path(repo))
            except (GitError, OSError) as exc:
                raise PipelineError(
                    f"--repo {repo} must be a git repository with at least one commit"
                ) from exc

        run_id = new_run_id()
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True)
        source = self.live.settings.path("pipelines") / f"{pipeline}.yaml"
        (run_dir / PIPELINE_SNAPSHOT).write_text(source.read_text("utf-8"), encoding="utf-8")
        self.artifacts(run_id).write("request.md", request.strip() + "\n")
        self.store.create_run(
            run_id,
            request=request.strip(),
            pipeline=pipeline,
            stage_ids=spec.ids,
            repo=repo,
            model_override=model_override,
        )
        self.store.save_state(run_id, RunState(cursor=spec.ids[0]).dump())
        self.bus.emit(
            "run.created",
            run_id=run_id,
            data={"pipeline": pipeline, "stages": spec.ids, "repo": repo},
        )
        return self._get(run_id)

    def run(self, run_id: str) -> RunRecord:
        """Execute (or resume) a run until it finishes, fails, or waits for a human."""
        run = self._get(run_id)
        if run.status in TERMINAL_RUN:
            return run
        spec = self.run_pipeline(run_id)
        state = self._load_state(run, spec)
        if self._pending_gate(run_id) is not None:
            return self._get(run_id)  # still waiting for a decision
        for stale in self.pending_approvals(run_id):
            # A network request whose agent died with the previous process; it re-asks.
            self.store.decide_approval(stale.id, "rejected", "superseded: run restarted")
        interrupted = self.store.reset_interrupted(run_id)
        self._set_run(run_id, "running", resumed_stages=interrupted)
        try:
            while state.cursor is not None:
                stage = spec.stage(state.cursor)
                record = self.store.stage(run_id, stage.id)
                assert record is not None
                if record.status in ("passed", "skipped"):
                    state.cursor = spec.next_id(stage.id)
                    self._save(run_id, state)
                    continue
                self.store.update_run(run_id, current_stage=stage.id)
                if record.status == "waiting_approval":
                    outcome = self._resume_after_decision(run, spec, stage, state)
                else:
                    outcome = self._execute(run, spec, stage, state)
                if outcome.approval is not None:
                    self._set_stage(run_id, stage.id, "waiting_approval")
                    self._save(run_id, state)
                    self._set_run(
                        run_id,
                        "waiting_approval",
                        approval=outcome.approval.id,
                        kind=outcome.approval.kind,
                        summary=outcome.approval.summary,
                    )
                    return self._get(run_id)
                if outcome.passed:
                    self._finish_stage(run, stage, state, skipped=outcome.skipped)
                    state.cursor = spec.next_id(stage.id)
                else:
                    state.cursor = self._on_failure(run, spec, stage, state, outcome.reason)
                self._save(run_id, state)
        except RunFailed as exc:
            self._save(run_id, state)
            self._set_run(run_id, "failed", error=str(exc))
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

    def set_run_model(self, run_id: str, alias: str | None) -> RunRecord:
        """Change (or clear) a run's per-run model override; applies to the next LLM call."""
        if alias and alias not in self.live.settings.models.models:
            raise ConfigError(f"unknown model alias {alias!r}")
        self._get(run_id)
        self.store.update_run(run_id, model_override=alias)
        self.bus.emit("run.model", run_id=run_id, data={"model_override": alias})
        return self._get(run_id)

    def _model_override(self, run_id: str) -> str | None:
        run = self.store.get_run(run_id)
        return run.model_override if run else None

    def pending_approvals(self, run_id: str | None = None) -> list[ApprovalRecord]:
        return self.store.approvals(run_id, status="pending")

    def decide(
        self,
        run_id: str,
        decision: str,
        comment: str | None = None,
        *,
        approval_id: int | None = None,
    ) -> ApprovalRecord:
        """Record a human decision: approved, rejected or changes_requested.

        Gate and budget decisions take effect on the next `run()`. A network decision
        is picked up immediately by the agent that is waiting for it.
        """
        pending = self.pending_approvals(run_id)
        if approval_id is not None:
            pending = [a for a in pending if a.id == approval_id]
        if not pending:
            raise PipelineError(f"run {run_id!r} has no pending approval")
        if len(pending) > 1:
            ids = ", ".join(str(a.id) for a in pending)
            raise PipelineError(f"run {run_id!r} has several pending approvals ({ids}); pick one")
        approval = pending[0]
        if not self.store.decide_approval(approval.id, decision, comment):
            raise PipelineError(f"approval {approval.id} was already decided")
        decided = self.store.approval(approval.id)
        assert decided is not None
        self.bus.emit(
            "approval.decided",
            run_id=run_id,
            stage=approval.stage_id,
            data={
                "id": approval.id,
                "kind": approval.kind,
                "decision": decision,
                "comment": comment,
            },
        )
        return decided

    # ---------------------------------------------------------------------- stage flow

    def _execute(
        self, run: RunRecord, spec: PipelineSpec, stage: StageSpec, state: RunState
    ) -> Outcome:
        store = self.artifacts(run.id)
        if stage.when:
            holds, why = evaluate_all(stage.when, store)
            if not holds:
                self.bus.emit("stage.skipped", run_id=run.id, stage=stage.id, data={"why": why})
                return Outcome(passed=True, skipped=True)
        self._set_stage(run.id, stage.id, "running")
        if stage.approval_before and not state.preapproved.get(stage.id):
            return self._request(
                run, stage, "gate", f"Approve stage {stage.id!r} ({stage.type}) before it runs",
                {"phase": "before"},
            )  # fmt: skip
        if (over := self._budget_exceeded(run.id, spec, stage.id, state)) is not None:
            return self._request_budget(run, stage, over)

        # ---- body
        if stage.is_builtin:
            body = self._run_builtin(run, stage)
        else:
            branches = self._run_branches(run, spec, stage, state)
            over_budget = [
                b.result.budget_exceeded for b in branches if b.result and b.result.budget_exceeded
            ]
            if over_budget:
                over = self._budget_exceeded(run.id, spec, stage.id, state)
                return self._request_budget(run, stage, over or ("run", over_budget[0]))
            failures = [
                f"{b.agent.name if b.agent else '?'}: {b.error}" for b in branches if b.error
            ]
            body = Outcome(passed=not failures, reason="; ".join(failures) or None)
        if not body.passed:
            return body

        # ---- automated gate (validity / tests) first, then verdict conditions
        gate = stage.gate
        if gate.type in ("check", "artifact_valid"):
            checked = (
                self._check_gate(run, stage)
                if gate.type == "check"
                else self._artifact_gate(run, stage)
            )
            if not checked.passed:
                return checked
        if stage.pass_when:
            holds, why = evaluate_all(stage.pass_when, store)
            self._emit_gate(run.id, stage.id, "pass_when", holds, why)
            if not holds:
                inputs = sorted({c.artifact for c in stage.pass_when if c.artifact})
                return Outcome(
                    False,
                    f"conditions not met: {'; '.join(why)}. Details are in {', '.join(inputs)}.",
                )
        # ---- human gate last: people only see work that passed the automated checks
        if gate.type == "human_approval" and not stage.approval_before:
            return self._request(
                run, stage, "gate", f"Review the output of stage {stage.id!r} and approve it",
                {"phase": "after", "artifacts": self._stage_outputs(stage)},
            )  # fmt: skip
        return Outcome(passed=True)

    def _resume_after_decision(
        self, run: RunRecord, spec: PipelineSpec, stage: StageSpec, state: RunState
    ) -> Outcome:
        approval = self._latest_approval(run.id, stage.id)
        if approval is None or approval.status == "pending":
            # Should not happen (run() returns early while pending); re-run the stage.
            return self._execute(run, spec, stage, state)
        comment = approval.comment
        phase = approval.details_dict.get("phase")
        if approval.status == "rejected":
            self._set_stage(run.id, stage.id, "failed", f"rejected: {comment or 'no comment'}")
            raise RunFailed(f"stage {stage.id!r} rejected by reviewer: {comment or 'no comment'}")
        if approval.kind == "budget":
            scope = str(approval.details_dict.get("scope", "run"))
            state.allowance[scope] = state.allowance.get(scope, 0) + 1
            if comment:
                state.feedback[stage.id] = comment
            return self._execute(run, spec, stage, state)
        if approval.status == "changes_requested":
            note = f"A human reviewer requested changes: {comment or '(no comment)'}"
            if stage.is_builtin:
                return Outcome(False, note)  # handled by on_fail (e.g. goto)
            state.feedback[stage.id] = note
            state.preapproved.pop(stage.id, None)
            return self._execute(run, spec, stage, state)
        # approved
        if phase == "before":
            state.preapproved[stage.id] = True
            return self._execute(run, spec, stage, state)
        return Outcome(passed=True)

    def _finish_stage(
        self, run: RunRecord, stage: StageSpec, state: RunState, *, skipped: bool
    ) -> None:
        if not skipped:
            error = self._merge(run, stage)
            if error:
                self._set_stage(run.id, stage.id, "failed", error)
                raise RunFailed(f"stage {stage.id!r}: {error}")
        for bucket in (state.feedback, state.retries, state.preapproved):
            bucket.pop(stage.id, None)
        self._set_stage(run.id, stage.id, "skipped" if skipped else "passed")

    def _on_failure(
        self,
        run: RunRecord,
        spec: PipelineSpec,
        stage: StageSpec,
        state: RunState,
        reason: str | None,
    ) -> str | None:
        """Apply on_fail and return the next cursor; raises RunFailed when out of options."""
        reason = reason or "stage failed"
        rule = stage.on_fail
        state.preapproved.pop(stage.id, None)
        if rule is not None and state.retries.get(stage.id, 0) < rule.retry:
            state.retries[stage.id] = state.retries.get(stage.id, 0) + 1
            state.feedback[stage.id] = f"The previous attempt failed: {reason}"
            self.bus.emit(
                "stage.retry",
                run_id=run.id,
                stage=stage.id,
                data={"retry": state.retries[stage.id], "of": rule.retry, "reason": reason},
            )
            self._set_stage(run.id, stage.id, "pending", reason)
            return stage.id
        if rule is not None and rule.then:
            error = self._merge(run, stage)  # carry the work forward so later stages see it
            self._set_stage(run.id, stage.id, "failed", reason)
            if error:
                raise RunFailed(f"stage {stage.id!r}: {error}")
            self.bus.emit(
                "stage.continue",
                run_id=run.id,
                stage=stage.id,
                data={"then": rule.then, "reason": reason},
            )
            state.retries.pop(stage.id, None)
            return rule.then
        if rule is not None and rule.goto:
            loops = state.loops.get(stage.id, 0)
            if loops < rule.max_loops:
                state.loops[stage.id] = loops + 1
                for sid in spec.between(rule.goto, stage.id):
                    self._set_stage(run.id, sid, "pending")
                    state.retries.pop(sid, None)
                    state.preapproved.pop(sid, None)
                state.feedback[rule.goto] = (
                    f"Stage {stage.id!r} failed and sent work back: {reason}"
                )
                self.bus.emit(
                    "stage.loop",
                    run_id=run.id,
                    stage=stage.id,
                    data={"goto": rule.goto, "loop": loops + 1, "max_loops": rule.max_loops},
                )
                return rule.goto
            self._set_stage(run.id, stage.id, "failed", reason)
            raise RunFailed(
                f"stage {stage.id!r} still failing after {rule.max_loops} loop(s) "
                f"back to {rule.goto!r}: {reason}"
            )
        self._set_stage(run.id, stage.id, "failed", reason)
        raise RunFailed(f"stage {stage.id!r}: {reason}")

    # -------------------------------------------------------------------- agent bodies

    def _run_branches(
        self, run: RunRecord, spec: PipelineSpec, stage: StageSpec, state: RunState
    ) -> list[_Branch]:
        branches = stage.branches
        if len(branches) == 1:
            return [self._invoke(run, spec, stage, branches[0], state)]
        workers = min(len(branches), self.live.settings.factory.limits.max_parallel_agents)
        self.bus.emit(
            "stage.parallel",
            run_id=run.id,
            stage=stage.id,
            data={"branches": [b.agent for b in branches], "workers": workers},
        )
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"stage-{stage.id}"
        ) as pool:
            futures = [pool.submit(self._invoke, run, spec, stage, b, state) for b in branches]
            return [f.result() for f in futures]

    def _invoke(
        self, run: RunRecord, spec: PipelineSpec, stage: StageSpec, branch: Branch, state: RunState
    ) -> _Branch:
        agent = self.agents.get(branch.agent)  # re-reads agent.md: edits apply next invocation
        if agent is None:
            err = self.agents.error_for(branch.agent)
            reason = f"agent {branch.agent!r} " + (
                f"is invalid: {err.message}" if err else "not found"
            )
            return _Branch(None, error=reason)
        writable = agent.level >= IsolationLevel.L2 and agent.role not in READ_ONLY_ROLES
        out = _Branch(agent, writable=writable)
        record = self.store.stage(run.id, stage.id)
        try:
            isolation = self._isolation(run, agent, writable, state)
            ctx = InvocationContext(
                run_id=run.id,
                stage_id=stage.id,
                request=run.request,
                artifacts=self.artifacts(run.id),
                model_override=run.model_override,
                model_default=branch.model,
                feedback=state.feedback.get(stage.id),
                attempt=record.attempts if record else 1,
                isolation=isolation,
                budget_check=lambda: self._budget_reason(run.id, spec, stage.id, state),
                model_override_source=lambda: self._model_override(run.id),
            )
            out.result = self.runtime.invoke(agent, ctx)
            if out.result.status != "passed":
                out.error = out.result.error or "agent failed"
            if isolation is not None and writable:
                status = "passed" if out.error is None else "failed"
                sha = isolation.repo.commit(
                    agent.name,
                    f"factory: {stage.id} by {agent.name} ({status}, attempt {ctx.attempt})",
                )
                self.bus.emit(
                    "workspace.committed",
                    run_id=run.id,
                    stage=stage.id,
                    agent=agent.name,
                    data={"commit": sha, "branch": isolation.repo.branch(agent.name)},
                )
        except (GitError, SandboxError) as exc:
            out.error = str(exc)
        except Exception as exc:
            out.error = f"internal error: {type(exc).__name__}: {exc}"
        return out

    def _merge(self, run: RunRecord, stage: StageSpec) -> str | None:
        """Merge every writing agent of the stage into integration. Returns an error."""
        if stage.is_builtin:
            return None
        for branch in stage.branches:
            agent = self.agents.agents.get(branch.agent)
            if agent is None or agent.level < IsolationLevel.L2 or agent.role in READ_ONLY_ROLES:
                continue
            repo = self.repo(run)
            if not repo.worktree_path(agent.name).exists():
                continue
            try:
                merged = repo.merge_to_integration(
                    agent.name, f"factory: merge {stage.id} ({agent.name})"
                )
            except GitError as exc:
                return str(exc)
            commit = run_git(["rev-parse", repo.branch(agent.name)], repo.repo).strip()
            self.bus.emit(
                "workspace.landed",
                run_id=run.id,
                stage=stage.id,
                agent=agent.name,
                data={
                    "commit": commit,
                    "merged_to_integration": merged,
                    "branch": repo.branch(agent.name),
                },
            )
        return None

    # --------------------------------------------------------------- gates & builtins

    def _check_gate(self, run: RunRecord, stage: StageSpec) -> Outcome:
        gate = stage.gate
        assert gate.command is not None
        workdir = self._check_workdir(run, stage)
        report_name = gate.report or f"{stage.id}_check.json"
        report = self._sandboxed(run, gate.command, workdir, gate.timeout_s)
        self.artifacts(run.id).write(report_name, json.dumps(report, indent=2) + "\n")
        passed = bool(report["passed"]) or not gate.must_pass
        detail = f"`{gate.command}` exited {report['exit_code']}" + (
            " (timed out)" if report["timed_out"] else ""
        )
        self._emit_gate(run.id, stage.id, "check", passed, [detail], report=report_name)
        if passed:
            return Outcome(passed=True)
        tail = (report["stdout_tail"] + "\n" + report["stderr_tail"]).strip()[-1500:]
        return Outcome(False, f"check {detail}. Full output in {report_name}.\n{tail}")

    def _artifact_gate(self, run: RunRecord, stage: StageSpec) -> Outcome:
        store = self.artifacts(run.id)
        problems: list[str] = []
        for check in stage.gate.schemas:
            try:
                doc = load_json_artifact(store, check.artifact)
                schema = json.loads((self.live.root / check.schema_path).read_text("utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(str(exc))
                continue
            problems += [f"{check.artifact}: {e}" for e in validate(doc, schema)]
        self._emit_gate(run.id, stage.id, "artifact_valid", not problems, problems)
        if problems:
            return Outcome(False, "invalid artifact(s): " + "; ".join(problems[:20]))
        return Outcome(passed=True)

    def _run_builtin(self, run: RunRecord, stage: StageSpec) -> Outcome:
        try:
            if stage.type == "builtin.merge":
                delivery = self.repo(run).deliver(Path(run.repo) if run.repo else None)
                self.artifacts(run.id).write("delivery.json", json.dumps(delivery, indent=2) + "\n")
                self.bus.emit("run.delivered", run_id=run.id, stage=stage.id, data=delivery)
                return Outcome(passed=True)
            command = stage.shell_command
            assert command is not None
            workdir = self.repo(run).prepare(BUILTIN_WORKTREE, writable=False)
            report = self._sandboxed(run, command, workdir, None)
            self.artifacts(run.id).write(f"{stage.id}.json", json.dumps(report, indent=2) + "\n")
            self._emit_gate(
                run.id, stage.id, stage.type, bool(report["passed"]),
                [f"`{command}` exited {report['exit_code']}"], report=f"{stage.id}.json",
            )  # fmt: skip
            if report["passed"]:
                return Outcome(passed=True)
            tail = (report["stdout_tail"] + "\n" + report["stderr_tail"]).strip()[-1500:]
            return Outcome(False, f"`{command}` exited {report['exit_code']}:\n{tail}")
        except (GitError, SandboxError) as exc:
            return Outcome(False, str(exc))

    def _check_workdir(self, run: RunRecord, stage: StageSpec) -> Path:
        """A single writing agent's own worktree (its unmerged work), else integration."""
        if stage.type == "agent" and stage.agent:
            agent = self.agents.agents.get(stage.agent)
            repo = self.repo(run)
            if (
                agent is not None
                and agent.level >= IsolationLevel.L2
                and agent.role not in READ_ONLY_ROLES
                and repo.worktree_path(agent.name).exists()
            ):
                return repo.worktree_path(agent.name)
        return self.repo(run).prepare(BUILTIN_WORKTREE, writable=False)

    def _sandboxed(
        self, run: RunRecord, command: str, workdir: Path, timeout_s: int | None
    ) -> dict[str, Any]:
        selection = self._announce_sandbox(run)
        result = selection.backend.run(command, workdir=workdir, timeout_s=timeout_s)
        return {
            "command": command,
            "passed": result.exit_code == 0 and not result.timed_out,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_s": result.duration_s,
            "backend": result.backend,
            "stdout_tail": result.stdout[-_OUTPUT_TAIL:],
            "stderr_tail": result.stderr[-_OUTPUT_TAIL:],
        }

    # --------------------------------------------------------------------- approvals

    def _request(
        self,
        run: RunRecord,
        stage: StageSpec | None,
        kind: ApprovalKind,
        summary: str,
        details: dict[str, Any],
    ) -> Outcome:
        approval = self.store.create_approval(
            run.id,
            stage_id=stage.id if stage else None,
            kind=kind,
            summary=summary,
            details=details,
        )
        self.bus.emit(
            "approval.requested",
            run_id=run.id,
            stage=approval.stage_id,
            data={"id": approval.id, "kind": kind, "summary": summary, "details": details},
        )
        return Outcome(passed=False, approval=approval)

    def _request_budget(self, run: RunRecord, stage: StageSpec, over: tuple[str, str]) -> Outcome:
        scope, message = over
        self.bus.emit(
            "budget.exceeded",
            run_id=run.id,
            stage=stage.id,
            data={"scope": scope, "message": message},
        )
        summary = f"Budget exceeded at stage {stage.id!r}: {message}. Approve to continue."
        return self._request(run, stage, "budget", summary, {"scope": scope, "message": message})

    def _latest_approval(self, run_id: str, stage_id: str) -> ApprovalRecord | None:
        mine = [
            a
            for a in self.store.approvals(run_id)
            if a.stage_id == stage_id and a.kind != "network"
        ]
        return mine[-1] if mine else None

    def _pending_gate(self, run_id: str) -> ApprovalRecord | None:
        pending = [a for a in self.pending_approvals(run_id) if a.kind != "network"]
        return pending[0] if pending else None

    def _network_approver(
        self, run: RunRecord, agent: AgentSpec, state: RunState
    ) -> Callable[[str], bool]:
        """Callback for L3 agents: block until a human approves network use (first use only)."""

        def approve(command: str) -> bool:
            with self._network_lock:  # parallel L3 agents share one approval per run
                if state.network is not None:
                    return state.network == "approved"
                approval = self.store.create_approval(
                    run.id, stage_id=None, kind="network",
                    summary=f"Agent {agent.name!r} wants network access for: {command}",
                    details={"agent": agent.name, "command": command},
                )  # fmt: skip
                self.bus.emit(
                    "approval.requested", run_id=run.id, agent=agent.name,
                    data={"id": approval.id, "kind": "network", "summary": approval.summary},
                )  # fmt: skip
                self.store.update_run(run.id, status="waiting_approval")
                timeout = self.live.settings.factory.limits.network_approval_timeout_s
                deadline = time.monotonic() + timeout
                decided = approval
                while decided.status == "pending" and time.monotonic() < deadline:
                    time.sleep(0.25)
                    decided = self.store.approval(approval.id) or decided
                if decided.status == "pending":
                    self.store.decide_approval(
                        approval.id, "rejected", f"timed out after {timeout}s"
                    )
                    decided = self.store.approval(approval.id) or decided
                state.network = "approved" if decided.status == "approved" else "denied"
                self.store.update_run(run.id, status="running")
                self._save(run.id, state)
                return state.network == "approved"

        return approve

    # ----------------------------------------------------------------------- budgets

    def _limits(self, spec: PipelineSpec, stage_id: str) -> dict[str, tuple[float | None, str]]:
        cfg = self.live.settings.factory.budgets
        stage_budget = spec.stage(stage_id).budget
        run_budget = spec.budget
        # Pipeline / stage `budget:` values override the config defaults.
        run_tokens = run_budget.tokens if run_budget and run_budget.tokens else None
        run_cost = run_budget.cost_usd if run_budget and run_budget.cost_usd else None
        stage_tokens = stage_budget.tokens if stage_budget and stage_budget.tokens else None
        stage_cost = stage_budget.cost_usd if stage_budget and stage_budget.cost_usd else None
        stage_scope = f"stage:{stage_id}"
        return {
            "run_tokens": (run_tokens or cfg.per_run_tokens, "run"),
            "run_cost": (run_cost or cfg.per_run_cost_usd, "run"),
            "stage_tokens": (stage_tokens or cfg.per_stage_tokens, stage_scope),
            "stage_cost": (stage_cost or cfg.per_stage_cost_usd, stage_scope),
        }

    def _budget_exceeded(
        self, run_id: str, spec: PipelineSpec, stage_id: str, state: RunState
    ) -> tuple[str, str] | None:
        """(scope, message) for the first exhausted budget, else None."""
        run_total = self.store.usage_total(run_id)
        stage_total = self.store.usage_total(run_id, stage_id)
        used = {
            "run_tokens": float(run_total.total_tokens),
            "run_cost": run_total.cost_usd,
            "stage_tokens": float(stage_total.total_tokens),
            "stage_cost": stage_total.cost_usd,
        }
        for key, (limit, scope) in self._limits(spec, stage_id).items():
            if limit is None:
                continue
            effective = limit * (1 + state.allowance.get(scope, 0))
            if used[key] >= effective:
                unit = "tokens" if key.endswith("tokens") else "USD"
                what = "run" if scope == "run" else f"stage {stage_id!r}"
                return scope, f"{what} used {used[key]:g} {unit} (limit {effective:g})"
        return None

    def _budget_reason(
        self, run_id: str, spec: PipelineSpec, stage_id: str, state: RunState
    ) -> str | None:
        over = self._budget_exceeded(run_id, spec, stage_id, state)
        return over[1] if over else None

    # ------------------------------------------------------------------- isolation

    def repo(self, run: RunRecord) -> RunRepo:
        """The run's private git repo (created on first use)."""
        repo = self._repos.get(run.id)
        if repo is None:
            repo = RunRepo(self.live.settings.path("workspaces"), run.id)
            self._repos[run.id] = repo
        repo.ensure(Path(run.repo) if run.repo else None)
        return repo

    def sandbox(self) -> SandboxSelection:
        """The sandbox backend for this process (selected once, on first use)."""
        if self._sandbox is None:
            self._sandbox = select_backend(self.live.settings.factory.sandbox)
        return self._sandbox

    def _announce_sandbox(self, run: RunRecord) -> SandboxSelection:
        selection = self.sandbox()
        with self._state_lock:
            first = run.id not in self._isolation_reported
            self._isolation_reported.add(run.id)
        if first:
            self.bus.emit(
                "isolation.status",
                run_id=run.id,
                data={
                    "backend": selection.backend.name,
                    "reduced": selection.reduced_isolation,
                    "reason": selection.reason,
                    "message": selection.describe(),
                },
            )
        return selection

    def _isolation(
        self, run: RunRecord, agent: AgentSpec, writable: bool, state: RunState
    ) -> AgentIsolation | None:
        if agent.level < IsolationLevel.L1:
            return None  # L0: text in, artifact out
        repo = self.repo(run)
        workspace = repo.prepare(agent.name, writable=writable)
        sandbox = self._announce_sandbox(run).backend if agent.level >= IsolationLevel.L2 else None
        approver = (
            self._network_approver(run, agent, state) if agent.level >= IsolationLevel.L3 else None
        )
        return AgentIsolation(
            workspace=workspace, repo=repo, sandbox=sandbox, approve_network=approver
        )

    # ---------------------------------------------------------------------- plumbing

    def _stage_outputs(self, stage: StageSpec) -> list[str]:
        names: list[str] = []
        for branch in stage.branches:
            agent = self.agents.agents.get(branch.agent)
            if agent:
                names += agent.outputs
        return names

    def _load_state(self, run: RunRecord, spec: PipelineSpec) -> RunState:
        state = RunState.load(run.state_dict)
        if state.cursor is None or state.cursor not in spec.ids:
            # Runs created before the persisted cursor existed: first unfinished stage.
            done = {
                s.stage_id for s in self.store.stages(run.id) if s.status in ("passed", "skipped")
            }
            state.cursor = next((sid for sid in spec.ids if sid not in done), None)
        return state

    def _save(self, run_id: str, state: RunState) -> None:
        with self._state_lock:
            self.store.save_state(run_id, state.dump())

    def _emit_gate(
        self, run_id: str, stage_id: str, gate: str, passed: bool, detail: list[str], **extra: Any
    ) -> None:
        self.bus.emit(
            "gate.evaluated",
            run_id=run_id,
            stage=stage_id,
            data={"gate": gate, "passed": passed, "detail": detail, **extra},
        )

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
