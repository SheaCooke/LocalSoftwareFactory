"""Docker sandbox (Docker Engine inside WSL2/Linux, or Docker Desktop on Windows).

Each command runs in a fresh container: only the agent's worktree is mounted (at
/workspace), there is no network unless an L3 agent was approved for it, CPU,
memory and process limits apply, the user is non-root (the host uid on Linux), all
capabilities are dropped, the root filesystem is read-only (with a /tmp tmpfs),
and the container is killed on timeout. Where the daemon cannot limit swap, each
process's address space is additionally capped with `ulimit -v`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
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

DOCKERFILE = Path(__file__).with_name("Dockerfile")
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, str | None]] = {}
_cache_lock = threading.Lock()
_swap_limit: dict[str, bool] = {}
# Daemon warnings that are about the host, not the command (e.g. WSL2 without swap accounting).
_DOCKER_NOISE = re.compile(r"^WARNING: Your kernel does not support swap limit")
_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def memory_kib(spec: str) -> int:
    """Docker memory string ('2g', '512m', '1048576') to KiB."""
    match = re.fullmatch(r"\s*(\d+)\s*([bkmg]?)b?\s*", spec.lower())
    if not match:
        raise SandboxError(f"invalid sandbox.memory value {spec!r}")
    return int(match.group(1)) * _UNITS[match.group(2)] // 1024


def container_user() -> str:
    """Run as the host user on Linux/WSL so files written to the bind mount stay owned
    by them; never as root. Docker Desktop on Windows maps ownership itself."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is not None and getgid is not None and getuid() != 0:
        return f"{getuid()}:{getgid()}"
    return "1000:1000"


def _docker(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def build_image(config: SandboxConfig) -> subprocess.CompletedProcess[str]:
    """`docker build` the sandbox image from the bundled Dockerfile."""
    with _cache_lock:
        _cache.clear()
    return _docker(
        ["build", "-t", config.image, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)], timeout=1800
    )


@register_sandbox("docker")
class DockerSandbox(SandboxBackend):
    reduced_isolation: ClassVar[bool] = False

    @classmethod
    def availability(cls, config: SandboxConfig) -> str | None:
        with _cache_lock:
            hit = _cache.get(config.image)
            if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
                return hit[1]
        reason = cls._probe(config)
        with _cache_lock:
            _cache[config.image] = (time.monotonic(), reason)
        return reason

    @staticmethod
    def _probe(config: SandboxConfig) -> str | None:
        if shutil.which("docker") is None:
            return "docker CLI not found"
        try:
            info = _docker(["info", "--format", "{{.ServerVersion}}|{{.SwapLimit}}"], timeout=20)
            if info.returncode != 0:
                return (
                    "Docker daemon is not running (start Docker Desktop, or in WSL/Linux: "
                    "`sudo service docker start`) or this user cannot reach it"
                )
            _swap_limit["supported"] = info.stdout.strip().endswith("|true")
            image = _docker(["image", "inspect", "--format", "{{.Id}}", config.image], timeout=20)
        except subprocess.TimeoutExpired:
            return "Docker did not respond"
        if image.returncode != 0:
            return f"sandbox image {config.image!r} not built (run `factory sandbox build`)"
        return None

    @staticmethod
    def swap_limit_supported() -> bool:
        """Whether the daemon can cap swap (False on WSL2 without systemd, for example)."""
        if "supported" not in _swap_limit:
            try:
                info = _docker(["info", "--format", "{{.SwapLimit}}"], timeout=20)
                _swap_limit["supported"] = info.stdout.strip() == "true"
            except (OSError, subprocess.TimeoutExpired):
                _swap_limit["supported"] = False
        return _swap_limit["supported"]

    def command(self, command: str, *, workdir: Path, name: str, network: bool) -> list[str]:
        source = str(workdir.resolve())
        if "," in source:
            raise SandboxError(f"worktree path may not contain ',' for Docker mounts: {source}")
        c = self.config
        if not self.swap_limit_supported():
            # --memory caps RAM but not swap here, so a runaway process would spill into
            # host swap instead of being killed. Cap each process's address space too.
            command = f"ulimit -v {memory_kib(c.memory)}; {command}"
        return [
            "docker", "run", "--rm", "--name", name,
            "--network", "bridge" if network else "none",
            "--cpus", str(c.cpus),
            "--memory", c.memory,
            "--pids-limit", str(c.pids_limit),
            "--user", container_user(),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,size=512m",
            "--mount", f"type=bind,source={source},target=/workspace",
            "--workdir", "/workspace",
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONUTF8=1",
            "--env", "PYTEST_ADDOPTS=-p no:cacheprovider",
            c.image,
            "sh", "-c", command,
        ]  # fmt: skip

    def run(
        self, command: str, *, workdir: Path, timeout_s: int | None = None, network: bool = False
    ) -> SandboxResult:
        timeout = timeout_s or self.config.timeout_s
        name = f"factory-{uuid.uuid4().hex[:12]}"
        argv = self.command(command, workdir=workdir, name=name, network=network)
        started = time.monotonic()
        # The docker CLI itself needs the user's environment (DOCKER_HOST etc.). Nothing
        # from it reaches the container: only the --env values above do.
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=dict(os.environ),
            )
            code, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired as exc:
            _docker(["kill", name], timeout=30)
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            code, timed_out = -1, True
        err = "".join(
            line for line in err.splitlines(keepends=True) if not _DOCKER_NOISE.match(line)
        )
        return SandboxResult(
            exit_code=code,
            stdout=cap(out),
            stderr=cap(err),
            timed_out=timed_out,
            duration_s=round(time.monotonic() - started, 2),
            backend=self.name,
        )
