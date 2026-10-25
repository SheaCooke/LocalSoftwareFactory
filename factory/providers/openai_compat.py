"""OpenAI-compatible Chat Completions adapter (plain httpx, no SDK).

Covers OpenAI, Azure OpenAI (`auth_header: api-key` + `query_params.api-version`),
Ollama, LM Studio, vLLM, OpenRouter and most local servers.
"""

from __future__ import annotations

import json
import time
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
from factory.secrets import redactor


@register_provider("openai_compat")
class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        config: ProviderConfig,
        api_key: str | None = None,
        *,
        root: Path,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config, api_key, root=root)
        if not config.base_url:
            raise ProviderError("openai_compat provider requires base_url")
        self._url = config.base_url.rstrip("/") + "/chat/completions"
        self._client = http_client or base.http_client(config)

    # ------------------------------------------------------------------ request building

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.extra_headers}
        if self.api_key and self.config.auth_header == "authorization":
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.api_key and self.config.auth_header in ("api-key", "x-api-key"):
            headers[self.config.auth_header] = self.api_key
        return headers

    @staticmethod
    def _message(msg: Message, codec: ToolNameCodec) -> dict[str, Any]:
        if msg.role == "tool":
            return {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
        out: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.tool_calls:
            out["content"] = msg.content or None
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": codec.encode(c.name),
                        "arguments": json.dumps(c.arguments),
                    },
                }
                for c in msg.tool_calls
            ]
        return out

    def _body(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        model: str,
        params: dict[str, Any],
        codec: ToolNameCodec,
    ) -> dict[str, Any]:
        params.pop("metadata", None)
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._message(m, codec) for m in messages],
            **params,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": codec.encode(t.name),
                        "description": t.description,
                        "parameters": t.parameters,
                    },
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
        return self._parse(data, model, codec, (time.perf_counter() - started) * 1000)

    @staticmethod
    def _usage(raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        return Usage(int(raw.get("prompt_tokens") or 0), int(raw.get("completion_tokens") or 0))

    def _parse(
        self, data: dict[str, Any], model: str, codec: ToolNameCodec, latency_ms: float
    ) -> Response:
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError):
            raise ProviderError(
                redactor.redact(f"unexpected response shape: {json.dumps(data)[:500]}")
            ) from None
        calls = tuple(
            ToolCall(
                id=str(tc.get("id") or f"call_{i}"),
                name=codec.decode(tc["function"]["name"]),
                arguments=parse_arguments(tc["function"].get("arguments")),
            )
            for i, tc in enumerate(message.get("tool_calls") or ())
        )
        return Response(
            content=message.get("content") or "",
            tool_calls=calls,
            usage=self._usage(data.get("usage")),
            model=str(data.get("model") or model),
            finish_reason=str(choice.get("finish_reason") or ("tool_calls" if calls else "stop")),
            latency_ms=latency_ms,
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
        body.setdefault("stream_options", {"include_usage": True})
        started = time.perf_counter()
        text: list[str] = []
        partial: dict[int, dict[str, str]] = {}  # index -> {id, name, arguments}
        usage, finish, served_model = Usage(), "stop", model
        events = stream_sse(
            self._client,
            self._url,
            body=body,
            headers=self._headers(),
            params=self.config.query_params,
            label=self.type_name,
        )
        for _, event in events:
            served_model = str(event.get("model") or served_model)
            if event.get("usage"):
                usage = self._usage(event["usage"])
            for choice in event.get("choices") or ():
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                    yield StreamChunk(text=delta["content"])
                for tc in delta.get("tool_calls") or ():
                    slot = partial.setdefault(
                        int(tc.get("index", 0)), {"id": "", "name": "", "arguments": ""}
                    )
                    fn = tc.get("function") or {}
                    slot["id"] = tc.get("id") or slot["id"]
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
        calls = tuple(
            ToolCall(
                id=slot["id"] or f"call_{i}",
                name=codec.decode(slot["name"]),
                arguments=parse_arguments(slot["arguments"]),
            )
            for i, slot in sorted(partial.items())
        )
        yield StreamChunk(
            response=Response(
                content="".join(text),
                tool_calls=calls,
                usage=usage,
                model=served_model,
                finish_reason=finish,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        )

    def close(self) -> None:
        self._client.close()
