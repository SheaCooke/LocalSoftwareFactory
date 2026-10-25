"""Anthropic Messages API adapter (plain httpx, no SDK; see D7).

API version 2026-10-21 (`options.anthropic_version` overrides it). Notes:

- Sampling parameters (`temperature`, `top_p`, `top_k`) are rejected with a 400 on
  2026-10-21, so they are dropped with a warning (shared model `params` may carry them).
- Claude's assistant turns (thinking, text, tool_use and any other blocks) are kept as
  native content and sent back verbatim and append-only, as the API requires for
  thinking blocks in tool-use loops.
- Results of several tool calls go back in one user message (parallel tool use).
- Automatic prompt caching (top-level `cache_control`) is on unless
  `options.prompt_caching: false`; the agent loop resends a growing history, so the
  prefix is reused on every call.
- `stop_reason: refusal` maps to finish_reason "refusal". Anything else the API adds
  (fallback blocks, server tools) is kept in the native content and otherwise ignored.
"""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx

from factory.config.schema import ProviderConfig
from factory.providers import base
from factory.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    Response,
    StreamChunk,
    ToolCall,
    ToolNameCodec,
    ToolSpec,
    Usage,
)
from factory.providers.http import parse_arguments, post_json, stream_sse
from factory.providers.registry import register_provider

API_VERSION = "2026-10-21"
DEFAULT_MAX_TOKENS = 16_000  # non-streaming default; raise via model params
NATIVE_KEY = "anthropic"
_SAMPLING = ("temperature", "top_p", "top_k")
_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "refusal",
    "pause_turn": "pause",
}


@register_provider("anthropic")
class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        config: ProviderConfig,
        api_key: str | None = None,
        *,
        root: Path,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config, api_key, root=root)
        base_url = (config.base_url or "https://api.anthropic.com").rstrip("/")
        self._url = f"{base_url}/v1/messages"
        self._version = str(config.options.get("anthropic_version", API_VERSION))
        self._caching = config.options.get("prompt_caching", True) is not False
        self._client = http_client or base.http_client(config)

    # ------------------------------------------------------------------ request building

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self._version,
            **self.config.extra_headers,  # e.g. anthropic-beta
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    @staticmethod
    def _assistant_blocks(msg: Message, codec: ToolNameCodec) -> list[dict[str, Any]]:
        native = msg.native_for(NATIVE_KEY)
        if isinstance(native, list):
            # Claude's own turn, unchanged (thinking blocks and signatures included);
            # only response-side nulls are dropped.
            return [{k: v for k, v in b.items() if v is not None} for b in native]
        blocks: list[dict[str, Any]] = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        blocks += [
            {"type": "tool_use", "id": c.id, "name": codec.encode(c.name), "input": c.arguments}
            for c in msg.tool_calls
        ]
        return blocks or [{"type": "text", "text": "(no content)"}]

    def _messages(
        self, messages: Sequence[Message], codec: ToolNameCodec
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        def add(role: str, blocks: list[dict[str, Any]]) -> None:
            # Merge consecutive same-role turns (tool results of one round go together).
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            elif msg.role == "assistant":
                add("assistant", self._assistant_blocks(msg, codec))
            elif msg.role == "tool":
                add(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content,
                        }
                    ],
                )
            else:
                add("user", [{"type": "text", "text": msg.content}])
        return "\n\n".join(p for p in system_parts if p), out

    def _body(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        model: str,
        params: dict[str, Any],
        codec: ToolNameCodec,
    ) -> dict[str, Any]:
        params.pop("metadata", None)
        if self._version >= "2026-10-21":
            dropped = [k for k in _SAMPLING if params.pop(k, None) is not None]
            if dropped:
                warnings.warn(
                    f"Anthropic API {self._version} rejects {', '.join(dropped)}; dropped",
                    stacklevel=3,
                )
        if "stop" in params:
            stop = params.pop("stop")
            params["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
        system, turns = self._messages(messages, codec)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": int(params.pop("max_tokens", DEFAULT_MAX_TOKENS)),
            "messages": turns,
            **params,
        }
        if system:
            body["system"] = system
        if self._caching:
            body.setdefault("cache_control", {"type": "ephemeral"})
        if tools:
            body["tools"] = [
                {
                    "name": codec.encode(t.name),
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        return body

    # ----------------------------------------------------------------------- completion

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Response:
        codec = ToolNameCodec(tools)
        body = self._body(messages, tools, model, dict(params), codec)
        started = time.perf_counter()
        data = post_json(
            self._client,
            self._url,
            body=body,
            headers=self._headers(),
            params=self.config.query_params,
            max_retries=self.config.max_retries,
            label=self.type_name,
        )
        if not isinstance(data.get("content"), list):
            raise ProviderError(f"anthropic: unexpected response shape: {json.dumps(data)[:500]}")
        return self._response(
            data["content"],
            data.get("usage") or {},
            str(data.get("model") or model),
            data.get("stop_reason"),
            codec,
            (time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _usage(raw: dict[str, Any]) -> Usage:
        read = int(raw.get("cache_read_input_tokens") or 0)
        write = int(raw.get("cache_creation_input_tokens") or 0)
        # `input_tokens` excludes cached tokens on this API; our Usage includes them.
        return Usage(
            int(raw.get("input_tokens") or 0) + read + write,
            int(raw.get("output_tokens") or 0),
            cache_read_tokens=read,
            cache_write_tokens=write,
        )

    def _response(
        self,
        blocks: list[dict[str, Any]],
        usage: dict[str, Any],
        model: str,
        stop_reason: Any,
        codec: ToolNameCodec,
        latency_ms: float,
    ) -> Response:
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = tuple(
            ToolCall(
                id=str(b.get("id")),
                name=codec.decode(str(b.get("name"))),
                arguments=parse_arguments(b.get("input")),
            )
            for b in blocks
            if b.get("type") == "tool_use"
        )
        finish = _STOP_REASONS.get(str(stop_reason), str(stop_reason or "stop"))
        if calls and finish == "stop":
            finish = "tool_calls"
        return Response(
            content=text,
            tool_calls=calls,
            usage=self._usage(usage),
            model=model,
            finish_reason=finish,
            latency_ms=latency_ms,
            native=((NATIVE_KEY, blocks),),
        )

    # ------------------------------------------------------------------------ streaming

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Iterator[StreamChunk]:
        codec = ToolNameCodec(tools)
        body = self._body(messages, tools, model, dict(params), codec)
        body["stream"] = True
        started = time.perf_counter()
        blocks: dict[int, dict[str, Any]] = {}
        partial_json: dict[int, str] = {}
        usage: dict[str, Any] = {}
        stop_reason: Any = None
        served_model = model
        events = stream_sse(
            self._client,
            self._url,
            body=body,
            headers=self._headers(),
            params=self.config.query_params,
            label=self.type_name,
        )
        for name, event in events:
            kind = event.get("type") or name
            if kind == "message_start":
                message = event.get("message") or {}
                served_model = str(message.get("model") or served_model)
                usage.update(message.get("usage") or {})
            elif kind == "content_block_start":
                block = dict(event.get("content_block") or {})
                if block.get("type") == "tool_use":
                    block["input"] = {}
                blocks[int(event.get("index", len(blocks)))] = block
            elif kind == "content_block_delta":
                index = int(event.get("index", 0))
                block = blocks.setdefault(index, {"type": "text", "text": ""})
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    block["text"] = block.get("text", "") + delta.get("text", "")
                    if delta.get("text"):
                        yield StreamChunk(text=delta["text"])
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                elif dtype == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta.get("signature", "")
                elif dtype == "input_json_delta":
                    partial_json[index] = partial_json.get(index, "") + delta.get(
                        "partial_json", ""
                    )
            elif kind == "content_block_stop":
                index = int(event.get("index", 0))
                if index in partial_json and index in blocks:
                    blocks[index]["input"] = parse_arguments(partial_json.pop(index) or "{}")
            elif kind == "message_delta":
                stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                usage.update(event.get("usage") or {})
            elif kind == "error":
                err = event.get("error") or {}
                retryable = err.get("type") in ("overloaded_error", "api_error", "rate_limit_error")
                raise ProviderError(
                    f"anthropic stream error: {err.get('type')}: {err.get('message')}",
                    retryable=retryable,
                )
        ordered = [blocks[i] for i in sorted(blocks)]
        yield StreamChunk(
            response=self._response(
                ordered,
                usage,
                served_model,
                stop_reason,
                codec,
                (time.perf_counter() - started) * 1000,
            )
        )

    def close(self) -> None:
        self._client.close()
