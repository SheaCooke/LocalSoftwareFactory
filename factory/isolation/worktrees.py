"""Per-run Git repository and per-agent worktrees.

Layout for run R:

    workspaces/R/_repo/      private clone of the target repo (or a fresh `git init`),
                             kept checked out on the integration branch
    workspaces/R/<agent>/    that agent's worktree, on branch factory/R/<agent>

Refs:  factory/R/base (tag: the starting commit), factory/R/integration (approved
work), factory/R/<agent> (one per agent).

Writing agents keep their worktree across invocations. The orchestrator commits
whatever they changed when the invocation ends, merges the branch into
integration when the stage passes, and merges integration back in before the next
invocation. Read-only agents get a fresh checkout of integration every time.
The target repository itself is never modified here. Delivering the integration
branch back to it is `builtin.merge` (milestone 4).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

REPO_DIR = "_repo"
_GIT_IDENTITY = ["-c", "user.name=Software Factory", "-c", "user.email=factory@localhost"]
_GIT_SAFE = [
    "-c",
    "core.longpaths=true",
    "-c",
    "core.autocrlf=false",
    "-c",
    "commit.gpgsign=false",
]  # Hooks: clones never copy them and commits use --no-verify.


class GitError(RuntimeError):
    pass


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(args: list[str], cwd: Path, *, check: bool = True, timeout: float = 120) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(
            ["git", *_GIT_SAFE, *_GIT_IDENTITY, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


class RunRepo:
    def __init__(self, workspaces_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self.root = workspaces_dir / run_id
        self.repo = self.root / REPO_DIR
        self.base_ref = f"factory/{run_id}/base"
        self.integration = f"factory/{run_id}/integration"
        self._lock = threading.Lock()

    def branch(self, agent: str) -> str:
        return f"factory/{self.run_id}/{agent}"

    def worktree_path(self, agent: str) -> Path:
        return self.root / agent

    @property
    def exists(self) -> bool:
        return (self.repo / ".git").exists()

    # ------------------------------------------------------------------------- setup

    def ensure(self, source: Path | None) -> None:
        """Create the run repo once: clone `source` (HEAD) or start an empty project."""
        with self._lock:
            if self.exists:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            if source is not None:
                source = source.resolve()
                if not source.is_dir():
                    raise GitError(f"target repo {source} does not exist")
                try:
                    run_git(["rev-parse", "--verify", "HEAD"], source)
                except GitError as exc:
                    raise GitError(
                        f"{source} is not a git repository with at least one commit"
                    ) from exc
                run_git(
                    ["clone", "--quiet", "--no-hardlinks", str(source), str(self.repo)], self.root
                )
            else:
                self.repo.mkdir(parents=True)
                run_git(["init", "--quiet", "-b", "main"], self.repo)
                run_git(
                    ["commit", "--quiet", "--allow-empty", "-m", "factory: new project"], self.repo
                )
            run_git(["tag", self.base_ref], self.repo)
            run_git(["checkout", "--quiet", "-b", self.integration], self.repo)

    # --------------------------------------------------------------------- worktrees

    def prepare(self, agent: str, *, writable: bool) -> Path:
        """Return the agent's worktree, up to date with integration."""
        with self._lock:
            path = self.worktree_path(agent)
            branch = self.branch(agent)
            if not (path / ".git").exists():
                if path.exists():
                    shutil.rmtree(path)
                run_git(["worktree", "prune"], self.repo)
                run_git(
                    ["worktree", "add", "--quiet", "-B", branch, str(path), self.integration],
                    self.repo,
                )
            elif writable:
                self._commit(path, f"factory: {agent} leftovers")
                out = run_git(
                    ["merge", "--no-edit", "--quiet", self.integration], path, check=False
                )
                if self._has_conflicts(path):
                    run_git(["merge", "--abort"], path, check=False)
                    raise GitError(f"cannot bring {agent}'s worktree up to date: {out.strip()}")
            else:
                run_git(["checkout", "--quiet", "--force", "-B", branch, self.integration], path)
                run_git(["clean", "-fdq"], path)
            return path

    def commit(self, agent: str, message: str) -> str | None:
        """Commit everything changed in the agent's worktree. Returns the sha or None."""
        with self._lock:
            return self._commit(self.worktree_path(agent), message)

    def merge_to_integration(self, agent: str, message: str | None = None) -> bool:
        """Merge the agent's branch into integration. Returns False if nothing to merge."""
        with self._lock:
            branch = self.branch(agent)
            ahead = run_git(["rev-list", "--count", f"{self.integration}..{branch}"], self.repo)
            if int(ahead.strip() or 0) == 0:
                return False
            msg = message or f"factory: merge {agent}"
            proc = run_git(
                ["merge", "--no-ff", "--no-edit", "-m", msg, branch], self.repo, check=False
            )
            if self._has_conflicts(self.repo):
                run_git(["merge", "--abort"], self.repo, check=False)
                raise GitError(f"merging {agent} into integration conflicts: {proc.strip()}")
            return True

    def diff(self, path: Path) -> str:
        """Everything changed in a worktree since the run started, including new files."""
        run_git(["add", "--all", "--intent-to-add"], path)
        return run_git(["diff", "--stat", "--patch", self.base_ref], path)

    def status(self, path: Path) -> str:
        return run_git(["status", "--short", "--branch"], path)

    def files_on_integration(self) -> list[str]:
        return run_git(["ls-tree", "-r", "--name-only", self.integration], self.repo).splitlines()

    def remove_worktrees(self) -> None:
        with self._lock:
            for line in run_git(["worktree", "list", "--porcelain"], self.repo).splitlines():
                if line.startswith("worktree "):
                    wt = Path(line[len("worktree ") :])
                    if wt.resolve() != self.repo.resolve():
                        run_git(["worktree", "remove", "--force", str(wt)], self.repo, check=False)
            run_git(["worktree", "prune"], self.repo, check=False)

    # ---------------------------------------------------------------------- internals

    @staticmethod
    def _commit(path: Path, message: str) -> str | None:
        run_git(["add", "--all"], path)
        if not run_git(["status", "--porcelain"], path).strip():
            return None
        run_git(["commit", "--quiet", "--no-verify", "-m", message], path)
        return run_git(["rev-parse", "HEAD"], path).strip()

    @staticmethod
    def _has_conflicts(path: Path) -> bool:
        return bool(run_git(["diff", "--name-only", "--diff-filter=U"], path, check=False).strip())
