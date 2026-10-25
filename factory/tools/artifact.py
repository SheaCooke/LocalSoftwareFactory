"""Artifact tools. Agents may read only their declared `inputs` (and their own
outputs), and write only their declared `outputs`."""

from __future__ import annotations

from typing import Any

from factory.core.artifacts import ArtifactError
from factory.tools.registry import ToolContext, ToolError, tool

_NAME = {"type": "string", "description": "Artifact name, e.g. plan.md"}


def _readable(ctx: ToolContext) -> list[str]:
    return [*ctx.agent.inputs, *ctx.agent.outputs]


@tool(
    "artifact.read",
    {"type": "object", "properties": {"name": _NAME}, "required": ["name"]},
    required_isolation_level="L1",
)
def artifact_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Read one of your input artifacts (or an output you already wrote)."""
    name = args["name"]
    if name not in _readable(ctx):
        raise ctx.deny("artifact.read", name, f"{name!r} is not one of this agent's inputs")
    try:
        content = ctx.artifacts.read_optional(name)
    except ArtifactError as exc:
        raise ToolError(str(exc)) from exc
    if content is None:
        raise ToolError(f"artifact {name!r} has not been produced yet")
    return content


@tool(
    "artifact.write",
    {
        "type": "object",
        "properties": {"name": _NAME, "content": {"type": "string"}},
        "required": ["name", "content"],
    },
    required_isolation_level="L1",
)
def artifact_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Write (replace) one of your declared output artifacts."""
    name, content = args["name"], args["content"]
    if name not in ctx.agent.outputs:
        raise ctx.deny("artifact.write", name, f"{name!r} is not one of this agent's outputs")
    try:
        ctx.artifacts.write(name, content)
    except ArtifactError as exc:
        raise ToolError(str(exc)) from exc
    ctx.bus.emit(
        "artifact.written",
        run_id=ctx.run_id,
        stage=ctx.stage_id,
        agent=ctx.agent.name,
        data={"name": name, "chars": len(content)},
    )
    return f"wrote {name} ({len(content)} chars)"


@tool("artifact.list", {"type": "object", "properties": {}}, required_isolation_level="L1")
def artifact_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    """List which of your input/output artifacts currently exist."""
    lines = [
        f"{name}: {'present' if ctx.artifacts.exists(name) else 'missing'}"
        for name in dict.fromkeys(_readable(ctx))
    ]
    return "\n".join(lines) or "(this agent declares no artifacts)"
