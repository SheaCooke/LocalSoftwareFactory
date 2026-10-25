# Decisions

Choices the build prompt left open, newest last. Each entry: decision, why, and
what would make us revisit it.

### D1. The working directory is the repository root
The prompt shows a `software-factory/` folder. The repo is this folder itself, and
the package name is `software-factory`. Nesting another folder would add nothing.

### D2. Config lives in `factory/config/`, not `factory/core/`
Providers, CLI, UI and orchestrator all depend on config, so config sits below
`core` in the import graph. Putting it in `core` would create import cycles.

### D3. Runtime model switches go to `config/overrides.yaml`
The UI dropdown and `factory model use` write a separate, machine-managed file
instead of rewriting `factory.yaml`. PyYAML drops comments on round-trip, and a
comment-preserving YAML library adds a dependency. Keeping `factory.yaml`
human-owned means user comments are never lost. Within each precedence tier, the
overrides file beats `factory.yaml`. `factory model reset` clears it.
`factory models list` shows the effective result. The file is git-ignored because
it is local state.

### D4. The default model is the offline `echo` alias
The factory runs end to end with no keys or network out of the box. Users pick a
real alias with `factory model use` or by editing `default_model`.

### D5. Providers are synchronous; the orchestrator will call them via threads
Adapters use a sync `httpx.Client`. The async orchestrator/UI (milestones 2 and 5)
run calls with `asyncio.to_thread`. Sync adapters are simpler to write and test,
and LLM latency outweighs thread overhead. Revisit if we need hundreds of
concurrent calls.

### D6. `model` is an explicit keyword argument on `LLMProvider.complete/stream`
One adapter instance serves one endpoint. The model id is chosen per call, so the
resolver can switch models without rebuilding clients. Clients are cached per
provider config and key fingerprint and rebuilt automatically when either changes.

### D7. No vendor SDKs
Every adapter uses plain `httpx`. Runtime dependencies are only `httpx`,
`pydantic`, `pyyaml`, `python-dotenv` and `typer` (plus optional `keyring`).

### D8. Tool names are encoded on the wire
Factory tools use dotted names (`fs.read`). OpenAI, Anthropic and Gemini only allow
`[A-Za-z0-9_-]`, so adapters send `fs__read` and decode replies through
`ToolNameCodec`.

### D9. JSON tool fallback lives in the resolver, not in adapters
For `supports_tools: false` aliases, the resolver rewrites the conversation
(`factory/providers/tool_protocol.py`) and parses JSON replies into `ToolCall`s.
This works for every adapter, and agents cannot tell the difference.

### D10. Fake provider scripts are matched, not just sequenced
Script entries can match on the calling agent (`metadata.agent`) and a substring
of the last user/tool message. Parallel stages (reviewer and security) can then
interleave in any order and still get the right canned replies in the e2e test.

### D11. uv uses the Windows certificate store
`setup.ps1` and `run.ps1` set `UV_SYSTEM_CERTS=true`. Corporate proxies and
antivirus TLS inspection otherwise make `uv sync` fail with `UnknownIssuer`.

### D12. The orchestrator is synchronous; concurrency will come from threads
The run loop, agent runtime and tools are plain sync code. Parallel stages (M4)
will use a thread pool, and the UI (M5) will run the orchestrator in a background
thread. Each piece stays easy to test and to reason about under crash or resume.

### D13. Config hot reload works by stat on use, not only by a file watcher
`LiveConfig.refresh()` checks the config files' mtime and size before each model
resolution. Edits from another process (`factory model use` during a run, a text
editor) apply to the next call, with no watcher thread and no Windows file-event
quirks. The M6 watcher only pushes changes to the UI.

### D14. L0 agents produce artifacts through their final reply
The spec says "L0: no tools". So an L0 agent cannot call `artifact.write`. Its final
reply becomes its single output, or `<artifact name="…">` blocks cover several.
`artifact.read/write/list` require L1. Tool-less models get the same convention.

### D15. `outputs` lists artifacts only; workspace files are governed by `fs_scope`
Outputs must be plain artifact names (no globs). The tester's `tests/**` and the
docs agent's `docs/**` are workspace writes, scoped by `fs_scope.write`. Each of
those agents also writes a summary artifact (`test_plan.md`, `docs_summary.md`).

### D16. `deny` blocks writes; `deny_read` blocks reads
The spec's implementer example is `deny: tests/**` ("cannot edit the tester's
tests"), but the implementer must *read* the tests to make them pass. So `deny`
beats `write` only, and a separate `deny_read` beats `read`. The M3 permission
layer also denies `.git/**` and `.env` reads unconditionally.

### D17. Each run snapshots its pipeline
`runs/<id>/pipeline.yaml` is copied at creation. Editing a pipeline affects new
runs only, so a resumed run never sees a different stage graph from the one it
started with.

### D18. Milestone 2 rejects pipeline features it cannot execute yet
Gates, parallel and builtin stages, and `on_fail` fail at `run start` with a list
of what is unsupported. They do not run partially. The `quick` pipeline
(requirements → design → plan) exercises the orchestrator meanwhile.

### D19. HTTPS uses the OS certificate store (`truststore`)
Python's `ssl` with certifi failed on this machine (`CERTIFICATE_VERIFY_FAILED`)
because a proxy or antivirus re-signs TLS. Adapters build their `httpx` client with
`truststore.SSLContext`, which uses the Windows certificate store, the same store
`UV_SYSTEM_CERTS` uses (D11). `options.verify_tls: false` exists for local
self-signed servers only.

### D20. A missing provider key fails before the request
If a provider declares `api_key_env` and no key is found, the resolver raises a
clear "model X needs OPENAI_API_KEY …" error instead of sending an
unauthenticated request.

### D21. Each run works in a private clone of the target repository
"Each run gets a Git repository for the target project" is implemented as a clone
(`workspaces/<run>/_repo`) of the target's committed `HEAD` (or a fresh
`git init` for a new project). Agent worktrees and factory branches therefore never
appear in the user's repo, and uncommitted changes there are ignored.
`builtin.merge` (M4) is the single, human-gated point where work flows back.

### D22. Stage-level merge into integration; final delivery is separate
When a writing agent's stage passes, its branch is merged into the run's
integration branch, so later agents (implementer after tester) see the work. This
"orchestrator merges approved changes" step is automatic for passed stages. The
human approval in the default pipeline sits on the final `integrate` stage. A
failed stage's commits stay on the agent's branch and are not merged.

### D23. Read-only agents get a worktree, reset every invocation
Reviewers need the code on disk to read. They get their own worktree, force-checked
out to integration and `git clean`ed every time, and it is never committed.

### D24. Path checks reject Windows aliasing tricks, not just `..`
NTFS lets several spellings reach the same file (`tests.`, `TESTS`, `a.py:stream`,
`CON`, short 8.3 names, junctions, hard links). The permission layer rejects the
syntactic forms outright, matches globs case-insensitively on Windows, follows
links via `Path.resolve()` before the containment check, and refuses multi-link
files. Short names are expanded by `resolve()` for existing paths.

### D25. Deny globs are evaluated before allow globs
`deny` / `deny_read` are checked first, so the reason given is always the deny
rule when one matches ("deny-over-allow").

### D26. The subprocess fallback runs argv, not a shell, with a tiny allowlist
Without Docker, commands are `shlex`-split and executed directly. The default
allowlist is `python, pytest, ruff, mypy`. Shells, `git`, `pip` and `uv` are left
out because they can reach the network or escape the worktree. Windows `.bat`/`.cmd`
resolutions are refused, because they are run through `cmd.exe` and its argument
parsing is injectable. Even so, an allowed interpreter can read outside the
worktree, which is why this mode is labelled REDUCED ISOLATION.

### D27. A bundled sandbox image instead of `python:3.12-slim`
Running `pytest` inside a `--network none` container requires pytest to be
installed already. `factory sandbox build` builds `software-factory-sandbox:py312`
(python 3.12 + pytest/ruff/mypy, uid 1000) from
`factory/isolation/sandboxes/Dockerfile`. `setup.ps1` builds it when Docker is
running. `auto` mode falls back to subprocess until the image exists.

### D28. L3 network approval is denied until the gate exists
`shell.run_sandboxed(network=true)` requires an L3 agent, the Docker backend, and an
approval callback. The orchestrator supplies no callback until milestone 4's human
gate, so networked commands are currently always denied (and logged).

### D29. The subprocess sandbox uses the factory's own Python first
The fallback sandbox prepends the factory interpreter's directory to `PATH`, and
`pytest` is a runtime dependency. Without this, `python -m pytest` resolved to
whatever Python came first on the user's `PATH` (on this machine, a global 3.14
without pytest), so results depended on how the factory was launched. In Docker
mode the image provides pytest (D27).

### D30. WSL2 is a first-class (and recommended) environment
The project now runs from the Linux filesystem in WSL2, alongside native Windows.
`scripts/setup.sh` and `scripts/run.sh` mirror the PowerShell scripts.
`.python-version` pins 3.12, so uv installs a managed interpreter instead of using
the distro's Python 3.10. `.gitattributes` keeps `*.sh` and the Dockerfile LF.
WSL gives the Docker sandbox a native Docker Engine and makes git much faster.
Everything Windows-specific in the permission layer still applies on native Windows.

### D31. The Docker sandbox runs as the host uid on Linux
A hard-coded `1000:1000` only works when the host user happens to be uid 1000. On
Linux/WSL the container runs as `os.getuid():os.getgid()`, so files written
through the bind mount stay owned by the user. It never runs as root: uid 0 falls
back to 1000. Docker Desktop on Windows maps ownership itself.

### D32. Per-process memory cap when Docker cannot limit swap
WSL2 without systemd runs Docker with the `cgroupfs` driver and no swap
accounting. A 600 MB allocation under `--memory 256m` succeeded by spilling into
swap. When `docker info` reports `SwapLimit: false`, the command is prefixed with
`ulimit -v <memory>`, and the same allocation now fails. Enabling systemd in WSL
removes the need for it. The daemon's swap warning is stripped from command output.

### D33. Moving the project between machines
`runs/` and `workspaces/` are portable, but agent worktrees record absolute paths.
After a move, repair them with `git -C workspaces/<run>/_repo worktree repair
<worktree paths>`. This was done for the runs carried over from Windows.
`core.filemode` is now `true` in this checkout, so the executable bit on
`scripts/*.sh` is tracked.

### D34. `factory up` from WSL (plan for milestone 5)
The UI binds to `127.0.0.1:8765` inside WSL. WSL2's localhost forwarding makes it
reachable from the Windows browser at the same address. To open the browser from
WSL, `factory up` will try `wslview`, then `explorer.exe`, then Python's
`webbrowser`, and always print the URL.
