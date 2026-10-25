from __future__ import annotations

from pathlib import Path

import pytest

from factory.isolation.worktrees import GitError, RunRepo, git_available, run_git

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _target_repo(path: Path) -> Path:
    path.mkdir()
    run_git(["init", "-q", "-b", "main"], path)
    (path / "README.md").write_text("# Target\n", "utf-8")
    run_git(["add", "."], path)
    run_git(["commit", "-q", "-m", "init"], path)
    return path


def test_new_project_repo(tmp_path: Path) -> None:
    repo = RunRepo(tmp_path / "ws", "r1")
    repo.ensure(None)
    assert repo.exists
    assert run_git(["branch", "--show-current"], repo.repo).strip() == "factory/r1/integration"
    assert "factory/r1/base" in run_git(["tag"], repo.repo)
    repo.ensure(None)  # idempotent


def test_clone_leaves_target_untouched(tmp_path: Path) -> None:
    target = _target_repo(tmp_path / "target")
    before = run_git(["for-each-ref"], target)
    repo = RunRepo(tmp_path / "ws", "r1")
    repo.ensure(target)
    wt = repo.prepare("implementer", writable=True)
    (wt / "src").mkdir()
    (wt / "src" / "app.py").write_text("print('hi')\n", "utf-8")
    repo.commit("implementer", "work")
    repo.merge_to_integration("implementer")
    assert run_git(["for-each-ref"], target) == before  # no branches/tags added to target
    assert not (target / "src").exists()


def test_not_a_repo_rejected(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    with pytest.raises(GitError, match="not a git repository"):
        RunRepo(tmp_path / "ws", "r1").ensure(tmp_path / "plain")


def test_worktree_flow_between_agents(tmp_path: Path) -> None:
    repo = RunRepo(tmp_path / "ws", "r1")
    repo.ensure(_target_repo(tmp_path / "target"))

    tester = repo.prepare("tester", writable=True)
    assert tester == tmp_path / "ws" / "r1" / "tester"
    assert (tester / "README.md").is_file()
    (tester / "tests").mkdir()
    (tester / "tests" / "test_x.py").write_text("def test_x(): pass\n", "utf-8")
    assert repo.commit("tester", "tests") is not None
    assert repo.merge_to_integration("tester") is True
    assert repo.merge_to_integration("tester") is False  # nothing new

    impl = repo.prepare("implementer", writable=True)
    assert (impl / "tests" / "test_x.py").is_file()  # sees merged tests
    assert not (tester / "src").exists()
    (impl / "src").mkdir()
    (impl / "src" / "x.py").write_text("X = 1\n", "utf-8")
    assert "src/x.py" in repo.diff(impl)  # new, untracked file shows in diff
    repo.commit("implementer", "impl")
    repo.merge_to_integration("implementer")
    assert set(repo.files_on_integration()) == {"README.md", "tests/test_x.py", "src/x.py"}

    # Writable worktrees persist and pick up integration on the next invocation.
    tester = repo.prepare("tester", writable=True)
    assert (tester / "src" / "x.py").is_file()

    # Read-only worktrees are reset to integration every time.
    reviewer = repo.prepare("reviewer", writable=False)
    (reviewer / "scratch.txt").write_text("junk", "utf-8")
    (reviewer / "src" / "x.py").write_text("tampered", "utf-8")
    reviewer = repo.prepare("reviewer", writable=False)
    assert not (reviewer / "scratch.txt").exists()
    assert (reviewer / "src" / "x.py").read_text("utf-8") == "X = 1\n"


def test_commit_without_changes_returns_none(tmp_path: Path) -> None:
    repo = RunRepo(tmp_path / "ws", "r1")
    repo.ensure(None)
    repo.prepare("docs", writable=True)
    assert repo.commit("docs", "nothing") is None


def test_conflicting_merge_raises_and_leaves_integration_clean(tmp_path: Path) -> None:
    repo = RunRepo(tmp_path / "ws", "r1")
    repo.ensure(_target_repo(tmp_path / "target"))
    a = repo.prepare("a", writable=True)
    b = repo.prepare("b", writable=True)
    (a / "README.md").write_text("A\n", "utf-8")
    (b / "README.md").write_text("B\n", "utf-8")
    repo.commit("a", "a")
    repo.commit("b", "b")
    repo.merge_to_integration("a")
    with pytest.raises(GitError, match="conflicts"):
        repo.merge_to_integration("b")
    assert run_git(["status", "--porcelain"], repo.repo).strip() == ""
    assert (repo.repo / "README.md").read_text("utf-8") == "A\n"


def test_worktree_rebuilt_if_deleted(tmp_path: Path) -> None:
    import shutil

    repo = RunRepo(tmp_path / "ws", "r1")
    repo.ensure(None)
    wt = repo.prepare("docs", writable=True)
    shutil.rmtree(wt)
    assert (repo.prepare("docs", writable=True) / ".git").exists()
