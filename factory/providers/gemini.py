"""Google Gemini adapter (Generative Language REST API, plain httpx).

`POST {base_url}/v1beta/models/{model}:generateContent` (and
`:streamGenerateContent?alt=sse`), key in the `x-goog-api-key` header.

Mapping notes:
- System messages become `systemInstruction`; assistant turns use role "model".
- Tool parameters are converted to Gemini's OpenAPI-style schema subset (unsupported
  JSON Schema keywords such as `additionalProperties` and `$schema` are dropped).
- Gemini function calls may carry no id; one is generated, and tool results are
  matched back by function name.
- The model's raw parts (including thought signatures) are kept as native content
  and sent back verbatim on the next turn.
- Neutral params map to `generationConfig` (temperature, top_p, top_k, max_tokens,
  stop); a `generation_config` dict in model params is merged in as-is.
"""

from __future__ import annotations

import itertools
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
from factory.providers.http import post_json, stream_sse
from factory.providers.registry import register_provider

NATIVE_KEY = "gemini"
_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "properties", "required",
    "items", "minItems", "maxItems", "minimum", "maximum", "anyOf", "title",
}  # fmt: skip
_GEN_CONFIG = {
    "temperature": "temperature",
    "top_p": "topP",
    "top_k": "topK",
    "max_tokens": "maxOutputTokens",
    "max_output_tokens": "maxOutputTokens",
    "candidate_count": "candidateCount",
}
_FINISH = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "refusal", "RECITATION": "refusal"}


def to_gemini_schema(schema: Any) -> Any:
    """JSON Schema -> Gemini Schema (uppercase types, supported keywords only)."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "type":
            if isinstance(value, list):  # ["string", "null"] -> STRING + nullable
                non_null = [v for v in value if v != "null"]
                out["nullable"] = len(non_null) != len(value) or out.get("nullable", False)
                value = non_null[0] if non_null else "string"
            out["type"] = str(value).upper()
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: to_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = to_gemini_schema(value)
        elif key == "anyOf" and isinstance(value, list):
            out["anyOf"] = [to_gemini_schema(v) for v in value]
        else:
            out[key] = value
    if out.get("type") == "OBJECT" and not out.get("properties"):
        out.pop("properties", None)
    return out


@register_provider("gemini")
class GeminiProvider(LLMProvider):
    def __init__(
        self,
        config: ProviderConfig,
        api_key: str | None = None,
        *,
        root: Path,
        http_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config, api_key, root=root)
        self._base = (config.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        self._version = str(config.options.get("api_version", "v1beta"))
        self._client = http_client or base.http_client(config)
        self._ids = itertools.count(1)

    def _url(self, model: str, stream: bool) -> str:
        method = "streamGenerateContent" if stream else "generateContent"
        return f"{self._base}/{self._version}/models/{model}:{method}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.extra_headers}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    # ------------------------------------------------------------------ request building

    def _contents(
        self, messages: Sequence[Message], codec: ToolNameCodec
    ) -> tuple[list[str], list[dict[str, Any]]]:
        system: list[str] = []
        contents: list[dict[str, Any]] = []

        def add(role: str, parts: list[dict[str, Any]]) -> None:
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": list(parts)})

        for msg in messages:
            if msg.role == "system":
                system.append(msg.content)
            elif msg.role == "assistant":
                native = msg.native_for(NATIVE_KEY)
                if isinstance(native, list):
                    add("model", native)
                    continue
                parts: list[dict[str, Any]] = []
                if msg.content:
                    parts.append({"text": msg.content})
                parts += [
                    {"functionCall": {"name": codec.encode(c.name), "args": c.arguments}}
                    for c in msg.tool_calls
                ]
                add("model", parts or [{"text": ""}])
            elif msg.role == "tool":
                add(
                    "user",
                    [
                        {
                            "functionResponse": {
                                "name": codec.encode(msg.name or ""),
                                "response": {"result": msg.content},
                            }
                        }
                    ],
                )
            else:
                add("user", [{"text": msg.content}])
        return [s for s in system if s], contents

    def _body(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: dict[str, Any],
        codec: ToolNameCodec,
    ) -> dict[str, Any]:
        params.pop("metadata", None)
        generation: dict[str, Any] = dict(params.pop("generation_config", {}) or {})
        for key, target in _GEN_CONFIG.items():
            if key in params:
                generation[target] = params.pop(key)
        if "stop" in params:
            stop = params.pop("stop")
            generation["stopSequences"] = [stop] if isinstance(stop, str) else list(stop)
        system, contents = self._contents(messages, codec)
        body: dict[str, Any] = {"contents": contents, **params}
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        if generation:
            body["generationConfig"] = generation
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": codec.encode(t.name),
                            "description": t.description,
                            **(
                                {"parameters": to_gemini_schema(t.parameters)}
                                if (t.parameters or {}).get("properties")
                                else {}
                            ),
                        }
                        for t in tools
                    ]
                }
            ]
        return body

    # -------------------------------------------------------------------- parsing

    @staticmethod
    def _usage(raw: dict[str, Any]) -> Usage:
        output = int(raw.get("candidatesTokenCount") or 0) + int(raw.get("thoughtsTokenCount") or 0)
        return Usage(
            int(raw.get("promptTokenCount") or 0),
            output,
            cache_read_tokens=int(raw.get("cachedContentTokenCount") or 0),
        )

    def _response(
        self,
        parts: list[dict[str, Any]],
        usage: dict[str, Any],
        finish: Any,
        model: str,
        codec: ToolNameCodec,
        latency_ms: float,
        *,
        blocked: str | None = None,
    ) -> Response:
        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
        calls = tuple(
            ToolCall(
                id=str(p["functionCall"].get("id") or f"gemini_{next(self._ids)}"),
                name=codec.decode(str(p["functionCall"].get("name"))),
                arguments=dict(p["functionCall"].get("args") or {}),
            )
            for p in parts
            if isinstance(p.get("functionCall"), dict)
        )
        reason = "refusal" if blocked else _FINISH.get(str(finish), str(finish or "stop").lower())
        if calls and reason == "stop":
            reason = "tool_calls"
        return Response(
            content=text,
            tool_calls=calls,
            usage=self._usage(usage),
            model=model,
            finish_reason=reason,
            latency_ms=latency_ms,
            native=((NATIVE_KEY, parts),) if parts else (),
        )

    @staticmethod
    def _candidate(data: dict[str, Any]) -> tuple[list[dict[str, Any]], Any, str | None]:
        feedback = data.get("promptFeedback") or {}
        blocked = feedback.get("blockReason")
        candidates = data.get("candidates") or []
        if not candidates:
            return [], None, blocked
        first = candidates[0]
        return (
            list((first.get("content") or {}).get("parts") or []),
            first.get("finishReason"),
            blocked,
        )

    # ----------------------------------------------------------------- completion

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Response:
        codec = ToolNameCodec(tools)
        body = self._body(messages, tools, dict(params), codec)
        started = time.perf_counter()
        data = post_json(
            self._client,
            self._url(model, stream=False),
            body=body,
            headers=self._headers(),
            params=self.config.query_params,
            max_retries=self.config.max_retries,
            label=self.type_name,
        )
        parts, finish, blocked = self._candidate(data)
        if not parts and not blocked and finish is None:
            raise ProviderError(f"gemini: unexpected response shape: {json.dumps(data)[:500]}")
        return self._response(
            parts,
            data.get("usageMetadata") or {},
            finish,
            str(data.get("modelVersion") or model),
            codec,
            (time.perf_counter() - started) * 1000,
            blocked=blocked,
        )

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Iterator[StreamChunk]:
        codec = ToolNameCodec(tools)
        body = self._body(messages, tools, dict(params), codec)
        started = time.perf_counter()
        parts: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        finish: Any = None
        blocked: str | None = None
        served = model
        events = stream_sse(
            self._client,
            self._url(model, stream=True),
            body=body,
            headers=self._headers(),
            params={**self.config.query_params, "alt": "sse"},
            label=self.type_name,
        )
        for _, chunk in events:
            new_parts, chunk_finish, chunk_blocked = self._candidate(chunk)
            served = str(chunk.get("modelVersion") or served)
            usage = chunk.get("usageMetadata") or usage
            finish = chunk_finish or finish
            blocked = chunk_blocked or blocked
            for part in new_parts:
                text = part.get("text")
                if text and not part.get("thought"):
                    yield StreamChunk(text=text)
                # Merge consecutive plain-text deltas so the native turn stays compact.
                if (
                    text is not None
                    and parts
                    and set(part) == {"text"}
                    and set(parts[-1]) == {"text"}
                ):
                    parts[-1] = {"text": parts[-1]["text"] + text}
                else:
                    parts.append(part)
        yield StreamChunk(
            response=self._response(
                parts,
                usage,
                finish,
                served,
                codec,
                (time.perf_counter() - started) * 1000,
                blocked=blocked,
            )
        )

    def close(self) -> None:
        self._client.close()
