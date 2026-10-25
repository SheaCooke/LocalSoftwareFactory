# Software Factory

A local, model-agnostic software factory. LLM-powered agents take a feature request
through defined stages (requirements → design → plan → tests → implementation →
review → docs → integrate) and produce working code in a target Git repository.
Agents are isolated from each other by context, filesystem, execution sandbox and
secrets. You can switch any agent to any model (cloud or local) mid-run, with no restart.

> **Status: Milestone 6 of 7.** Anthropic and Gemini adapters, hot reload of
> `config/` and `agents/`, and `factory new agent|pipeline` are done (see
> [docs/EXTENDING.md](docs/EXTENDING.md)). Remaining: the end-to-end todo-app test
> (milestone 7). Earlier milestones: Config, providers (Fake and OpenAI-compatible),
> declarative agents, and full isolation all work. So does the complete pipeline
> interpreter: gates, parallel stages, loops, budgets and resume. `factory up` adds
> a live web UI with gate controls and model switching. Remaining: Anthropic and
> Gemini adapters, the config file watcher, `factory new agent` (milestone 6), and
> the end-to-end todo-app test (milestone 7). See
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/ISOLATION.md](docs/ISOLATION.md).

## Quickstart

### WSL2 (recommended)

Keep the project in the Linux filesystem (for example `~/Software_Factory`, not
`/mnt/c/...`). You need Git and, for full isolation, Docker Engine inside the
distro (`sudo service docker start`). Your user must be in the `docker` group.
Python 3.12 is installed by uv; the distro's own Python version doesn't matter.

```bash
./scripts/setup.sh           # installs uv + Python 3.12, syncs .venv, creates .env, builds the sandbox image
uv run factory up            # orchestrator + web UI on http://localhost:8765 (opens your Windows browser)
```

`factory up` binds to `127.0.0.1` inside WSL. WSL2 forwards `localhost`, so the
same URL works in your Windows browser. Runs keep going while the browser is
closed. Stopping `factory up` pauses them, and the next `factory up` resumes them.

From Windows, open the folder as `\\wsl.localhost\Ubuntu-22.04\home\<you>\Software_Factory`.
Run commands inside WSL: the tools are much slower across the `\\wsl.localhost` share.

### Windows 11 (native)

Requirements: Python 3.12+, Git. Docker Desktop (WSL2 backend) is optional and
gives full sandbox isolation. Without it, commands run in the reduced-isolation
subprocess sandbox.

```powershell
.\scripts\setup.ps1          # installs uv if needed, syncs .venv, creates .env
uv run factory prompt "hello" --model echo
```

The examples below work the same in both shells.

Point at a real model by adding its key to `.env` (or env vars / Windows Credential
Manager) and picking an alias:

```powershell
uv run factory models list                     # * marks the active default
uv run factory prompt "Write a haiku about diffs" --model smart
uv run factory model use local-coder           # switch the global default (Ollama)
uv run factory model use smart --agent implementer
uv run factory model reset --all               # back to factory.yaml defaults
```

Run a pipeline. `--model echo` keeps it offline; omit it to use each agent's own model:

```powershell
uv run factory agents list                     # definitions + any load errors
uv run factory pipelines list
uv run factory run start "A CLI todo app with add/list/done" --pipeline quick --model echo
uv run factory runs list
uv run factory runs show <run-id> --events 20  # stages, token/cost tally, artifacts
uv run factory runs resume <run-id>            # after a crash or a failed stage
```

Each run writes `runs/<run-id>/artifacts/`, `transcripts/` (redacted) and
`events.jsonl`. State lives in `runs/factory.db`. Code is produced in
`workspaces/<run-id>/`: a private clone of `--repo` (or a new project) plus one git
worktree per agent. Your target repository is never modified by a run.

```powershell
uv run factory run start "A calculator CLI" --pipeline build --repo C:\src\myproj
uv run factory sandbox status                  # Docker, or REDUCED ISOLATION fallback
uv run factory sandbox build                   # build the Docker sandbox image
```

### Human gates and approvals

The `default` pipeline pauses for you after requirements and design, and before
delivering code. A paused run prints what to review and how to answer:

```bash
uv run factory run start "A CLI todo app with add/list/done"   # stops at the requirements gate
uv run factory runs approvals                                   # everything waiting on you
uv run factory runs request-changes <run-id> -c "Store todos in ~/.todo.json"   # re-runs the agent with your note
uv run factory runs approve <run-id> [-c "lgtm"]                # continue
uv run factory runs reject <run-id> -c "Wrong approach"          # fail the run
```

The same commands answer budget overruns, where approving grants one more full
budget, and L3 network requests (use `--id` from `runs approvals`). After delivery,
`delivery.json` in the run's artifacts says where the code went: branch
`factory/<run-id>` in your `--repo`, ready to merge.

### Web UI

`uv run factory up` (`--port N`, `--no-open`, `--no-resume`) gives you:
- the pipeline as a stage graph, with the current stage highlighted;
- a live activity feed (messages, tool calls, permission denials) you can filter by agent;
- Approve / Request changes / Reject buttons with a comment box for every gate;
- artifacts and code diffs per stage;
- token and cost tallies;
- model switching: a global default, per-agent overrides, and a per-run model,
  all applied on the next LLM call.

Banners flag config errors, invalid agents and reduced isolation. The CLI
commands above keep working alongside the UI, against the same database.

## Layout

| Path | What |
|---|---|
| `config/models.yaml` | Providers (endpoints) and model aliases with capabilities |
| `config/factory.yaml` | Default model, per-agent overrides, ports, paths, sandbox, budgets |
| `config/overrides.yaml` | Machine-managed runtime model selections (git-ignored) |
| `config/pipelines/` | Stage graphs |
| `agents/` | One `agent.md` per agent (milestone 2) |
| `factory/` | The Python package: `config`, `providers`, `core`, `agents`, `tools`, `isolation`, `ui` |
| `docs/` | Architecture, decisions, extending, isolation |

## Development

```powershell
uv run pytest
uv run ruff check .
uv run mypy factory
```
