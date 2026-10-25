# Isolation

Agents are isolated from each other and from the host by code, not by prompt
instructions. This document describes each layer, what it guarantees, and its
known limits. The tests that prove each claim are listed at the end.

## Threat model

An agent is treated as **untrusted code that a language model writes on the fly**.
It may be confused, prompt-injected by content it reads (for example a malicious
file in the target repo), or simply wrong. The factory must ensure that an agent:

- cannot read another agent's conversation, or any artifact it was not given;
- cannot read or write files outside its declared scope, nor tamper with work
  that belongs to another role (tests vs. source);
- cannot obtain provider API keys or other secrets;
- cannot run arbitrary programs on the host or reach the network, unless an L3
  agent has explicit human approval inside the Docker sandbox.

Out of scope: a malicious *user* of the factory, a compromised Docker daemon or
OS, and side channels.

## 1. Context isolation

| Guarantee | How |
|---|---|
| Fresh conversation per invocation | `factory/agents/context.py` builds `[system, task]` from scratch on every invocation. No message list is shared or reused. |
| Only declared inputs | The task message embeds only the artifacts named in the agent's `inputs`. `artifact.read` refuses any other name. `artifact.write` refuses anything not in `outputs`. |
| Artifacts are the only channel | Agents have no messaging tool. Everything flows through `runs/<run>/artifacts/`, whose names are validated (no `..`, no absolute paths, no backslashes). |

## 2. Filesystem isolation

### Per-run repository and per-agent worktrees

`factory/isolation/worktrees.py`:

```
workspaces/<run>/_repo/       private clone of --repo (or a fresh git init)
workspaces/<run>/<agent>/     git worktree on branch factory/<run>/<agent>
```

- The target repository is **cloned**, never modified. The run's work lives on
  `factory/<run>/integration` inside the clone. Delivering it back is
  `builtin.merge` (milestone 4), behind a human gate.
- Each L1+ agent works in its own worktree. Writing agents (L2+, and not a
  reviewer or security role) keep their worktree across invocations. When an
  invocation ends, the orchestrator commits its changes. If the stage passed, it
  merges the branch into integration. The next agent's worktree starts from
  integration.
- Read-only agents get a forced checkout of integration plus `git clean` on every
  invocation, so nothing they (or a tool) leave behind persists.
- All git commands run with `core.autocrlf=false`, `commit.gpgsign=false` and
  `--no-verify`. Clones never copy hooks, so no repository hooks run.

### Permission layer

Every file tool resolves paths through `PathPolicy` (`factory/isolation/permissions.py`).
A path must pass all of these checks, in order:

1. **Syntax:** not empty; no NUL; no `..` component; no UNC or `\\?\` paths; no
   `:` in a component (NTFS alternate data streams); no reserved device names
   (`CON`, `NUL`, `COM1`, …); no component ending in `.` or a space (Windows
   strips those, so `tests./x` would alias `tests/x`); no drive-relative (`C:x`)
   or rooted (`\x`) paths. Relative paths are resolved against the workspace.
2. **Containment:** after `Path.resolve()`, which follows symlinks and NTFS
   junctions, the path must be inside the workspace or the run's artifact
   directory. This defeats symlink and junction escapes.
3. **Always denied:** anything inside a `.git` directory, and `.env` / `.env.*` files.
4. **Hard links:** an existing file with more than one link is refused, because it
   could alias a file outside the scope.
5. **Scope globs** (`fs_scope` in `agent.md`; `{workspace}` / `{artifacts}`
   placeholders; patterns without a placeholder are workspace-relative):
   - read: `deny_read` match → denied; otherwise a `read` match is required.
   - write: `deny` match → denied; otherwise a `write` match is required.
   - Deny is checked first and always wins. `deny` blocks writes only, so the
     implementer can *read* the tests it must not *edit*.
   - Matching is case-insensitive on Windows (NTFS is), so `TESTS/x.py` cannot
     slip past `deny: tests/**`.

Every decision (allowed and denied) is emitted as a `permission.decision` event,
written to `runs/<run>/events.jsonl`, printed by the CLI (denials) and shown in
the UI (milestone 5). The model receives `ERROR: permission denied: <reason>` and
can carry on.

### Role separation

| Role | Enforced by |
|---|---|
| reviewer, security: cannot write | The schema rejects write tools or `fs_scope.write` for these roles. The runtime refuses them again at invocation. Their worktree is reset every time and never committed. |
| implementer: cannot modify tests | `deny: ["{workspace}/tests/**"]`. |
| tester: cannot modify `src/` | `deny: ["{workspace}/src/**"]`. |
| docs: only README and docs | `write: [README.md, docs/**]`, `deny: [src/**, tests/**]`. |

## 3. Execution isolation

### Isolation levels

The tool registry only offers an agent the tools it lists **and** whose
`required_isolation_level` is at or below the agent's level. An agent that lists
a tool above its level fails to start, with a clear error.

| Level | Tools | Workspace |
|---|---|---|
| L0 | none; the final reply is the artifact | none |
| L1 | `fs.read`, `fs.list`, `git.diff`, `git.status`, `artifact.*` | read-only worktree |
| L2 | + `fs.write`, `shell.run_sandboxed` | writable worktree |
| L3 | + `network: true` on `shell.run_sandboxed` | writable worktree |

L3 networked commands need human approval on first use per run. The approval gate
arrives in milestone 4. Until then, networked commands are always denied. They also
require the Docker backend.

### Sandbox backends (`factory/isolation/sandboxes/`)

`sandbox.backend: auto` (the default) uses Docker when the daemon is running and
the image is built (`factory sandbox build`). Otherwise it falls back to the
restricted subprocess backend and announces **REDUCED ISOLATION** through an
`isolation.status` event, a CLI banner, and the UI banner (milestone 5). Setting
`backend: docker` makes Docker mandatory: stages that need a sandbox fail instead
of falling back.

**Docker** (`docker_backend.py`). Every command runs in a fresh
`software-factory-sandbox:py312` container with:

- only the agent's worktree mounted, at `/workspace`;
- `--network none` (unless approved for L3), `--cpus`, `--memory`, `--pids-limit`;
- a non-root user: the host user's uid:gid on Linux/WSL, so files the agent
  writes stay owned by you; `1000:1000` elsewhere; never root;
- `--cap-drop ALL`, `no-new-privileges`, and a read-only root filesystem with a
  `/tmp` tmpfs;
- the container killed on timeout;
- no host environment variables: only the explicit `--env` values are passed;
- Docker's own host warnings (for example about swap accounting) filtered out of
  the output the agent sees.

*Memory on WSL2 without systemd.* There, Docker uses the `cgroupfs` driver and
cannot limit swap (`docker info` reports `SwapLimit: false`). `--memory` still caps
RAM, but a runaway process would spill into WSL's swap instead of being killed. In
that case the sandbox also runs `ulimit -v <memory>`, which caps each process's
address space, and `--pids-limit` bounds the number of processes. Enabling systemd
in `/etc/wsl.conf` (`[boot]` / `systemd=true`) gives Docker full cgroup v2 limits.
`factory sandbox status` and `scripts/setup.sh` report which mode is active.

Verified on WSL2 (Ubuntu 22.04, Docker 29) by the real-Docker tests in
`tests/test_sandbox.py`:
- no network and no DNS;
- uid matches the host user, and the host still owns files the agent writes;
- read-only root and zero capabilities;
- no host secrets or Windows drives visible;
- a 600 MB allocation under a 256 MB limit fails;
- timeouts leave no containers behind;
- `pytest` runs inside the sandbox.

**Restricted subprocess** (`subprocess_backend.py`). This is a fallback, and it
does *not* provide full isolation:

- no shell: the command is split into argv, so `&&`, `|` and `>` are literal arguments;
- the executable must be a bare name on `sandbox.command_allowlist` (default
  `python, pytest, ruff, mypy`) and must resolve to a real executable, not a
  `.bat`/`.cmd` script;
- the environment is rebuilt from a small allowlist (`PATH`, `SYSTEMROOT`, …),
  never inherited, and any value equal to a known secret is dropped. `HOME` and
  `TEMP` point at a throwaway directory;
- the working directory is the worktree; output is capped; the whole process tree
  is killed on timeout (`taskkill /T`);
- **not enforced:** network access, and an allowed interpreter reading files
  outside the worktree. Hence the banner.

To add a backend (Windows Sandbox, WSL-native), subclass `SandboxBackend` in a new
module in that package, decorate it with `@register_sandbox("name")`, and set
`sandbox.backend: name`.

## 4. Secret isolation

- Provider keys are read only by `SecretStore` and handed only to provider
  adapters, inside the orchestrator process. Agents are not processes. They never
  see a key in their context, tools or environment.
- Every value `SecretStore` returns is registered with the global `Redactor`. All
  events, transcripts and provider error messages pass through it before they are
  written.
- Sandboxes build their environment from an allowlist (subprocess) or pass none
  (Docker), so keys in the parent environment cannot leak into commands.

## Tests

| Claim | Test |
|---|---|
| Traversal, ADS, device names, trailing dots, UNC, NUL, drive-relative paths | `tests/test_permissions.py::test_denied_paths`, `test_drive_relative_and_rooted_denied` |
| Symlink, junction and hard-link escapes | `test_symlink_escape_denied` (skipped without symlink privilege), `test_junction_escape_denied`, `test_hard_link_denied` |
| Deny over allow; case-insensitive deny | `test_deny_beats_overlapping_allow`, `test_case_variants_cannot_bypass_deny` |
| Worktrees per agent, target untouched, read-only reset, conflicts | `tests/test_worktrees.py` |
| Sandbox: cwd, env without secrets, allowlist, no shell, timeout, network | `tests/test_sandbox.py` |
| Docker flags; real no-network check (when Docker is up) | `test_docker_command_is_locked_down`, `test_docker_really_has_no_network` |
| End to end: blocked, logged, attributed denials; tests run in the sandbox; only approved work lands | `tests/test_isolation_e2e.py` |
| Fresh context; only declared inputs; redacted transcripts | `tests/test_runtime.py` |
