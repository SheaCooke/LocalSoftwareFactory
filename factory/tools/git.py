"""Read-only git tools over the agent's own worktree."""

from __future__ import annotations

from typing import Any

from factory.isolation.worktrees import GitError
from factory.tools.registry import ToolContext, ToolError, tool


def _require(ctx: ToolContext, name: str) -> None:
    if ctx.repo is None or ctx.workspace is None:
        raise ctx.deny(name, "workspace", "this agent has no git worktree")


@tool("git.diff", {"type": "object", "properties": {}}, required_isolation_level="L1")
def git_diff(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Show everything changed in your workspace since the run started (incl. new files)."""
    _require(ctx, "git.diff")
    assert ctx.repo is not None and ctx.workspace is not None
    try:
        return ctx.repo.diff(ctx.workspace) or "(no changes since the run started)"
    except GitError as exc:
        raise ToolError(str(exc)) from exc


@tool("git.status", {"type": "object", "properties": {}}, required_isolation_level="L1")
def git_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Show `git status --short` for your workspace."""
    _require(ctx, "git.status")
    assert ctx.repo is not None and ctx.workspace is not None
    try:
        return ctx.repo.status(ctx.workspace)
    except GitError as exc:
        raise ToolError(str(exc)) from exc
