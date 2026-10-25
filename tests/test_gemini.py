from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from factory.config.schema import ProviderConfig
from factory.providers.base import Message, ProviderError, ToolCall, ToolSpec
from factory.providers.gemini import GeminiProvider, to_gemini_schema

Handler = Callable[[httpx.Request], httpx.Response]
TOOL = ToolSpec(
    "fs.read",
    "Read a file",
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {"path": {"type": "string"}, "limit": {"type": ["integer", "null"]}},
        "required": ["path"],
    },
)


def make(tmp_path: Path, handler: Handler) -> GeminiProvider:
    config = ProviderConfig(type="gemini", api_key_env="GEMINI_API_KEY")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GeminiProvider(config, "gem-test-key-123", root=tmp_path, http_client=client)


def reply(parts: list[dict[str, Any]], finish: str = "STOP") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "usageMetadata": {
                "promptTokenCount": 9,
                "candidatesTokenCount": 4,
                "thoughtsTokenCount": 2,
            },
            "modelVersion": "gemini-2.5-pro",
        },
    )


def test_schema_conversion() -> None:
    out = to_gemini_schema(TOOL.parameters)
    assert out == {
        "type": "OBJECT",
        "properties": {"path": {"type": "STRING"}, "limit": {"type": "INTEGER", "nullable": True}},
        "required": ["path"],
    }


def test_request_shape_and_tool_round_trip(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        call = {"name": "fs__read", "args": {"path": "a.py"}}
        return reply(
            [
                {"text": "thinking", "thought": True},
                {"functionCall": call, "thoughtSignature": "s1"},
            ]
        )

    provider = make(tmp_path, handler)
    resp = provider.complete(
        [Message.system("be brief"), Message.user("go")],
        [TOOL],
        model="gemini-2.5-pro",
        temperature=0.1,
        max_tokens=100,
        stop=["END"],
        metadata={"agent": "x"},
    )
    req = seen[0]
    assert (
        str(req.url)
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
    )
    assert req.headers["x-goog-api-key"] == "gem-test-key-123"
    body = json.loads(req.content)
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert body["generationConfig"] == {
        "temperature": 0.1,
        "maxOutputTokens": 100,
        "stopSequences": ["END"],
    }
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "fs__read"
    assert "metadata" not in body
    assert resp.content == ""  # thought text is not answer text
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls[0].name == "fs.read" and resp.tool_calls[0].arguments == {"path": "a.py"}
    assert resp.usage.output_tokens == 6

    seen.clear()
    provider.complete(
        [
            Message.user("go"),
            resp.as_message(),
            Message.tool_result(resp.tool_calls[0], "contents"),
        ],
        [TOOL],
        model="gemini-2.5-pro",
    )
    contents = json.loads(seen[0].content)["contents"]
    assert contents[1]["role"] == "model" and contents[1]["parts"][1]["thoughtSignature"] == "s1"
    assert contents[2] == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "fs__read", "response": {"result": "contents"}}}],
    }


def test_assistant_without_native_and_safety(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    provider = make(tmp_path, lambda r: seen.append(r) or reply([{"text": "no"}], finish="SAFETY"))
    call = ToolCall("c1", "fs.read", {"path": "x"})
    resp = provider.complete(
        [Message.user("q"), Message.assistant("", [call]), Message.tool_result(call, "r")],
        [TOOL],
        model="m",
    )
    contents = json.loads(seen[0].content)["contents"]
    assert contents[1]["parts"] == [{"functionCall": {"name": "fs__read", "args": {"path": "x"}}}]
    assert resp.finish_reason == "refusal"


def test_blocked_prompt(tmp_path: Path) -> None:
    provider = make(
        tmp_path, lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    resp = provider.complete([Message.user("q")], model="m")
    assert resp.finish_reason == "refusal" and resp.content == ""


def test_errors_are_retried_then_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("factory.providers.http.time.sleep", lambda s: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    with pytest.raises(ProviderError) as info:
        make(tmp_path, handler).complete([Message.user("q")], model="m")
    assert info.value.status == 503 and len(calls) == 3  # first try + 2 retries


def test_stream(tmp_path: Path) -> None:
    chunks_json = [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hel"}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "lo"}]}}]},
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": "fs__read", "args": {"path": "q"}}}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5},
        },
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        text = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks_json)
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    out = list(make(tmp_path, handler).stream([Message.user("q")], [TOOL], model="m"))
    assert (
        seen[0].url.path.endswith(":streamGenerateContent") and seen[0].url.params["alt"] == "sse"
    )
    assert "".join(c.text for c in out) == "Hello"
    final = out[-1].response
    assert final is not None and final.content == "Hello"
    assert final.tool_calls[0].arguments == {"path": "q"}
    assert final.native[0][1][0] == {"text": "Hello"}  # text deltas merged into one part
    assert final.usage.total_tokens == 8
