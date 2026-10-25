# Software Factory

A local, model-agnostic software factory. LLM-powered agents take a feature request
through defined stages (requirements → design → plan → tests → implementation →
review → docs → integrate) and produce working code in a target Git repository.
Agents are isolated from each other by context, filesystem, execution sandbox and
secrets. You can switch any agent to any model (cloud or local) mid-run, with no restart.

> **Status: Milestone 3 of 7.** Config, providers (Fake and OpenAI-compatible),
> declarative agents, the artifact store, a sequential orchestrator with SQLite
> state, and full isolation all work. Isolation means per-agent git worktrees, the
> path permission layer, and the Docker or restricted-subprocess sandbox. The `quick`
> and `build` pipelines run end to end. The `default` pipeline needs gates, parallel
> stages and loops (milestone 4). See
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#12-milestones) and
> [docs/ISOLATION.md](docs/ISOLATION.md).

## Quickstart

### WSL2 (recommended)

Keep the project in the Linux filesystem (for example `~/Software_Factory`, not
`/mnt/c/...`). You need Git and, for full isolation, Docker Engine inside the
distro (`sudo service docker start`). Your user must be in the `docker` group.
Python 3.12 is installed by uv; the distro's own Python version doesn't matter.

```bash
./scripts/setup.sh           # installs uv + Python 3.12, syncs .venv, creates .env, builds the sandbox image
uv run factory prompt "hello" --model echo
uv run factory sandbox status   # should say "docker sandbox"
```

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

`factory up` (orchestrator + UI on http://127.0.0.1:8765) lands in milestone 5.

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
