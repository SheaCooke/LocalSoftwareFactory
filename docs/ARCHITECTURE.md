# Architecture

The software factory is a local, model-agnostic system in which LLM-powered agents
take a feature request through defined stages (requirements → design → plan →
tests → implementation → review → docs → integrate) and produce working code in a
target Git repository.

This document is the plan of record. Sections marked **(M1)** … **(M7)** note the
milestone in which each part lands. Choices the build prompt left open are recorded
in [DECISIONS.md](DECISIONS.md).

---

## 1. Goals and non-goals

**Goals**

- Runs on Windows 11 with Python 3.12+ and `uv`, either natively (PowerShell scripts,
  Docker Desktop optional) or inside WSL2 (bash scripts, Docker Engine in the
  distro). WSL2 is the recommended setup. Code assumes no particular shell, and
  every path goes through `pathlib`. Windows-specific path defences stay active on
  native Windows.
- Any LLM: OpenAI-compatible servers (OpenAI, Azure, Ollama, LM Studio, vLLM,
  OpenRouter), Anthropic, Gemini, plus a Fake provider for offline tests.
- Instant model switching with no restart: the model is resolved on every LLM call
  from a live config object.
- Isolation enforced in code: context, filesystem, execution and secrets.
- Everything declarative: agents are Markdown files, pipelines are YAML, tools and
  providers are plugins discovered at runtime.
- One command (`factory up`) starts orchestrator and UI on `127.0.0.1:8765`.

**Non-goals**

- Multi-user or remote hosting. The UI binds to loopback only and has no auth.
- A general workflow engine. Pipelines cover the stage graph features listed in §6
  and nothing more.
- Model routing or cost optimisation beyond explicit aliases and budget guards.

---

## 2. Component overview

```
                      ┌────────────────────────────────────────────┐
  Browser  ◀── SSE ── │ factory.ui  (FastAPI, static HTML/JS)       │
     │                └──────────────┬─────────────────────────────┘
     │ REST                          │ commands / queries
     ▼                               ▼
┌──────────────┐   events   ┌─────────────────────────────────────────┐
│ factory.cli  │──────────▶ │ factory.core                             │
└──────────────┘            │  Orchestrator ─ PipelineInterpreter      │
                            │  RunStore (SQLite) ─ ArtifactStore       │
                            │  EventBus ─ EventLog (JSONL)             │
                            └───┬───────────────┬───────────────┬──────┘
                                │               │               │
                    ┌───────────▼───┐   ┌───────▼───────┐  ┌────▼──────────────┐
                    │ factory.agents│   │ factory.tools │  │ factory.isolation │
                    │ loader,runtime│──▶│ registry,@tool│─▶│ PermissionLayer   │
                    │ context build │   │ fs, shell, git│  │ Worktrees         │
                    └───────┬───────┘   └───────────────┘  │ Sandboxes (Docker │
                            │                              │  / subprocess)    │
                    ┌───────▼────────────────┐             └───────────────────┘
                    │ factory.providers      │
                    │ LLMProvider interface  │──▶ OpenAI-compat / Anthropic /
                    │ ModelResolver (live)   │    Gemini / Fake
                    └───────▲────────────────┘
                            │ reads every call
                    ┌───────┴────────────────┐
                    │ factory.config         │◀── watchdog on config/, agents/
                    │ LiveConfig (hot reload)│
                    └────────────────────────┘
```

Every arrow into an LLM goes through the orchestrator process. Agents are not
separate OS processes holding keys. An "agent" is a conversation loop run by
`factory.agents.runtime` inside the orchestrator. Its tools act on the filesystem
only through the permission layer, and on commands only through a sandbox.

---

## 3. Configuration (M1, hot reload M6)

| File | Contents |
|---|---|
| `config/models.yaml` | `providers:` (type, base_url, api_key_env) and `models:` aliases → provider + model id + params + capabilities (`supports_tools`, `context_window`, `supports_vision`) + optional pricing |
| `config/factory.yaml` | `default_model`, `agent_overrides`, ports, paths, limits, sandbox settings, command allowlist, budgets |
| `config/pipelines/*.yaml` | Stage graphs (§6) |
| `.env` | Provider API keys (git-ignored); `.env.example` is committed |

- Loading and validation use Pydantic v2 models in `factory/config/`.
- `LiveConfig` holds the current immutable `Settings` snapshot behind a lock and
  swaps it atomically on reload. A bad edit keeps the last good snapshot and
  publishes a `config.error` event. The UI shows this as a banner.
- `LiveConfig.refresh()` re-reads only when a config file's mtime or size changed
  (three `stat` calls). The resolver calls it before every model resolution, so an
  edit made by any process applies to the next LLM call with no watcher. The M6
  file watcher only pushes the change to the UI.
- Runtime overrides (`factory model use`, UI dropdown) write to
  `config/factory.yaml`, so the CLI, UI and file edits share one source of truth.
  The watcher picks up the write. Per-run overrides live in the run row in SQLite.

### Model resolution

`ModelResolver.resolve(agent, run)` runs on **every** LLM call:

```
per-run override  →  per-agent override (factory.yaml)  →  agent.md `model:`  →  default_model
```

It returns a `ResolvedModel` (alias, provider config, model id, params,
capabilities). Provider client instances are cached by
`(provider name, config hash)`, so a switch costs nothing and an endpoint change
builds a new client.

### Secrets

`factory.secrets` looks keys up in this order: process env → `.env` → Windows
Credential Manager via `keyring` (optional). Values are registered with a global
`Redactor`. Every log line, event and transcript passes through it before it is
written. Keys are attached only inside provider adapters. They never enter agent
context, tool environments or sandboxes.

---

## 4. Providers (M1: interface + Fake + OpenAI-compat; M6: Anthropic, Gemini)

`factory/providers/base.py`:

```python
class LLMProvider(ABC):
    def __init__(self, config: ProviderConfig, api_key: str | None, *, root: Path): ...
    @abstractmethod
    def complete(self, messages, tools=None, *, model: str, **params) -> Response: ...
    def stream(self, messages, tools=None, *, model: str, **params) -> Iterator[StreamChunk]: ...
```

One instance serves one endpoint, and the model id is chosen per call (see D6).

Normalized types: `Message(role, content, tool_calls, tool_call_id, name)`,
`ToolSpec(name, description, parameters: JSON Schema)`,
`ToolCall(id, name, arguments: dict)`,
`Usage(input_tokens, output_tokens)`,
`Response(content, tool_calls, usage, model, finish_reason, raw_latency_ms)`.

- Adapters are thin `httpx` clients with no vendor SDKs. This keeps the dependency
  surface to `httpx` plus `pydantic`.
- Providers register by `type` string through a small registry (`@register_provider("openai")`).
  Adding a provider means adding a module and referencing its type in `models.yaml`.
- **JSON tool fallback**: when `supports_tools: false`, the runtime (not the adapter)
  injects a tool-protocol instruction block. It asks the model to reply with
  `{"tool_calls":[{"name":…,"arguments":{…}}]}` or `{"final": "…"}` and parses the
  reply into the same `ToolCall` structure. Agents see no difference.
- **Fake provider**: returns scripted responses keyed by agent/stage from a YAML/JSON
  script, or echoes the last user message. It is used by every test and the e2e run.

---

## 5. Agents (M2)

An agent is `agents/<name>/agent.md`: YAML frontmatter (validated by the Pydantic
`AgentSpec`) followed by a Markdown body that becomes the system prompt.

- **Loader** scans `agents/*/agent.md` (skipping `_`-prefixed folders) at startup
  and on file change. Invalid definitions become `AgentLoadError` entries that are
  shown in the UI. They never raise into the orchestrator.
- **Context assembly** builds a *fresh* message list per invocation: system prompt
  + rendered task message (request text, gate comments, and the contents of only
  the artifacts named in `inputs`). There is no shared history between agents.
- **Runtime loop**: call LLM → execute tool calls through the registry → append
  results → repeat until a final answer, `max_iterations`, or `max_tokens_budget`.
  Declared `outputs` are written to the artifact store via the `artifact.write`
  tool or taken from the final message. An agent with one output and no
  `artifact.write` tool (always the case at L0) has its final reply saved as that
  artifact. An enclosing code fence is stripped. With several outputs, it wraps
  each in `<artifact name="…">…</artifact>`. A declared output that is still
  missing fails the invocation.
- The `agents/<name>/agent.md` file is re-read (if changed) at the start of each
  invocation, so prompt, model, tool and scope edits apply on the next invocation.
- A pipeline stage may set `model:`, which replaces the agent.md default for that
  stage. Per-agent and per-run overrides still beat it.
- Shipped agents: requirements, architect, planner, tester, implementer, reviewer,
  security, docs, and `_template`.

---

## 6. Pipelines and orchestration (M2 sequential; M4 full)

Pipelines are data (`factory/core/pipeline.py`). The interpreter in
`factory/core/orchestrator.py` walks a validated `PipelineSpec` with a persisted cursor.

**Per stage, in order:**

1. `when:` conditions. If false, the stage is `skipped`.
2. A human gate with `before: true` (the default for `builtin.merge`) asks for
   approval *before* the body runs.
3. Budget check (see below).
4. **Body.** `agent`, or `parallel` (branches run concurrently in a thread pool of
   `limits.max_parallel_agents`, and every branch must pass). Or a builtin:
   - `builtin.shell` and `builtin.lint` run a sandboxed command in a read-only
     checkout of integration, with the report in `<stage>.json`. Lint defaults to
     `ruff check .`.
   - `builtin.merge` delivers integration to the target repo as branch
     `factory/<run>`, never touching its checked-out branch. For a new project it
     tags the result. Either way it writes `delivery.json`.
5. **Automated gate.** `check` runs a sandboxed command, in the single writing
   agent's own worktree (its unmerged work) or else in integration. The result
   goes to `gate.report` (default `<stage>_check.json`). `must_pass: false` makes
   it informational. `artifact_valid` validates JSON artifacts against JSON Schema
   files (`factory/core/jsonschema_lite.py`).
6. `pass_when:` conditions on JSON artifacts, e.g. `{artifact: review.json, path:
   verdict, equals: pass}`. Operators: `equals`, `not_equals`, `in`, and `exists`.
7. A human gate (after) asks for approval of the stage's outputs.

**On failure** (`on_fail`):
- `retry: N` re-runs the stage with the failure (e.g. the test output) as feedback.
- `then: <later stage>` marks the stage failed and continues there, carrying its
  work forward into integration.
- `goto: <earlier stage>` resets that range to pending and loops back with
  feedback, at most `max_loops` times.
- With none of these, the run fails.

`then` must point forward and `goto` backward (validated), so every run terminates.

**Approvals** (`approvals` table; kinds `gate`, `budget`, `network`) pause the run
in `waiting_approval`. `Orchestrator.decide()` records `approved`, `rejected` or
`changes_requested` (with a comment), and the next `run()` applies it:
- *Rejected*: the run fails.
- *Changes requested*: the stage re-runs with the comment as feedback, alongside
  its previous output.
- *Budget approved*: that budget scope gets one more full allowance.
- *Network* approvals are polled live by the waiting L3 agent (first use per run
  only, timeout `limits.network_approval_timeout_s`).

**State:** `runs/factory.db` (SQLite) holds `runs` (including a `state` JSON with
the cursor, loop and retry counters, pending feedback, pre-approvals, budget
allowances and the network decision), `stage_runs`, `approvals` and `usage`. Older
databases are migrated in place. Every transition is committed before the next
step. On restart, a `running` stage goes back to `pending` and re-runs; finished
stages are never re-run; a run waiting for a human stays waiting.

**Budgets:** per-run and per-stage token and cost limits, from `factory.yaml` or a
pipeline/stage `budget:`. They are checked before every LLM call (through a
runtime callback) and before each stage. A breach stops the agent before its next
call and opens a `budget` approval. A writing agent's work so far is committed and
kept for the re-run.

**Event bus:** in-process synchronous pub/sub. Subscribers are the JSONL event
log, the usage recorder, the CLI printer and (M5) the SSE broadcaster. Events
added in M4: `gate.evaluated`, `stage.retry`, `stage.loop`, `stage.continue`,
`stage.skipped`, `stage.parallel`, `approval.requested`, `approval.decided`,
`budget.exceeded`, `workspace.committed`, `run.delivered`.

Run directory: `runs/<run_id>/{artifacts/, transcripts/, events.jsonl, pipeline.yaml}`.

---

## 7. Isolation (M3)

See [ISOLATION.md](ISOLATION.md) for the full threat model. In summary:

| Layer | Mechanism |
|---|---|
| Context | Fresh conversation per invocation; only declared `inputs` artifacts injected |
| Filesystem | `PermissionLayer` resolves every tool path: expand `{workspace}`/`{artifacts}`, reject absolute-outside-scope and `..` traversal, `Path.resolve()` to defeat symlink/junction escapes, then apply globs: a write needs a `write` match and no `deny` match; a read needs a `read` match and no `deny_read` match. (`deny` blocks writes only, so the implementer can read the tests it may not edit.) Every denial is logged and emitted as a `permission.decision` event with `decision: denied`. |
| Worktrees | Each run gets a private clone of the target (or a fresh `git init`) at `workspaces/<run_id>/_repo`, checked out on `factory/<run_id>/integration`. Each L1+ agent gets `workspaces/<run_id>/<agent>` on branch `factory/<run_id>/<agent>`. A writing agent's changes are committed when it finishes and merged into integration when its stage passes. Read-only agents get a fresh checkout every invocation. `builtin.merge` (M4) delivers integration back to the target repo behind a human gate. |
| Execution | Levels L0–L3 checked by the tool registry (`required_isolation_level ≤ agent level`). `shell.run_sandboxed` goes to a pluggable `SandboxBackend`: **Docker** (only the worktree mounted, `--network none`, cpu/mem/pids limits, `--user 1000:1000`, timeout) or **restricted subprocess** fallback (cwd = worktree, timeout, secret-stripped env, allowlisted executables). The fallback raises a "reduced isolation" banner. L3 network needs a human gate on first use per run. |
| Secrets | Keys only in provider adapters. Sandbox and subprocess environments are built from an explicit allowlist, not inherited. |
| Roles | Enforced through `fs_scope` in shipped agent.md files *and* hard rules in the permission layer: an agent with `role: reviewer`/`security` can never get write tools; implementer deny `tests/**`; tester deny `src/**`. |

---

## 8. Tools (M2–M3)

`@tool(name, schema, required_isolation_level)` registers a function in the
`ToolRegistry` at import time. Discovery imports every module in `factory/tools/`.
An agent sees only the tools it lists **and** whose level it meets. Built-ins:
`fs.read`, `fs.write`, `fs.list`, `git.diff`, `git.status`, `shell.run_sandboxed`,
`artifact.read`, `artifact.write`, `human.ask` (requirements clarifications).

---

## 9. UI (M5)

`factory up` serves a FastAPI app on `127.0.0.1` (port from `ui.port`, default
8765). It resumes runs a previous process left `running`, and opens the browser:
`wslview` or `explorer.exe` inside WSL, otherwise Python's `webbrowser`. The
frontend is static `index.html` / `app.js` / `app.css` with no build step.

**Code layout** (`factory/ui/`):
- `service.py` runs each run in its own background thread (`RunManager`) and builds
  the JSON views. It holds no HTTP code, so tests drive it directly.
- `app.py` contains the routes and the security middleware.
- `broadcast.py` hands bus events from any thread to SSE clients through per-client
  asyncio queues. A slow client drops events rather than blocking a run, and the
  activity log reloads from `events.jsonl` on reconnect.

**API** (all JSON; live updates on `GET /api/stream`, SSE):

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | config / agent / pipeline errors, sandbox mode, active runs, pending approvals |
| `GET /api/models`, `POST /api/models/default`, `POST /api/models/agents/{agent}` | model selector (writes `config/overrides.yaml`) |
| `GET /api/pipelines` | pipelines with stage shapes and "cannot run" reasons |
| `GET/POST /api/runs` | history and start-run form |
| `GET /api/runs/{id}` | stages (status, attempts, loops, retries, tokens), usage, approvals, artifact index, sandbox, delivery |
| `GET /api/runs/{id}/events`, `/artifacts/{name}`, `/changes`, `/diff?ref=` | activity history, artifact text, branches / per-stage commits / diffs |
| `POST /api/runs/{id}/decision`, `/resume`, `/model` | gate controls, resume, per-run model switch |

**Views:**
- Horizontal stage graph. The current stage is enlarged with a "current" or "needs
  you" tag and pulses; each stage shows gate, verdict, retry and loop badges.
- Live activity stream, filterable by agent and by kind (messages, tool calls,
  denials, stages and gates, LLM calls, allowed access).
- Approval cards (Approve / Request changes / Reject + comment) for gate, budget and
  network requests.
- Artifact browser grouped by stage; diff viewer for integration, each agent
  branch, and each stage's commits.
- Usage table per agent and per stage; run history; start-run form.
- "Agents & models": global default, per-agent overrides, and which model each
  agent's next call will use. Each run also has a run-model selector.
- Banners for config errors (the last good config stays active), invalid agents,
  invalid pipelines, and reduced isolation. Status is polled every 5 s, so file
  edits show up without a watcher.

**Security:**
- The server binds loopback only (validated in config).
- Requests need a loopback `Host` header, which blocks DNS rebinding.
- State changes need a JSON body and, if present, a loopback `Origin` (CSRF).
- All dynamic content is rendered with `textContent`, so LLM or artifact output
  can't inject markup.

---

## 10. Observability (M2, completed M5)

- `runs/<run_id>/events.jsonl`: one redacted JSON object per event (`llm.call`
  with model/tokens/latency, `tool.call`, `permission.decision`, `stage.transition`,
  `gate.*`, `config.*`).
- `runs/<run_id>/transcripts/<stage>-<agent>-<n>.json`: full redacted message list.
- CLI: `factory runs list`, `factory runs show <id>`.

---

## 11. CLI

`factory` (Typer) commands:

| Command | Milestone |
|---|---|
| `factory prompt "<text>" --model <alias>` | M1 |
| `factory models list` / `factory model use <alias> [--agent <name>]` | M1 |
| `factory run start "<request>" --pipeline default --repo <path>` | M2 |
| `factory runs list` / `factory runs show <id>` / `factory runs resume <id>` | M2 |
| `factory agents list` / `factory pipelines list` | M2 |
| `factory up` | M5 |
| `factory new agent <name>` | M6 |

---

## 12. Milestones

1. **Skeleton**: repo layout, `pyproject.toml`, config models + loader, provider
   interface, Fake + OpenAI-compatible adapters, secrets/redaction, CLI
   `prompt` / `models list` / `model use`, unit tests, setup/run scripts.
2. Agent loader + shipped agent.md files, artifact store, sequential orchestrator
   with SQLite run store, event log, transcripts, `runs` CLI.
3. Permission layer, worktree manager, sandbox backends (Docker, subprocess), tool
   registry with isolation levels, isolation tests.
4. Full pipeline interpreter: gates, parallel stages, retries/goto loops, budgets,
   resume after crash.
5. Web UI with SSE, model switcher, gate controls, artifact/diff views, `factory up`.
6. Anthropic + Gemini adapters, file-watcher hot reload, `factory new agent`, full
   docs (README, EXTENDING, ISOLATION).
7. E2E test: the default pipeline with the Fake provider builds a small CLI todo app
   from a one-line request. Optional GitHub Actions on `windows-latest`.

Each milestone ends with `pytest`, `ruff check`, and `mypy --strict factory/`
passing.
