from __future__ import annotations

import os
from pathlib import Path

import pytest

from factory.agents.spec import FsScope
from factory.isolation.permissions import PathPolicy, PermissionDenied, compile_glob

IMPLEMENTER = FsScope(
    read=["{workspace}/**", "{artifacts}/plan.md"],
    write=["{workspace}/src/**"],
    deny=["{workspace}/tests/**"],
    deny_read=["{workspace}/secrets/**"],
)


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "secrets").mkdir()
    (ws / "src" / "app.py").write_text("x = 1\n", "utf-8")
    (ws / "tests" / "test_app.py").write_text("def test(): pass\n", "utf-8")
    (ws / ".git").write_text("gitdir: elsewhere\n", "utf-8")
    (ws / ".env").write_text("KEY=1\n", "utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "plan.md").write_text("plan", "utf-8")
    (artifacts / "design.md").write_text("design", "utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", "utf-8")
    return {"ws": ws, "artifacts": artifacts, "outside": outside}


@pytest.fixture
def policy(layout: dict[str, Path]) -> PathPolicy:
    return PathPolicy(IMPLEMENTER, workspace=layout["ws"], artifacts=layout["artifacts"])


def test_allowed_paths(policy: PathPolicy, layout: dict[str, Path]) -> None:
    assert policy.check("src/app.py", "read") == (layout["ws"] / "src/app.py").resolve()
    assert policy.check("src/new/module.py", "write").name == "module.py"
    assert policy.check("tests/test_app.py", "read").name == "test_app.py"  # read, not write
    assert policy.check(".", "read") == layout["ws"].resolve()
    assert policy.check(str(layout["ws"] / "src" / "app.py"), "write")  # absolute, in scope
    assert policy.check(str(layout["artifacts"] / "plan.md"), "read")  # via {artifacts}
    assert policy.check("src\\app.py", "read")  # Windows separators


@pytest.mark.parametrize(
    ("raw", "mode", "reason"),
    [
        ("../outside/secret.txt", "read", "traversal"),
        ("src/../../outside/secret.txt", "read", "traversal"),
        ("src/..", "read", "traversal"),
        ("tests/test_app.py", "write", "denied by fs_scope.deny"),  # deny beats write
        ("tests/new_test.py", "write", "denied by fs_scope.deny"),
        ("README.md", "write", "not allowed by fs_scope.write"),
        ("secrets/k.txt", "read", "deny_read"),
        (".git", "read", ".git"),
        (".git/config", "write", "traversal|.git"),
        (".env", "read", "secrets file"),
        ("src/app.py:evil", "write", "alternate data streams"),
        ("tests./x.py", "write", "may not end with"),
        ("src /x.py", "write", "may not end with"),
        ("src/CON", "write", "reserved device name"),
        ("src/nul.txt", "write", "reserved device name"),
        ("//server/share/x", "read", "UNC"),
        ("\\\\?\\C:\\Windows\\win.ini", "read", "UNC"),
        ("", "read", "empty"),
        ("src/a\x00.py", "write", "NUL"),
    ],
)
def test_denied_paths(policy: PathPolicy, raw: str, mode: str, reason: str) -> None:
    with pytest.raises(PermissionDenied, match=reason):
        policy.check(raw, mode)  # type: ignore[arg-type]


def test_deny_beats_overlapping_allow(layout: dict[str, Path]) -> None:
    scope = FsScope(
        read=["{workspace}/**"],
        write=["{workspace}/**"],
        deny=["{workspace}/tests/**"],
        deny_read=["{workspace}/secrets/**"],
    )
    policy = PathPolicy(scope, workspace=layout["ws"], artifacts=None)
    assert policy.check("src/app.py", "write")
    with pytest.raises(PermissionDenied, match=r"denied by fs_scope\.deny"):
        policy.check("tests/test_app.py", "write")
    assert policy.check("tests/test_app.py", "read")  # deny blocks writes only
    with pytest.raises(PermissionDenied, match="deny_read"):
        policy.check("secrets/x", "read")


def test_absolute_outside_scope_denied(policy: PathPolicy, layout: dict[str, Path]) -> None:
    with pytest.raises(PermissionDenied, match="outside the agent's scope"):
        policy.check(str(layout["outside"] / "secret.txt"), "read")
    with pytest.raises(PermissionDenied, match=r"not allowed by fs_scope\.read"):
        policy.check(str(layout["artifacts"] / "design.md"), "read")  # artifact not in scope


@pytest.mark.skipif(os.name != "nt", reason="drive-relative paths are Windows-only")
def test_drive_relative_and_rooted_denied(policy: PathPolicy) -> None:
    for raw in ("C:secret.txt", "\\Windows\\win.ini"):
        with pytest.raises(PermissionDenied):
            policy.check(raw, "read")


@pytest.mark.skipif(os.name != "nt", reason="NTFS is case-insensitive")
def test_case_variants_cannot_bypass_deny(policy: PathPolicy) -> None:
    with pytest.raises(PermissionDenied, match="deny"):
        policy.check("TESTS/test_app.py", "write")
    with pytest.raises(PermissionDenied, match=r"\.git"):
        policy.check(".GIT", "read")
    assert policy.check("SRC/app.py", "write")


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are Windows-only")
def test_short_names_cannot_bypass_deny(layout: dict[str, Path]) -> None:
    import ctypes

    protected = layout["ws"] / "protected_directory"
    protected.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(protected), buf, 1024)  # type: ignore[attr-defined]
    short_name = Path(buf.value).name
    if not buf.value or short_name.lower() == protected.name.lower():
        pytest.skip("8.3 short names are disabled on this volume")
    scope = FsScope(write=["{workspace}/**"], deny=["{workspace}/protected_directory/**"])
    policy = PathPolicy(scope, workspace=layout["ws"], artifacts=None)
    with pytest.raises(PermissionDenied, match="deny"):
        policy.check(f"{short_name}/new.py", "write")


def test_symlink_escape_denied(policy: PathPolicy, layout: dict[str, Path]) -> None:
    link = layout["ws"] / "src" / "link"
    try:
        os.symlink(layout["outside"], link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs Developer Mode or admin on Windows")
    with pytest.raises(PermissionDenied, match="outside the agent's scope"):
        policy.check("src/link/secret.txt", "read")
    with pytest.raises(PermissionDenied, match="outside the agent's scope"):
        policy.check("src/link/new.txt", "write")


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_junction_escape_denied(policy: PathPolicy, layout: dict[str, Path]) -> None:
    import _winapi

    _winapi.CreateJunction(str(layout["outside"]), str(layout["ws"] / "src" / "junction"))
    with pytest.raises(PermissionDenied, match="outside the agent's scope"):
        policy.check("src/junction/secret.txt", "read")


def test_hard_link_denied(policy: PathPolicy, layout: dict[str, Path]) -> None:
    try:
        os.link(layout["outside"] / "secret.txt", layout["ws"] / "src" / "alias.txt")
    except OSError:
        pytest.skip("hard links unsupported on this filesystem")
    with pytest.raises(PermissionDenied, match="hard links"):
        policy.check("src/alias.txt", "read")


def test_no_workspace_means_no_relative_paths(layout: dict[str, Path]) -> None:
    policy = PathPolicy(IMPLEMENTER, workspace=None, artifacts=layout["artifacts"])
    with pytest.raises(PermissionDenied, match="no workspace"):
        policy.check("src/app.py", "read")
    assert policy.check(str(layout["artifacts"] / "plan.md"), "read")


def test_empty_scope_denies_everything(layout: dict[str, Path]) -> None:
    policy = PathPolicy(FsScope(), workspace=layout["ws"], artifacts=layout["artifacts"])
    for mode in ("read", "write"):
        with pytest.raises(PermissionDenied):
            policy.check("src/app.py", mode)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("{workspace}/src/**", "/w/src", True),
        ("{workspace}/src/**", "/w/src/a/b.py", True),
        ("{workspace}/src/**", "/w/srcx/a.py", False),
        ("{workspace}/src/*.py", "/w/src/a.py", True),
        ("{workspace}/src/*.py", "/w/src/sub/a.py", False),
        ("{workspace}/**/test_*.py", "/w/test_a.py", True),
        ("{workspace}/**/test_*.py", "/w/x/y/test_a.py", True),
        ("{workspace}/README.md", "/w/README.md", True),
        ("{workspace}/README.md", "/w/README.mdx", False),
        ("src/**", "/w/src/a.py", True),  # relative patterns are workspace-relative
        ("{artifacts}/plan.md", "/w/plan.md", False),  # {artifacts} unavailable -> no match
    ],
)
def test_glob_semantics(pattern: str, path: str, matches: bool) -> None:
    rx = compile_glob(pattern, {"workspace": Path("/w")})
    root = Path("/w").as_posix()
    assert bool(rx.match(path.replace("/w", root, 1))) is matches


def test_workspace_path_with_regex_characters(tmp_path: Path) -> None:
    ws = tmp_path / "we(ird)+[path]"
    (ws / "src").mkdir(parents=True)
    policy = PathPolicy(
        FsScope(read=["{workspace}/**"], write=["{workspace}/src/**"]), workspace=ws, artifacts=None
    )
    assert policy.check("src/a.py", "write")
