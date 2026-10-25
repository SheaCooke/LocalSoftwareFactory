"""The single LLM interface every adapter implements, plus normalized message types.

Adapters translate between these types and their wire format. Nothing outside
factory/providers/ ever sees a vendor-specific payload.
"""

from __future__ import annotations

import re
import ssl
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
import truststore

from factory.config.schema import ProviderConfig

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # set on role="tool"
    name: str | None = None  # tool name on role="tool"

    @classmethod
    def system(cls, content: str) -> Message:
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Sequence[ToolCall] = ()) -> Message:
        return cls("assistant", content, tuple(tool_calls))

    @classmethod
    def tool_result(cls, call: ToolCall, content: str) -> Message:
        return cls("tool", content, tool_call_id=call.id, name=call.name)


@dataclass(frozen=True)
class ToolSpec:
    name: str  # factory name, e.g. "fs.read"
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )  # JSON Schema


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True)
class Response:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = "stop"
    latency_ms: float = 0.0

    def as_message(self) -> Message:
        return Message.assistant(self.content, self.tool_calls)


@dataclass(frozen=True)
class StreamChunk:
    """A text delta; the final chunk carries the complete `response` (with usage)."""

    text: str = ""
    response: Response | None = None


class ProviderError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMProvider(ABC):
    """One instance per configured provider endpoint; the model id is chosen per call.

    `complete`/`stream` accept provider-neutral params (temperature, max_tokens, top_p,
    stop, ...) and an optional `metadata` mapping (agent/stage names) that adapters
    must not send to the wire. Adapters ignore params they do not understand only if
    their API does; otherwise they forward them unchanged.
    """

    type_name: ClassVar[str] = ""

    def __init__(self, config: ProviderConfig, api_key: str | None = None, *, root: Path) -> None:
        self.config = config
        self.api_key = api_key
        self.root = root

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Response: ...

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Iterator[StreamChunk]:
        """Default: no incremental output; adapters override when the API supports it."""
        response = self.complete(messages, tools, model=model, **params)
        if response.content:
            yield StreamChunk(text=response.content)
        yield StreamChunk(response=response)

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


def http_client(config: ProviderConfig) -> httpx.Client:
    """httpx client that trusts the OS certificate store (Windows cert store on Windows).

    Corporate proxies and antivirus TLS inspection install their root CA there, not in
    certifi's bundle. `options.verify_tls: false` disables verification (local servers only).
    """
    verify: ssl.SSLContext | bool = config.options.get("verify_tls", True) is not False
    if verify:
        verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.Client(timeout=config.timeout_s, verify=verify)


# ------------------------------------------------------------------ tool name encoding

_WIRE_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class ToolNameCodec:
    """Maps factory tool names ("fs.read") to wire-safe names ("fs__read") and back.

    OpenAI, Anthropic and Gemini all restrict function names to [A-Za-z0-9_-].
    """

    def __init__(self, tools: Sequence[ToolSpec] | None) -> None:
        self._to_wire: dict[str, str] = {}
        self._from_wire: dict[str, str] = {}
        for tool in tools or ():
            wire = _WIRE_SAFE.sub("_", tool.name.replace(".", "__"))[:64]
            self._to_wire[tool.name] = wire
            self._from_wire[wire] = tool.name

    def encode(self, name: str) -> str:
        return self._to_wire.get(name, _WIRE_SAFE.sub("_", name.replace(".", "__")))

    def decode(self, wire: str) -> str:
        return self._from_wire.get(wire, wire)
