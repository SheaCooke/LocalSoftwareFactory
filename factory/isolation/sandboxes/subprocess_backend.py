"""Restricted-subprocess fallback used when Docker is unavailable (REDUCED ISOLATION).

What it enforces: the working directory is the agent's worktree; no shell is
involved (the command is split into argv); the executable must be a bare name on
the `sandbox.command_allowlist` and must resolve to a real executable (not a
.bat/.cmd script); the environment is rebuilt from a small allowlist, so provider
keys are never inherited; HOME/TEMP point at a throwaway directory; output is
capped; and the whole process tree is killed on timeout.

What it cannot enforce: network isolation, or stopping an allowed interpreter
(e.g. `python`) from reading files outside the worktree. That is why the UI shows
a "reduced isolation" banner while this backend is active.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar

from factory.config.schema import SandboxConfig
from factory.isolation.sandboxes.base import (
    SandboxBackend,
    SandboxError,
    SandboxResult,
    cap,
    register_sandbox,
)
from factory.secrets import redactor

_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "LANG",
    "LC_ALL",
    "TZ",
)
_SCRIPT_SUFFIXES = (".bat", ".cmd", ".ps1", ".vbs", ".js", ".wsf")


def sandbox_env(home: Path) -> dict[str, str]:
    """A minimal environment: allowlisted variables only, never secrets."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env = {k: v for k, v in env.items() if redactor.redact(v) == v}  # paranoia: no secret values
    # `python` / `pytest` resolve to the factory's own environment first, regardless of
    # how the factory was launched (uv run, an activated venv, or the venv's python.exe).
    tools_dir = str(Path(sys.executable).parent)
    env["PATH"] = os.pathsep.join([tools_dir, env.get("PATH", "")])
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TEMP": str(home),
            "TMP": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "NO_COLOR": "1",
        }
    )
    return env


@register_sandbox("subprocess")
class SubprocessSandbox(SandboxBackend):
    reduced_isolation: ClassVar[bool] = True

    @classmethod
    def availability(cls, config: SandboxConfig) -> str | None:
        return None if config.command_allowlist else "sandbox.command_allowlist is empty"

    def parse(self, command: str, env: dict[str, str]) -> list[str]:
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise SandboxError(f"cannot parse command: {exc}") from exc
        if not argv:
            raise SandboxError("empty command")
        exe = argv[0]
        if "/" in exe or "\\" in exe or ":" in exe:
            raise SandboxError(f"executable must be a bare name, got {exe!r}")
        stem = exe.lower().removesuffix(".exe")
        allowed = {a.lower() for a in self.config.command_allowlist}
        if stem not in allowed:
            raise SandboxError(
                f"{exe!r} is not in sandbox.command_allowlist ({', '.join(sorted(allowed))})"
            )
        resolved = shutil.which(exe, path=env.get("PATH"))
        if resolved is None:
            raise SandboxError(f"{exe!r} not found on PATH")
        if resolved.lower().endswith(_SCRIPT_SUFFIXES):
            raise SandboxError(f"{exe!r} resolves to a script ({resolved}); only executables run")
        return [resolved, *argv[1:]]

    def run(
        self, command: str, *, workdir: Path, timeout_s: int | None = None, network: bool = False
    ) -> SandboxResult:
        if network:
            raise SandboxError(
                "the restricted-subprocess sandbox cannot isolate network access; "
                "networked commands require the Docker backend"
            )
        timeout = timeout_s or self.config.timeout_s
        with tempfile.TemporaryDirectory(prefix="factory-sbx-") as home:
            env = sandbox_env(Path(home))
            argv = self.parse(command, env)
            started = time.monotonic()
            kwargs: dict[str, object] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                argv,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kwargs,  # type: ignore[call-overload]
            )
            timed_out = False
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                out, err = proc.communicate()
            return SandboxResult(
                exit_code=proc.returncode if not timed_out else -1,
                stdout=cap(out or ""),
                stderr=cap(err or ""),
                timed_out=timed_out,
                duration_s=round(time.monotonic() - started, 2),
                backend=self.name,
            )


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()
