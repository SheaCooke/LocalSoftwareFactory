"""Tool registry. Drop a module into factory/tools/ that uses `@tool(...)`; it is
imported and registered automatically.

    @tool("text.count", {"type": "object", "properties": {"text": {"type": "string"}},
                         "required": ["text"]}, required_isolation_level="L0")
    def count(ctx: ToolContext, args: dict[str, Any]) -> str:
        "Count words."
        return str(len(args["text"].split()))

An agent can call a tool only if it lists it in `tools:` *and* its isolation level
is at least the tool's `required_isolation_level`. Every other call is denied,
logged as a `permission.decision` event, and answered with an error message.
"""

from __future__ import annotations

import importlib
import pkgutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from factory.agents.spec import AgentSpec, IsolationLevel
from factory.core.artifacts import ArtifactStore
from factory.core.events import EventBus
from factory.providers.base import ToolCall, ToolSpec

ToolFunc = Callable[["ToolContext", dict[str, Any]], str]

_MAX_RESULT_CHARS = 60_000
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


class ToolError(Exception):
    """Expected failure; the message is returned to the model."""


class ToolConfigError(Exception):
    """An agent definition asks for a tool it cannot have."""


@dataclass
class ToolContext:
    run_id: str
    stage_id: str
    agent: AgentSpec
    artifacts: ArtifactStore
    bus: EventBus

    def deny(self, tool: str, target: str, reason: str) -> ToolError:
        """Log a permission denial and return the error to raise."""
        self.bus.emit(
            "permission.decision",
            run_id=self.run_id,
            stage=self.stage_id,
            agent=self.agent.name,
            data={"decision": "denied", "tool": tool, "target": target, "reason": reason},
        )
        return ToolError(f"permission denied: {reason}")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    level: IsolationLevel
    func: ToolFunc

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.schema)


_TOOLS: dict[str, Tool] = {}
_discovered = False


def tool(
    name: str,
    schema: dict[str, Any],
    required_isolation_level: IsolationLevel | str,
    *,
    description: str | None = None,
) -> Callable[[ToolFunc], ToolFunc]:
    level = (
        IsolationLevel[required_isolation_level]
        if isinstance(required_isolation_level, str)
        else required_isolation_level
    )

    def decorator(func: ToolFunc) -> ToolFunc:
        existing = _TOOLS.get(name)
        if existing is not None and existing.func.__qualname__ != func.__qualname__:
            raise ValueError(f"tool {name!r} is already registered by {existing.func.__module__}")
        doc = description or (func.__doc__ or "").strip() or name
        _TOOLS[name] = Tool(name, doc, schema, level, func)
        return func

    return decorator


def discover() -> None:
    global _discovered
    if _discovered:
        return
    import factory.tools as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name != "registry" and not info.name.startswith("_"):
            importlib.import_module(f"{pkg.__name__}.{info.name}")
    _discovered = True


def _check_args(schema: dict[str, Any], args: dict[str, Any]) -> None:
    """Minimal JSON-Schema check: required keys and top-level property types."""
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")
    for key, prop in (schema.get("properties") or {}).items():
        expected = _JSON_TYPES.get(str(prop.get("type")))
        value = args.get(key)
        if key in args and expected is not None:
            bad_bool = isinstance(value, bool) and prop.get("type") in ("integer", "number")
            if not isinstance(value, expected) or bad_bool:
                raise ToolError(f"argument {key!r} must be of type {prop['type']}")
    if "_raw" in args:
        raise ToolError("arguments were not valid JSON")


class ToolRegistry:
    def __init__(self) -> None:
        discover()

    @property
    def tools(self) -> dict[str, Tool]:
        return dict(_TOOLS)

    def get(self, name: str) -> Tool | None:
        return _TOOLS.get(name)

    def for_agent(self, spec: AgentSpec) -> dict[str, Tool]:
        """Tools the agent may use. Raises ToolConfigError for unknown/too-privileged tools."""
        allowed: dict[str, Tool] = {}
        for name in spec.tools:
            t = _TOOLS.get(name)
            if t is None:
                raise ToolConfigError(
                    f"agent {spec.name!r} lists unknown tool {name!r}; "
                    f"registered: {', '.join(sorted(_TOOLS))}"
                )
            if t.level > spec.level:
                raise ToolConfigError(
                    f"agent {spec.name!r} is {spec.isolation_level} but tool {name!r} "
                    f"requires {t.level.name}"
                )
            allowed[name] = t
        return allowed

    def execute(self, call: ToolCall, allowed: dict[str, Tool], ctx: ToolContext) -> str:
        started = time.perf_counter()
        ok = False
        try:
            t = allowed.get(call.name)
            if t is None:
                reason = (
                    f"tool {call.name!r} is not available to agent {ctx.agent.name!r}"
                    if call.name in _TOOLS
                    else f"unknown tool {call.name!r}"
                )
                raise ctx.deny(call.name, call.name, reason)
            _check_args(t.schema, call.arguments)
            result = t.func(ctx, call.arguments)
            ok = True
        except ToolError as exc:
            result = f"ERROR: {exc}"
        except Exception as exc:
            result = f"ERROR: tool {call.name!r} failed: {type(exc).__name__}: {exc}"
        if len(result) > _MAX_RESULT_CHARS:
            result = result[:_MAX_RESULT_CHARS] + f"\n... [truncated, {len(result)} chars total]"
        ctx.bus.emit(
            "tool.call",
            run_id=ctx.run_id,
            stage=ctx.stage_id,
            agent=ctx.agent.name,
            data={
                "tool": call.name,
                "call_id": call.id,
                "arguments": _preview(call.arguments),
                "ok": ok,
                "result_preview": result[:300],
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return result

    @staticmethod
    def specs(tools: Sequence[Tool]) -> list[ToolSpec]:
        return [t.spec for t in tools]


def _preview(args: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v) for k, v in args.items()
    }
