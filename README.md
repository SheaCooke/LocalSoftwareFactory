# Software Factory

A local, model-agnostic software factory. LLM-powered agents take a feature request
through defined stages (requirements → design → plan → tests → implementation →
review → docs → integrate) and produce working code in a target Git repository.
Agents are isolated from each other by context, filesystem, execution sandbox and
secrets. You can switch any agent to any model (cloud or local) mid-run, with no restart.

> **Status: Milestone 2 of 7.** Config, providers (Fake and OpenAI-compatible),
> declarative agents, the artifact store, and a sequential orchestrator with SQLite
> state, JSONL event logs and transcripts all work. The `quick` pipeline runs end to
> end. The `default` pipeline needs gates, parallel stages and loops (milestone 4) and
> file/sandbox tools (milestone 3). See
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#12-milestones).

## Quickstart (Windows 11)

Requirements: Python 3.12+, Git. Docker Desktop (WSL2 backend) is optional and
gives full sandbox isolation.

```powershell
.\scripts\setup.ps1          # installs uv if needed, syncs .venv, creates .env
uv run factory prompt "hello" --model echo
```

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
`events.jsonl`. State lives in `runs/factory.db`.

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
