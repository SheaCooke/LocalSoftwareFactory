"""`factory` command-line interface."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from factory.config.loader import ConfigError, LiveConfig, find_root
from factory.core.events import Event
from factory.core.orchestrator import Orchestrator
from factory.core.pipeline import PipelineError
from factory.isolation.sandboxes import SandboxError, select_backend
from factory.isolation.sandboxes.docker_backend import DockerSandbox, build_image, container_user
from factory.isolation.worktrees import RunRepo
from factory.providers.base import Message, ProviderError, Response
from factory.providers.resolver import ModelResolver
from factory.secrets import redactor

app = typer.Typer(no_args_is_help=True, help="Local, model-agnostic software factory.")
models_app = typer.Typer(no_args_is_help=True, help="Inspect the model registry.")
model_app = typer.Typer(no_args_is_help=True, help="Switch the active model (no restart needed).")
run_app = typer.Typer(no_args_is_help=True, help="Start a run.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect and resume runs.")
agents_app = typer.Typer(no_args_is_help=True, help="Inspect agent definitions.")
pipelines_app = typer.Typer(no_args_is_help=True, help="Inspect pipelines.")
sandbox_app = typer.Typer(no_args_is_help=True, help="Inspect or build the command sandbox.")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(models_app, name="models")
app.add_typer(model_app, name="model")
app.add_typer(run_app, name="run")
app.add_typer(runs_app, name="runs")
app.add_typer(agents_app, name="agents")
app.add_typer(pipelines_app, name="pipelines")

RootOption = Annotated[
    Path | None,
    typer.Option("--root", help="Factory root (default: $FACTORY_ROOT or nearest config/)."),
]


def _fail(message: str) -> typer.Exit:
    typer.secho(redactor.redact(message), fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _live(root: Path | None) -> LiveConfig:
    try:
        return LiveConfig((root or find_root()).resolve())
    except ConfigError as exc:
        raise _fail(f"config error: {exc}") from exc


@app.command()
def prompt(
    text: Annotated[str, typer.Argument(help="Prompt text. Use '-' to read stdin.")],
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="Model alias (run-level override).")
    ] = None,
    agent: Annotated[
        str | None, typer.Option("--agent", "-a", help="Resolve the model as this agent would.")
    ] = None,
    system: Annotated[str | None, typer.Option("--system", "-s", help="System prompt.")] = None,
    stream: Annotated[bool, typer.Option("--stream/--no-stream", help="Stream tokens.")] = True,
    root: RootOption = None,
) -> None:
    """Send one prompt through a model alias and print the reply."""
    if text == "-":
        text = sys.stdin.read()
    resolver = ModelResolver(_live(root))
    messages = [Message.system(system)] if system else []
    messages.append(Message.user(text))
    try:
        if stream:
            resolved, chunks = resolver.stream(messages, agent=agent, run_override=model)
            final: Response | None = None
            for chunk in chunks:
                if chunk.text:
                    typer.echo(chunk.text, nl=False)
                if chunk.response is not None:
                    final = chunk.response
            typer.echo()
        else:
            resolved, final = resolver.complete(messages, agent=agent, run_override=model)
            typer.echo(final.content)
    except (ConfigError, ProviderError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        resolver.close()
    if final is not None:
        cost = resolved.model.pricing.cost(final.usage.input_tokens, final.usage.output_tokens)
        typer.secho(
            f"-- {resolved.alias} ({resolved.provider_name}/{resolved.model_id}, "
            f"via {resolved.source.replace('_', ' ')}) "
            f"tokens in={final.usage.input_tokens} out={final.usage.output_tokens} "
            f"cost=${cost:.4f} latency={final.latency_ms:.0f}ms",
            fg=typer.colors.BRIGHT_BLACK,
            err=True,
        )


@models_app.command("list")
def models_list(root: RootOption = None) -> None:
    """List model aliases and show which one is active."""
    settings = _live(root).settings
    default = settings.default_model
    width = max(len(a) for a in settings.models.models)
    for alias, cfg in settings.models.models.items():
        marker = "*" if alias == default else " "
        caps = ("tools" if cfg.supports_tools else "json-tools") + (
            ",vision" if cfg.supports_vision else ""
        )
        typer.echo(
            f"{marker} {alias:<{width}}  {cfg.provider}/{cfg.model}  [{caps}, "
            f"ctx {cfg.context_window:,}]  {cfg.description}"
        )
    overrides = settings.agent_overrides
    if overrides:
        typer.echo("\nper-agent overrides:")
        for agent, alias in sorted(overrides.items()):
            typer.echo(f"  {agent} -> {alias}")


@model_app.command("use")
def model_use(
    alias: Annotated[str, typer.Argument(help="Model alias from models.yaml.")],
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="Only for this agent.")] = None,
    root: RootOption = None,
) -> None:
    """Make ALIAS the global default, or the override for one agent."""
    live = _live(root)
    try:
        live.set_model(alias, agent)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    target = f"agent {agent!r}" if agent else "global default"
    typer.echo(f"{target} -> {alias} (applies to the next LLM call)")


@model_app.command("reset")
def model_reset(
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="Only this agent.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Clear every runtime selection.")] = False,
    root: RootOption = None,
) -> None:
    """Clear a runtime selection so factory.yaml / agent.md defaults apply again."""
    live = _live(root)
    try:
        live.clear_model(agent, all_agents=all_)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"global default is now {live.settings.default_model}")


# ---------------------------------------------------------------------- runs & agents


def _orchestrator(root: Path | None) -> Orchestrator:
    return Orchestrator(_live(root))


def _print_event(event: Event) -> None:
    d, who = event.data, f"[{event.agent}]" if event.agent else ""
    if event.type == "stage.transition":
        to = d.get("to")
        color = {"passed": typer.colors.GREEN, "failed": typer.colors.RED}.get(str(to))
        suffix = f": {d['error']}" if d.get("error") else ""
        typer.secho(f"== stage {event.stage}: {to}{suffix}", fg=color or typer.colors.CYAN)
    elif event.type == "llm.call":
        typer.secho(
            f"   {who} llm {d['alias']} ({d['model']}) in={d['input_tokens']} "
            f"out={d['output_tokens']} tools={d['tool_calls']}",
            fg=typer.colors.BRIGHT_BLACK,
        )
    elif event.type == "tool.call":
        status = "ok" if d.get("ok") else "error"
        typer.echo(f"   {who} tool {d['tool']} -> {status}")
    elif event.type == "permission.decision" and d.get("decision") == "denied":
        typer.secho(
            f"   {who} DENIED {d['tool']} {d['target']}: {d['reason']}", fg=typer.colors.YELLOW
        )
    elif event.type == "artifact.written":
        typer.echo(f"   {who} wrote artifact {d['name']}")
    elif event.type == "isolation.status" and d.get("reduced"):
        typer.secho(f"!! {d['message']}", fg=typer.colors.YELLOW, bold=True)
    elif event.type == "workspace.landed":
        merged = " -> merged into integration" if d.get("merged_to_integration") else ""
        commit = str(d["commit"])[:10] if d.get("commit") else "no changes"
        typer.echo(f"   {who} committed {commit} on {d['branch']}{merged}")


def _execute(orch: Orchestrator, run_id: str, quiet: bool, *, resume: bool = False) -> None:
    unsubscribe = None if quiet else orch.bus.subscribe(_print_event)
    try:
        run = orch.resume(run_id) if resume else orch.run(run_id)
    finally:
        if unsubscribe:
            unsubscribe()
        orch.resolver.close()
    color = typer.colors.GREEN if run.status == "passed" else typer.colors.RED
    typer.secho(f"run {run.id}: {run.status}" + (f" ({run.error})" if run.error else ""), fg=color)
    if run.status == "failed":
        raise typer.Exit(1)


@run_app.command("start")
def run_start(
    request: Annotated[str, typer.Argument(help="Feature request text. Use '-' to read stdin.")],
    pipeline: Annotated[str, typer.Option("--pipeline", "-p", help="Pipeline name.")] = "default",
    repo: Annotated[
        str | None, typer.Option("--repo", help="Target repository path (worktrees: milestone 3).")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="Per-run model override for every agent.")
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print the result.")] = False,
    root: RootOption = None,
) -> None:
    """Create a run and execute its pipeline."""
    if request == "-":
        request = sys.stdin.read()
    orch = _orchestrator(root)
    try:
        run = orch.create_run(request, pipeline=pipeline, repo=repo, model_override=model)
    except (ConfigError, PipelineError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"run {run.id} created ({pipeline})")
    _execute(orch, run.id, quiet)


@runs_app.command("list")
def runs_list(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20, root: RootOption = None
) -> None:
    """List recent runs."""
    orch = _orchestrator(root)
    runs = orch.store.list_runs(limit)
    if not runs:
        typer.echo("no runs yet")
    for run in runs:
        total = orch.store.usage_total(run.id)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.created_at))
        request = run.request.splitlines()[0][:60]
        typer.echo(
            f"{run.id}  {when}  {run.status:<16} {run.pipeline:<10} "
            f"{total.total_tokens:>8} tok ${total.cost_usd:.4f}  {request}"
        )


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Run id (see `factory runs list`).")],
    events: Annotated[int, typer.Option("--events", "-e", help="Show the last N events.")] = 0,
    root: RootOption = None,
) -> None:
    """Show a run's stages, token/cost tally, artifacts and transcripts."""
    orch = _orchestrator(root)
    run = orch.store.get_run(run_id)
    if run is None:
        raise _fail(f"run {run_id!r} not found")
    typer.echo(f"run       {run.id}")
    typer.echo(f"status    {run.status}" + (f"  ({run.error})" if run.error else ""))
    typer.echo(
        f"pipeline  {run.pipeline}"
        + (f"  model override: {run.model_override}" if run.model_override else "")
    )
    typer.echo(f"request   {run.request}")
    typer.echo("\nstages:")
    for st in orch.store.stages(run_id):
        marker = ">" if st.stage_id == run.current_stage else " "
        took = f"{st.finished_at - st.started_at:.1f}s" if st.started_at and st.finished_at else ""
        err = f"  {st.error}" if st.error else ""
        typer.echo(
            f" {marker} {st.stage_id:<14} {st.status:<16} attempts={st.attempts} {took}{err}"
        )
    typer.echo("\nusage:")
    for u in orch.store.usage_by_agent(run_id):
        typer.echo(
            f"   {u.agent or '-':<14} calls={u.calls:<3} in={u.input_tokens:<8} "
            f"out={u.output_tokens:<8} ${u.cost_usd:.4f}"
        )
    total = orch.store.usage_total(run_id)
    typer.echo(
        f"   {'total':<14} calls={total.calls:<3} tokens={total.total_tokens} ${total.cost_usd:.4f}"
    )
    typer.echo("\nartifacts:")
    for name in orch.artifacts(run_id).list():
        typer.echo(f"   {name}")
    run_repo = RunRepo(orch.live.settings.path("workspaces"), run_id)
    if run_repo.exists:
        files = run_repo.files_on_integration()
        typer.echo(f"\nworkspace: {run_repo.repo}  ({run_repo.integration}, {len(files)} files)")
    transcripts = sorted((orch.run_dir(run_id) / "transcripts").glob("*.json"))
    typer.echo(f"\ntranscripts: {len(transcripts)} in {orch.run_dir(run_id) / 'transcripts'}")
    if events:
        typer.echo("\nevents:")
        for ev in orch.event_log.read(run_id)[-events:]:
            when = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
            stage, agent = ev.get("stage") or "", ev.get("agent") or ""
            typer.echo(f"   {when} {ev['type']:<20} {stage:<12} {agent:<12} {ev['data']}")


@runs_app.command("resume")
def runs_resume(
    run_id: Annotated[str, typer.Argument(help="Run id to resume.")],
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
    root: RootOption = None,
) -> None:
    """Resume a run from its last completed stage (re-runs an interrupted stage)."""
    orch = _orchestrator(root)
    run = orch.store.get_run(run_id)
    if run is None:
        raise _fail(f"run {run_id!r} not found")
    _execute(orch, run_id, quiet, resume=True)


@agents_app.command("list")
def agents_list(root: RootOption = None) -> None:
    """List agent definitions and any load errors."""
    orch = _orchestrator(root)
    for name, spec in sorted(orch.agents.agents.items()):
        model = spec.model or "(default)"
        typer.echo(f"  {name:<14} {spec.isolation_level}  {model:<12} {spec.description}")
    for err in orch.agents.errors:
        typer.secho(f"! {err.name:<14} {err.path}: {err.message}", fg=typer.colors.RED)


@pipelines_app.command("list")
def pipelines_list(root: RootOption = None) -> None:
    """List pipelines, whether they can run yet, and any definition errors."""
    orch = _orchestrator(root)
    pipelines, errors = orch.pipelines()
    for name, spec in sorted(pipelines.items()):
        stages = " -> ".join(s.id for s in spec.stages)
        try:
            orch.check_runnable(spec)
            note = ""
        except PipelineError:
            note = "  (needs a later milestone or has errors; see `factory run start`)"
        typer.echo(f"  {name:<10} {stages}{note}")
    for file, message in sorted(errors.items()):
        typer.secho(f"! {file}: {message}", fg=typer.colors.RED)


@sandbox_app.command("status")
def sandbox_status(root: RootOption = None) -> None:
    """Show which sandbox backend shell.run_sandboxed would use."""
    config = _live(root).settings.factory.sandbox
    try:
        selection = select_backend(config)
    except SandboxError as exc:
        raise _fail(str(exc)) from exc
    color = typer.colors.YELLOW if selection.reduced_isolation else typer.colors.GREEN
    typer.secho(selection.describe(), fg=color)
    if selection.reduced_isolation:
        typer.echo(f"allowlisted commands: {', '.join(config.command_allowlist)}")
        typer.echo(
            "For full isolation start Docker (WSL/Linux: `sudo service docker start`; "
            "Windows: Docker Desktop), then run `factory sandbox build`."
        )
    elif isinstance(selection.backend, DockerSandbox):
        typer.echo(f"image {config.image}, user {container_user()}, network none")
        memory = f"memory {config.memory}, cpus {config.cpus}, pids {config.pids_limit}"
        if DockerSandbox.swap_limit_supported():
            typer.echo(memory)
        else:
            typer.echo(f"{memory} (no swap accounting: per-process `ulimit -v` cap applied)")


@sandbox_app.command("build")
def sandbox_build(root: RootOption = None) -> None:
    """Build the Docker sandbox image (python + pytest/ruff/mypy, non-root user)."""
    config = _live(root).settings.factory.sandbox
    typer.echo(f"building {config.image} ...")
    try:
        proc = build_image(config)
    except FileNotFoundError as exc:
        raise _fail(
            "docker CLI not found; install Docker Engine (WSL/Linux) or Docker Desktop"
        ) from exc
    if proc.returncode != 0:
        raise _fail(f"docker build failed:\n{proc.stderr[-4000:]}")
    typer.secho(f"built {config.image}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
