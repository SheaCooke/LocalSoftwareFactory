"""shell.run_sandboxed: run a command in the agent's worktree inside the sandbox."""

from __future__ import annotations

from typing import Any

from factory.agents.spec import IsolationLevel
from factory.isolation.sandboxes.base import SandboxError
from factory.tools.registry import ToolContext, ToolError, tool


@tool(
    "shell.run_sandboxed",
    {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command to run in your workspace root, e.g. `pytest -q`",
            },
            "timeout_s": {"type": "integer", "description": "Optional timeout in seconds"},
            "network": {
                "type": "boolean",
                "description": "Needs network (L3 agents only; requires human approval)",
            },
        },
        "required": ["command"],
    },
    required_isolation_level="L2",
)
def run_sandboxed(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Run a command in your workspace inside the isolation sandbox (no network by default)."""
    command: str = args["command"]
    network = bool(args.get("network", False))
    if ctx.sandbox is None or ctx.workspace is None:
        raise ctx.deny("shell.run_sandboxed", command, "no sandbox is available to this agent")
    if network:
        if ctx.agent.level < IsolationLevel.L3:
            level = ctx.agent.isolation_level
            reason = f"network access requires isolation level L3 (agent is {level})"
            raise ctx.deny("shell.run_sandboxed", command, reason)
        if ctx.approve_network is None or not ctx.approve_network(command):
            raise ctx.deny("shell.run_sandboxed", command, "network access was not approved")
    limit = ctx.sandbox.config.timeout_s
    timeout = min(int(args.get("timeout_s") or limit), limit)
    try:
        result = ctx.sandbox.run(command, workdir=ctx.workspace, timeout_s=timeout, network=network)
    except SandboxError as exc:
        raise ctx.deny("shell.run_sandboxed", command, str(exc)) from None
    except OSError as exc:
        raise ToolError(f"could not start command: {exc}") from exc
    return result.render()
