# Extending the factory

Every extension below works **without touching orchestrator code**. Agents,
pipelines and config are data. Tools, providers and sandbox backends are plugins,
found by scanning their package. Each example is exercised by a test in
`tests/test_extending.py`.

| You want to… | Edit | Takes effect |
|---|---|---|
| [Add an agent](#1-add-an-agent) | `agents/<name>/agent.md` (`factory new agent`) | immediately (hot reload; shows in the UI) |
| [Modify an agent](#2-modify-an-agent) | its `agent.md` | that agent's next invocation |
| [Add or reorder stages](#3-add-or-reorder-stages) | `config/pipelines/*.yaml` | new runs (running runs keep their snapshot) |
| [Switch models](#4-switch-models) | UI, `factory model use`, or `config/*.yaml` | the next LLM call |
| [Add a tool](#5-add-a-tool) | a module in `factory/tools/` | next start of the factory process |
| [Add a provider](#6-add-a-provider) | a module in `factory/providers/` + `models.yaml` | next start (the alias itself hot-reloads) |
| [Add a sandbox backend](#7-add-a-sandbox-backend) | a module in `factory/isolation/sandboxes/` | next start |

`factory up` watches `config/` and `agents/` and pushes changes to open browsers.
Invalid edits never crash anything. The last good config stays active, an invalid
agent or pipeline is skipped, and the UI shows a banner saying why.

---

## 1. Add an agent

**Worked example: a `changelog` agent that turns the implementation summary into a
CHANGELOG entry.**

```bash
uv run factory new agent changelog -d "Writes a CHANGELOG entry for the change" -m fast
```

This copies `agents/_template/agent.md` to `agents/changelog/agent.md`, fills in
the name, description and model, and validates the result. Now edit it:

```markdown
---
name: changelog
description: Writes a CHANGELOG entry for the change
model: fast
isolation_level: L0            # text in, artifact out: needs no tools
inputs: [requirements.md, implementation_summary.md]
outputs: [changelog_entry.md]
max_iterations: 3
max_tokens_budget: 40000
---
# Role
You are the Release Writer. You write one CHANGELOG entry for the change described
in `implementation_summary.md`, for end users, not developers.

# Output
Reply with only the entry, in Keep a Changelog format:
### Added / Changed / Fixed
- one line per user-visible change, present tense, no internal names
```

Rules the loader enforces (errors appear in `factory agents list` and as a UI banner):

- The folder name must equal `name`. Folders starting with `_` are ignored.
- `isolation_level` sets the ceiling for `tools`: L0 lists none; L1 adds read-only
  file tools; L2 adds `fs.write` and `shell.run_sandboxed`; L3 adds networked commands.
- `inputs` and `outputs` are artifact names. An L0 agent's final reply is saved
  as its single output, or use `<artifact name="…">` blocks for several. Agents
  with `artifact.write` write their outputs explicitly.
- `role: reviewer` and `role: security` are read-only. The schema rejects write
  tools for them.
- `fs_scope` globs use `{workspace}` (the agent's git worktree) and `{artifacts}`.
  `deny` beats `write`, and `deny_read` beats `read`. See [ISOLATION.md](ISOLATION.md).

The agent does nothing until a pipeline uses it (next section).

## 2. Modify an agent

Edit `agents/<name>/agent.md`: the prompt (Markdown body), `model`, `tools`,
`fs_scope`, `inputs`/`outputs` or limits. The orchestrator re-reads changed agent
files at the start of every invocation, so the edit applies to that agent's next
turn, including in a run that is already going. If you break the file, that agent's
stages fail with the validation message until you fix it, and the UI shows a banner.

Example: let the implementer also maintain `pyproject.toml` dependencies:

```yaml
fs_scope:
  read: ["{workspace}/**"]
  write: ["{workspace}/src/**", "{workspace}/pyproject.toml"]
  deny: ["{workspace}/tests/**"]
```

## 3. Add or reorder stages

Pipelines are YAML in `config/pipelines/`. Start from a copy:

```bash
uv run factory new pipeline release --from default
```

**Worked example: run the `changelog` agent after `docs`, but only when an
implementation summary exists, and lint before integrating.**

```yaml
  - id: docs
    agent: docs
  - id: changelog                       # new stage
    agent: changelog
    when: { exists: implementation_summary.md }
    gate: human_approval
  - id: lint                            # builtin stage, runs in the sandbox
    type: builtin.lint                  # default command: ruff check .
    on_fail: { goto: implement, max_loops: 1 }
  - id: integrate
    type: builtin.merge
    gate: human_approval
```

To reorder, move stage blocks. To run agents side by side, use
`parallel: [{agent: a}, {agent: b}]`. Every pipeline file is validated on load:
- ids must be unique;
- `on_fail.then` must point forward and `goto` backward;
- gates need their fields.

Errors show in `factory pipelines list` and the UI. The full stage grammar is in
[ARCHITECTURE.md §6](ARCHITECTURE.md#6-pipelines-and-orchestration-m2-sequential-m4-full)
and `factory/core/pipeline.py`.

Gates and conditions at a glance:

```yaml
gate: human_approval                                   # Approve / Request changes / Reject
gate: { type: check, command: "pytest -q", report: test_report.json }
gate: { type: artifact_valid, artifact: review.json, schema: agents/reviewer/review.schema.json }
pass_when: { artifact: review.json, path: verdict, equals: pass }
when: { exists: design.md }
on_fail: { retry: 2, then: review }                     # or { goto: implement, max_loops: 2 }
budget: { tokens: 200000, cost_usd: 3 }                 # per stage (or top-level per run)
```

A run snapshots its pipeline when it starts, so editing a pipeline never changes a
run that is in flight.

## 4. Switch models

Agents name **aliases** from `config/models.yaml`, never raw model ids. Priority,
highest first:
1. the run's own model (start form / `--model` / run header in the UI);
2. per-agent override;
3. the agent's `model:`;
4. the global default.

```bash
uv run factory model use local-coder                   # global default
uv run factory model use claude --agent implementer    # one agent
uv run factory model reset --all
```

All of these apply to the next LLM call, even mid-run. Runtime choices go to
`config/overrides.yaml`, and your `factory.yaml` comments stay intact.

**Worked example: add a local model served by LM Studio.**

```yaml
models:
  qwen-local:
    provider: lmstudio            # defined under providers: (OpenAI-compatible)
    model: qwen2.5-coder-32b-instruct
    supports_tools: false         # use the JSON tool protocol instead of native calls
    context_window: 32768
```

## 5. Add a tool

Drop a module into `factory/tools/`. Every module there is imported at startup, and
the `@tool` decorator registers it.

**Worked example: `text.word_count`.**

```python
# factory/tools/text.py
from typing import Any

from factory.tools.registry import ToolContext, ToolError, tool


@tool(
    "text.word_count",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File in your workspace"}},
        "required": ["path"],
    },
    required_isolation_level="L1",
)
def word_count(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Count the words in a text file in your workspace."""
    path = ctx.check_path("text.word_count", args["path"], "read")  # permission layer
    if not path.is_file():
        raise ToolError(f"{args['path']!r} is not a file")
    return str(len(path.read_text("utf-8", errors="replace").split()))
```

Then list it in an agent's `tools:`. Tool contract:
- The docstring is the description the model sees.
- The JSON Schema's `required` keys and top-level types are checked before your
  function runs.
- Raise `ToolError` for expected failures. The message goes back to the model.
- Always resolve paths through `ctx.check_path` (and run commands through
  `ctx.sandbox`), so the agent's `fs_scope` and isolation level apply. Denials are
  logged for you.
- `required_isolation_level` is enforced: an agent below it can't load the tool.

## 6. Add a provider

Implement `LLMProvider` (`factory/providers/base.py`) in a module in
`factory/providers/`, register it with a type name, and reference that type in
`models.yaml`.

**Worked example: a provider for a hypothetical in-house HTTP API.**

```python
# factory/providers/inhouse.py
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from factory.config.schema import ProviderConfig
from factory.providers import base
from factory.providers.base import LLMProvider, Message, Response, ToolSpec, Usage
from factory.providers.http import post_json
from factory.providers.registry import register_provider


@register_provider("inhouse")
class InhouseProvider(LLMProvider):
    def __init__(self, config: ProviderConfig, api_key: str | None = None, *, root: Path) -> None:
        super().__init__(config, api_key, root=root)
        self._client = base.http_client(config)  # trusts the OS certificate store

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Response:
        params.pop("metadata", None)  # never send factory metadata
        started = time.perf_counter()
        data = post_json(  # retries 429/5xx with backoff
            self._client,
            f"{self.config.base_url}/generate",
            body={"model": model, "prompt": [m.content for m in messages], **params},
            headers={"Authorization": f"Bearer {self.api_key}"},
            params=None,
            max_retries=self.config.max_retries,
            label="inhouse",
        )
        return Response(
            content=data["text"],
            usage=Usage(data["tokens_in"], data["tokens_out"]),
            model=model,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
```

```yaml
# config/models.yaml
providers:
  inhouse:
    type: inhouse
    base_url: https://llm.internal.example
    api_key_env: INHOUSE_API_KEY
models:
  inhouse-large:
    provider: inhouse
    model: large-2
    supports_tools: false        # no native tools: the factory falls back to the JSON protocol
```

Adapter checklist:
- Map neutral params (`max_tokens`, `stop`, …) and pop `metadata`.
- Normalize usage into `Usage`, counting cache tokens if the API reports them.
- Map tool names through `ToolNameCodec` (`fs.read` ↔ `fs__read`).
- Return provider-native turns in `Response.native` if the API needs them echoed back.
- Implement `stream()` (optional; the default wraps `complete()`).

Keys come from env, `.env` or the keychain and are redacted everywhere
automatically. The bundled `openai_compat.py`, `anthropic.py` and `gemini.py` are
the reference implementations.

## 7. Add a sandbox backend

Subclass `SandboxBackend` in `factory/isolation/sandboxes/`, decorate it with
`@register_sandbox("name")`, implement `availability()` and `run()`, and set
`sandbox.backend: name` in `factory.yaml`. Set `reduced_isolation = True` if it
can't enforce no-network and filesystem confinement, and the UI will say so.
`docker_backend.py` and `subprocess_backend.py` are the examples. Candidates
include Windows Sandbox and WSL-native namespaces.
