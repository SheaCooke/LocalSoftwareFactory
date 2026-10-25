"""The agent loop: call the LLM, execute tool calls, repeat until a final answer.

The model is resolved on every iteration (see ModelResolver), so a model switch
made mid-invocation applies to the next turn. Every call is reported as an
`llm.call` event, and the full transcript is saved (redacted) when the invocation ends.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from factory.agents.context import InvocationContext, initial_messages, outputs_from_final
from factory.agents.spec import READ_ONLY_ROLES, WRITE_TOOLS, AgentSpec, IsolationLevel
from factory.config.loader import ConfigError
from factory.core.events import EventBus
from factory.isolation.permissions import PathPolicy
from factory.providers.base import Message, ProviderError, Usage
from factory.providers.resolver import ModelResolver
from factory.secrets import redactor
from factory.tools.registry import ToolConfigError, ToolContext, ToolRegistry


@dataclass
class AgentResult:
    agent: str
    status: Literal["passed", "failed"]
    final: str = ""
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    iterations: int = 0
    outputs: list[str] = field(default_factory=list)
    error: str | None = None
    transcript: Path | None = None
    #: Set when a run/stage budget (not the agent's own max_tokens_budget) stopped it.
    budget_exceeded: str | None = None


class AgentRuntime:
    def __init__(
        self, resolver: ModelResolver, tools: ToolRegistry, bus: EventBus, runs_dir: Path
    ) -> None:
        self.resolver = resolver
        self.tools = tools
        self.bus = bus
        self.runs_dir = runs_dir

    def invoke(self, spec: AgentSpec, ctx: InvocationContext) -> AgentResult:
        result = AgentResult(spec.name, "failed")
        messages: list[Message] = []
        models_used: list[str] = []
        emit = self._emitter(spec, ctx)
        emit("agent.started", {"attempt": ctx.attempt, "inputs": spec.inputs})
        before = {name: self._stamp(ctx, name) for name in spec.outputs}
        try:
            allowed = self.tools.for_agent(spec)
            if spec.role in READ_ONLY_ROLES and WRITE_TOOLS.intersection(allowed):
                raise ToolConfigError(f"read-only role {spec.role!r} cannot hold write tools")
            tool_ctx = self._tool_context(spec, ctx)
            tool_specs = [t.spec for t in allowed.values()]
            messages = initial_messages(spec, ctx, list(allowed))
            final: str | None = None
            for iteration in range(1, spec.max_iterations + 1):
                result.iterations = iteration
                if ctx.budget_check is not None and (reason := ctx.budget_check()):
                    result.budget_exceeded = reason
                    raise _Fail(reason)
                resolved, response = self.resolver.complete(
                    messages,
                    tool_specs or None,
                    agent=spec.name,
                    agent_default=ctx.model_default or spec.model,
                    run_override=ctx.current_model_override(),
                    metadata={"agent": spec.name, "stage": ctx.stage_id, "run": ctx.run_id},
                )
                u = response.usage
                cost = resolved.model.pricing.cost(
                    u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens
                )
                result.usage += response.usage
                result.cost_usd += cost
                if resolved.alias not in models_used:
                    models_used.append(resolved.alias)
                emit(
                    "llm.call",
                    {
                        "alias": resolved.alias,
                        "source": resolved.source,
                        "provider": resolved.provider_name,
                        "model": resolved.model_id,
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "cost_usd": cost,
                        "latency_ms": round(response.latency_ms, 1),
                        "finish_reason": response.finish_reason,
                        "tool_calls": len(response.tool_calls),
                        "iteration": iteration,
                    },
                )
                messages.append(response.as_message())
                if response.content:
                    emit("agent.message", {"content": response.content, "iteration": iteration})
                if result.usage.total_tokens > spec.max_tokens_budget:
                    raise _Fail(
                        f"token budget exceeded ({result.usage.total_tokens} > "
                        f"{spec.max_tokens_budget})"
                    )
                if not response.tool_calls:
                    final = response.content
                    break
                for call in response.tool_calls:
                    output = self.tools.execute(call, allowed, tool_ctx)
                    messages.append(Message.tool_result(call, output))
            if final is None:
                raise _Fail(f"no final answer within max_iterations={spec.max_iterations}")
            result.final = final
            pending = [n for n in spec.outputs if self._stamp(ctx, n) == before[n]]
            for name, content in outputs_from_final(spec, final, pending).items():
                ctx.artifacts.write(name, content)
                emit("artifact.written", {"name": name, "chars": len(content), "from": "final"})
                pending.remove(name)
            if pending:
                raise _Fail(f"required output(s) not produced: {', '.join(pending)}")
            result.outputs = list(spec.outputs)
            result.status = "passed"
        except _Fail as exc:
            result.error = str(exc)
        except (ToolConfigError, ConfigError, ProviderError) as exc:
            result.error = redactor.redact(f"{type(exc).__name__}: {exc}")
        finally:
            result.transcript = self._save_transcript(spec, ctx, messages, result, models_used)
            emit(
                "agent.finished",
                {
                    "status": result.status,
                    "error": result.error,
                    "iterations": result.iterations,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cost_usd": result.cost_usd,
                },
            )
        return result

    # ------------------------------------------------------------------------- helpers

    def _tool_context(self, spec: AgentSpec, ctx: InvocationContext) -> ToolContext:
        iso = ctx.isolation
        tool_ctx = ToolContext(ctx.run_id, ctx.stage_id, spec, ctx.artifacts, self.bus)
        if iso is not None and spec.level >= IsolationLevel.L1:
            tool_ctx.workspace = iso.workspace
            tool_ctx.repo = iso.repo
            tool_ctx.policy = PathPolicy(
                spec.fs_scope, workspace=iso.workspace, artifacts=ctx.artifacts.directory
            )
            if spec.level >= IsolationLevel.L2:
                tool_ctx.sandbox = iso.sandbox
            if spec.level >= IsolationLevel.L3:
                tool_ctx.approve_network = iso.approve_network
        return tool_ctx

    def _emitter(
        self, spec: AgentSpec, ctx: InvocationContext
    ) -> Callable[[str, dict[str, object]], None]:
        def emit(type: str, data: dict[str, object]) -> None:
            self.bus.emit(type, run_id=ctx.run_id, stage=ctx.stage_id, agent=spec.name, data=data)

        return emit

    @staticmethod
    def _stamp(ctx: InvocationContext, name: str) -> int | None:
        path = ctx.artifacts.path(name)
        return path.stat().st_mtime_ns if path.is_file() else None

    def _save_transcript(
        self,
        spec: AgentSpec,
        ctx: InvocationContext,
        messages: list[Message],
        result: AgentResult,
        models_used: list[str],
    ) -> Path:
        directory = self.runs_dir / ctx.run_id / "transcripts"
        directory.mkdir(parents=True, exist_ok=True)
        n = 1
        while (path := directory / f"{ctx.stage_id}-{spec.name}-{n}.json").exists():
            n += 1
        doc = {
            "run_id": ctx.run_id,
            "stage": ctx.stage_id,
            "agent": spec.name,
            "attempt": ctx.attempt,
            "models": models_used,
            "status": result.status,
            "error": result.error,
            "iterations": result.iterations,
            "usage": asdict(result.usage),
            "cost_usd": result.cost_usd,
            "messages": [asdict(m) for m in messages],
        }
        path.write_text(
            json.dumps(redactor.redact_obj(doc), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path


class _Fail(Exception):
    pass
