"""Context assembly: the fresh message list each agent invocation starts from.

An agent sees only its own system prompt, the run request, any gate feedback, and
the artifacts listed in its `inputs`. No other agent's conversation is shared.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from factory.agents.spec import AgentSpec
from factory.core.artifacts import ArtifactStore
from factory.isolation.sandboxes.base import SandboxBackend
from factory.isolation.worktrees import RunRepo
from factory.providers.base import Message

_TAGGED = re.compile(r'<artifact\s+name="([^"]+)"\s*>\n?(.*?)\n?</artifact>', re.DOTALL)
_WHOLE_FENCE = re.compile(r"\A```[\w-]*\n(.*)\n```\s*\Z", re.DOTALL)
_MAX_INPUT_CHARS = 200_000


@dataclass(frozen=True)
class AgentIsolation:
    """Filesystem and execution resources granted to one invocation (L1+)."""

    workspace: Path
    repo: RunRepo
    sandbox: SandboxBackend | None = None  # L2+
    approve_network: Callable[[str], bool] | None = None  # L3


@dataclass(frozen=True)
class InvocationContext:
    run_id: str
    stage_id: str
    request: str
    artifacts: ArtifactStore
    model_override: str | None = None  # per-run override: beats everything
    model_default: str | None = None  # pipeline stage `model:`: replaces the agent.md default
    feedback: str | None = None  # human gate comment or loop-back reason
    attempt: int = 1
    isolation: AgentIsolation | None = None
    #: Called before every LLM call; returns a reason string when a budget is exhausted.
    budget_check: Callable[[], str | None] | None = None
    #: Re-reads the per-run override before every LLM call (so the UI can switch it mid-run).
    model_override_source: Callable[[], str | None] | None = None

    def current_model_override(self) -> str | None:
        if self.model_override_source is not None:
            return self.model_override_source()
        return self.model_override


def writes_outputs_with_tool(spec: AgentSpec, tool_names: Iterable[str]) -> bool:
    return "artifact.write" in tool_names and bool(spec.outputs)


def system_prompt(spec: AgentSpec, tool_names: list[str]) -> str:
    lines = [
        spec.system_prompt,
        "",
        "---",
        "## Factory contract",
        f"- You are the `{spec.name}` agent (isolation level {spec.isolation_level}).",
        "- This is a fresh conversation. You know only what this conversation and your "
        "tools show you. Other agents communicate with you only through artifacts.",
    ]
    if tool_names:
        lines.append(
            f"- Tools available to you: {', '.join(tool_names)}. Any other access is denied."
        )
    else:
        lines.append("- You have no tools. Answer in text only.")
    if any(t.startswith(("fs.", "git.", "shell.")) for t in tool_names):

        def show(globs: list[str]) -> str:
            pretty = [g.replace("{workspace}/", "").replace("{workspace}", ".") for g in globs]
            return ", ".join(f"`{g}`" for g in pretty) or "nothing"

        scope = spec.fs_scope
        lines.append(
            "- File paths are relative to your workspace root (a private git worktree of the "
            f"project). You may read {show(scope.read)} and write {show(scope.write)}."
        )
        if scope.deny:
            lines.append(f"- You may never modify {show(scope.deny)}.")
        lines.append("- Attempts outside this scope are blocked and logged.")
    if writes_outputs_with_tool(spec, tool_names):
        lines.append(
            "- Write every required output with `artifact.write`, then finish with a short "
            "plain-text summary."
        )
    elif len(spec.outputs) == 1:
        lines.append(
            f"- Your final reply is saved verbatim as `{spec.outputs[0]}`. Reply with the "
            "complete artifact content and nothing else."
        )
    elif spec.outputs:
        lines.append(
            '- Wrap each required output as <artifact name="NAME">CONTENT</artifact> in your '
            "final reply."
        )
    return "\n".join(lines)


def task_message(spec: AgentSpec, ctx: InvocationContext) -> str:
    parts = [
        f"# Request\n{ctx.request.strip()}",
        f"# Stage\n`{ctx.stage_id}` of run `{ctx.run_id}`",
    ]
    if ctx.feedback:
        parts.append(f"# Feedback to address\n{ctx.feedback.strip()}")
    inputs = [n for n in spec.inputs if n != "request.md"]
    if inputs:
        sections = ["# Input artifacts"]
        for name in inputs:
            content = ctx.artifacts.read_optional(name)
            if content is None:
                sections.append(f"## {name}\n(not produced yet)")
            else:
                if len(content) > _MAX_INPUT_CHARS:
                    content = content[:_MAX_INPUT_CHARS] + "\n... [truncated]"
                sections.append(f"## {name}\n{content}")
        parts.append("\n\n".join(sections))
    for name in spec.outputs:
        previous = ctx.artifacts.read_optional(name)
        if previous is not None and ctx.feedback:
            parts.append(f"# Your previous {name}\n{previous}")
    if spec.outputs:
        parts.append("# Required outputs\n" + "\n".join(f"- {n}" for n in spec.outputs))
    return "\n\n".join(parts)


def initial_messages(
    spec: AgentSpec, ctx: InvocationContext, tool_names: list[str]
) -> list[Message]:
    return [Message.system(system_prompt(spec, tool_names)), Message.user(task_message(spec, ctx))]


def _unfence(text: str) -> str:
    match = _WHOLE_FENCE.match(text.strip())
    return match.group(1) if match else text.strip()


def outputs_from_final(spec: AgentSpec, final: str, pending: list[str]) -> dict[str, str]:
    """Extract still-missing outputs from the final reply (tagged blocks or whole text)."""
    found: dict[str, str] = {}
    tagged = {name: body for name, body in _TAGGED.findall(final)}
    for name in pending:
        if name in tagged:
            found[name] = _unfence(tagged[name]) + "\n"
    if not tagged and len(pending) == 1 and final.strip():
        found[pending[0]] = _unfence(final) + "\n"
    return found
