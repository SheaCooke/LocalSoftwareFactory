from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from factory.config.schema import SandboxConfig
from factory.isolation.sandboxes import SandboxError, select_backend
from factory.isolation.sandboxes.docker_backend import DockerSandbox, container_user, memory_kib
from factory.isolation.sandboxes.subprocess_backend import SubprocessSandbox

CONFIG = SandboxConfig(command_allowlist=["python", "pytest"], timeout_s=30)


@pytest.fixture
def sbx() -> SubprocessSandbox:
    return SubprocessSandbox(CONFIG)


def test_runs_in_workdir(sbx: SubprocessSandbox, tmp_path: Path) -> None:
    result = sbx.run('python -c "import os; print(os.getcwd())"', workdir=tmp_path)
    assert result.exit_code == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()
    assert result.backend == "subprocess" and not result.timed_out


def test_environment_has_no_secrets(
    sbx: SubprocessSandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak-1234")
    monkeypatch.setenv("SOME_RANDOM_VAR", "x")
    code = "import os, json; print(json.dumps(dict(os.environ)))"
    result = sbx.run(f'python -c "{code}"', workdir=tmp_path)
    env = json.loads(result.stdout)
    assert "OPENAI_API_KEY" not in env and "SOME_RANDOM_VAR" not in env
    assert "sk-should-never-leak-1234" not in result.stdout
    assert Path(env["HOME"]).resolve() != tmp_path.resolve()  # throwaway home, not the worktree


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("cmd /c dir", "not in sandbox.command_allowlist"),
        ("powershell -c Get-ChildItem", "not in sandbox.command_allowlist"),
        ("git status", "not in sandbox.command_allowlist"),
        ("../python -V", "bare name"),
        ("C:\\Windows\\System32\\cmd.exe /c dir", "bare name"),
        ("", "empty command"),
        ('python -c "unterminated', "cannot parse"),
    ],
)
def test_refused_commands(
    sbx: SubprocessSandbox, tmp_path: Path, command: str, reason: str
) -> None:
    with pytest.raises(SandboxError, match=reason):
        sbx.run(command, workdir=tmp_path)


def test_shell_metacharacters_are_not_interpreted(sbx: SubprocessSandbox, tmp_path: Path) -> None:
    result = sbx.run(
        'python -c "import sys; print(sys.argv[1:])" a && echo pwned', workdir=tmp_path
    )
    assert "['a', '&&', 'echo', 'pwned']" in result.stdout


def test_timeout_kills_process(sbx: SubprocessSandbox, tmp_path: Path) -> None:
    started = time.monotonic()
    result = sbx.run('python -c "import time; time.sleep(60)"', workdir=tmp_path, timeout_s=1)
    assert result.timed_out and result.exit_code == -1
    assert time.monotonic() - started < 20


def test_network_refused_without_docker(sbx: SubprocessSandbox, tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="cannot isolate network"):
        sbx.run("python -V", workdir=tmp_path, network=True)


@pytest.mark.parametrize("swap_limit", [True, False])
def test_docker_command_is_locked_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_limit: bool
) -> None:
    monkeypatch.setattr(DockerSandbox, "swap_limit_supported", staticmethod(lambda: swap_limit))
    argv = DockerSandbox(CONFIG).command("pytest -q", workdir=tmp_path, name="n1", network=False)
    joined = " ".join(argv)
    for flag in (
        "--network none", f"--user {container_user()}", "--cap-drop ALL", "--read-only",
        "--security-opt no-new-privileges", "--pids-limit 256", "--memory 2g", "--cpus 2.0",
    ):  # fmt: skip
        assert flag in joined
    assert not container_user().startswith("0:")  # never root
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a in ("--mount", "-v", "--volume")]
    assert mounts == [f"type=bind,source={tmp_path.resolve()},target=/workspace"]
    assert "--env" in argv and not any("KEY" in a for a in argv)
    expected = "pytest -q" if swap_limit else "ulimit -v 2097152; pytest -q"
    assert argv[-3:] == ["sh", "-c", expected]
    networked = DockerSandbox(CONFIG).command(
        "pip install x", workdir=tmp_path, name="n", network=True
    )
    assert "bridge" in networked


@pytest.mark.parametrize(
    ("spec", "kib"), [("2g", 2097152), ("512m", 524288), ("256M", 262144), ("1048576", 1024)]
)
def test_memory_kib(spec: str, kib: int) -> None:
    assert memory_kib(spec) == kib


def test_memory_kib_rejects_garbage() -> None:
    with pytest.raises(SandboxError):
        memory_kib("lots")


def test_auto_selection_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerSandbox, "availability", classmethod(lambda cls, c: "daemon down"))
    sel = select_backend(SandboxConfig(command_allowlist=["python"]))
    assert sel.backend.name == "subprocess" and sel.reduced_isolation
    assert "daemon down" in sel.describe() and "REDUCED ISOLATION" in sel.describe()
    with pytest.raises(SandboxError, match="unavailable: daemon down"):
        select_backend(SandboxConfig(backend="docker"))
    monkeypatch.setattr(DockerSandbox, "availability", classmethod(lambda cls, c: None))
    sel = select_backend(SandboxConfig())
    assert sel.backend.name == "docker" and not sel.reduced_isolation


def test_unknown_backend() -> None:
    with pytest.raises(SandboxError, match="unknown sandbox backend"):
        select_backend(SandboxConfig(backend="windows-sandbox"))


# ---------------------------------------------------------------- real Docker (if available)

needs_docker = pytest.mark.skipif(
    DockerSandbox.availability(SandboxConfig()) is not None, reason="Docker sandbox not available"
)


@pytest.fixture
def docker_sbx() -> DockerSandbox:
    return DockerSandbox(SandboxConfig(memory="256m", timeout_s=60))


@needs_docker
def test_docker_really_has_no_network(docker_sbx: DockerSandbox, tmp_path: Path) -> None:
    connect = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    result = docker_sbx.run(f'python -c "{connect}"', workdir=tmp_path)
    assert result.exit_code != 0 and "unreachable" in result.stderr.lower()
    dns = docker_sbx.run(
        "python -c \"import socket; socket.gethostbyname('pypi.org')\"", workdir=tmp_path
    )
    assert dns.exit_code != 0


@needs_docker
def test_docker_mount_user_and_readonly_root(docker_sbx: DockerSandbox, tmp_path: Path) -> None:
    (tmp_path / "in.txt").write_text("from host", "utf-8")
    result = docker_sbx.run(
        "cat in.txt; echo; id -u; echo out > out.txt; touch /etc/x || echo ro-root; "
        "grep CapEff /proc/self/status",
        workdir=tmp_path,
    )
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "from host"
    assert lines[1] == container_user().split(":")[0]
    assert "ro-root" in result.stdout and "Read-only file system" in result.stderr
    assert "CapEff:\t0000000000000000" in result.stdout
    assert "swap limit" not in result.stderr  # daemon host warnings are filtered out
    out = tmp_path / "out.txt"
    assert out.read_text("utf-8") == "out\n"
    if hasattr(os, "getuid"):
        assert out.stat().st_uid == os.getuid()  # host user still owns what the agent wrote


@needs_docker
def test_docker_hides_host_secrets_and_filesystem(
    docker_sbx: DockerSandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-docker-should-not-leak-1")
    result = docker_sbx.run("env; ls /workspace; ls /mnt 2>&1 || true", workdir=tmp_path)
    assert "sk-docker-should-not-leak-1" not in result.stdout
    assert "OPENAI_API_KEY" not in result.stdout
    assert "/mnt/c" not in result.stdout and "Users" not in result.stdout


@needs_docker
def test_docker_memory_limit_is_enforced(docker_sbx: DockerSandbox, tmp_path: Path) -> None:
    hog = (
        "b = bytearray(600 * 1024 * 1024)\n"
        "for i in range(0, len(b), 4096):\n"
        "    b[i] = 1\n"
        "print('allocated')\n"
    )
    (tmp_path / "hog.py").write_text(hog, "utf-8")
    result = docker_sbx.run("python hog.py", workdir=tmp_path)
    assert result.exit_code != 0 and "allocated" not in result.stdout


@needs_docker
def test_docker_timeout_kills_container(docker_sbx: DockerSandbox, tmp_path: Path) -> None:
    import subprocess

    result = docker_sbx.run("sleep 120", workdir=tmp_path, timeout_s=3)
    assert result.timed_out and result.duration_s < 30
    # `--rm` removes the killed container asynchronously; give the daemon a moment.
    deadline = time.monotonic() + 10
    leftover = "?"
    while leftover and time.monotonic() < deadline:
        leftover = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=factory-"], capture_output=True, text=True
        ).stdout.strip()
        if leftover:
            time.sleep(0.25)
    assert leftover == ""


@needs_docker
def test_docker_runs_pytest(docker_sbx: DockerSandbox, tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", "utf-8")
    result = docker_sbx.run("pytest -q", workdir=tmp_path)
    assert result.exit_code == 0 and "1 passed" in result.stdout
    assert not (tmp_path / ".pytest_cache").exists() and not (tmp_path / "__pycache__").exists()
