"""Structured-JSON tool protocol for models without native tool calling.

When a model alias has `supports_tools: false`, the resolver strips native tool
definitions, describes the tools in the system prompt instead, rewrites tool-call
history into plain text, and parses the model's JSON reply back into ToolCalls.
Callers see the same Response shape either way.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from factory.providers.base import Message, Response, ToolCall, ToolSpec

_INSTRUCTIONS = """\
## Tool protocol
You can call tools, but not through native function calling. To call one or more
tools, reply with ONLY a JSON object and nothing else:

{"tool_calls": [{"name": "<tool name>", "arguments": { ... }}]}

Tool results will come back in the next user message. When you are finished and
need no more tools, reply with your final answer as plain text (not JSON).

Available tools:
"""

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _render_tools(tools: Sequence[ToolSpec]) -> str:
    lines = [_INSTRUCTIONS]
    for tool in tools:
        schema = json.dumps(tool.parameters, separators=(",", ":"))
        lines.append(f"- {tool.name}: {tool.description}\n  arguments schema: {schema}")
    return "\n".join(lines)


def prepare(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> list[Message]:
    """Rewrite a conversation so a tool-less model can follow it."""
    block = _render_tools(tools)
    out: list[Message] = []
    injected = False
    for msg in messages:
        if msg.role == "system" and not injected:
            out.append(Message.system(f"{msg.content}\n\n{block}"))
            injected = True
        elif msg.role == "assistant" and msg.tool_calls:
            calls = [{"name": c.name, "arguments": c.arguments} for c in msg.tool_calls]
            out.append(Message.assistant(json.dumps({"tool_calls": calls})))
        elif msg.role == "tool":
            out.append(
                Message.user(f"Result of tool {msg.name} (call {msg.tool_call_id}):\n{msg.content}")
            )
        else:
            out.append(msg)
    if not injected:
        out.insert(0, Message.system(block))
    return out


def _extract_json(text: str) -> Any:
    text = text.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None
    return None


def parse(response: Response) -> Response:
    """Turn a JSON tool-call reply into native ToolCalls; leave plain answers alone."""
    payload = _extract_json(response.content)
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_calls"), list):
        return response
    calls: list[ToolCall] = []
    for raw in payload["tool_calls"]:
        if isinstance(raw, dict) and isinstance(raw.get("name"), str):
            args = raw.get("arguments")
            calls.append(
                ToolCall(
                    id=f"json_{uuid.uuid4().hex[:12]}",
                    name=raw["name"],
                    arguments=args if isinstance(args, dict) else {},
                )
            )
    if not calls:
        return response
    return dataclasses.replace(
        response, content="", tool_calls=tuple(calls), finish_reason="tool_calls"
    )
