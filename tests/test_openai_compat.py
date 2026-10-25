from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from factory.config.schema import ProviderConfig
from factory.providers.base import Message, ProviderError, ToolCall, ToolSpec
from factory.providers.openai_compat import OpenAICompatProvider
from factory.secrets import redactor

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(
    tmp_path: Path, handler: Handler, api_key: str | None = "sk-test-123456", **cfg: Any
) -> OpenAICompatProvider:
    config = ProviderConfig(type="openai_compat", base_url="https://api.example/v1/", **cfg)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider(config, api_key, root=tmp_path, http_client=client)


def _ok(message: dict[str, Any], usage: dict[str, int] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-test-2026",
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


TOOL = ToolSpec(
    "fs.read", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}}
)


def test_request_shape_and_tool_round_trip(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fs__read", "arguments": '{"path": "src/a.py"}'},
                    }
                ],
            }
        )

    history = [
        Message.system("sys"),
        Message.user("read it"),
        Message.assistant("", [ToolCall("c0", "fs.read", {"path": "x"})]),
        Message.tool_result(ToolCall("c0", "fs.read", {}), "contents"),
    ]
    resp = _provider(tmp_path, handler).complete(
        history, [TOOL], model="gpt-test", temperature=0.2, metadata={"agent": "a"}
    )

    req = seen[0]
    assert str(req.url) == "https://api.example/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test-123456"
    body = json.loads(req.content)
    assert body["model"] == "gpt-test" and body["temperature"] == 0.2
    assert "metadata" not in body
    assert body["tools"][0]["function"]["name"] == "fs__read"
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "fs__read"
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "contents"}

    assert resp.tool_calls == (ToolCall("call_1", "fs.read", {"path": "src/a.py"}),)
    assert resp.usage.input_tokens == 11 and resp.usage.output_tokens == 7
    assert resp.model == "gpt-test-2026"


def test_azure_style_auth_and_query(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok({"content": "hi"})

    p = _provider(
        tmp_path, handler, auth_header="api-key", query_params={"api-version": "2024-10-21"}
    )
    assert p.complete([Message.user("x")], model="dep").content == "hi"
    assert seen[0].headers["api-key"] == "sk-test-123456"
    assert "authorization" not in seen[0].headers
    assert seen[0].url.params["api-version"] == "2024-10-21"


def test_no_key_sends_no_auth(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok({"content": "local"})

    _provider(tmp_path, handler, api_key=None).complete([Message.user("x")], model="llama")
    assert "authorization" not in seen[0].headers


def test_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("factory.providers.http.time.sleep", lambda s: None)
    statuses = iter([503, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return _ok({"content": "ok"}) if status == 200 else httpx.Response(status, text="busy")

    assert _provider(tmp_path, handler).complete([Message.user("x")], model="m").content == "ok"


def test_non_retryable_error_is_redacted(tmp_path: Path) -> None:
    redactor.add("sk-test-123456")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key sk-test-123456")

    with pytest.raises(ProviderError) as info:
        _provider(tmp_path, handler).complete([Message.user("x")], model="m")
    assert info.value.status == 401 and not info.value.retryable
    assert "sk-test-123456" not in str(info.value)
    assert "[REDACTED]" in str(info.value)


def test_stream_accumulates_text_tool_calls_and_usage(tmp_path: Path) -> None:
    events = [
        {"model": "m1", "choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_9",
                                "function": {"name": "fs__", "arguments": '{"pa'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "read", "arguments": 'th":"a"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
    ]
    sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    chunks = list(_provider(tmp_path, handler).stream([Message.user("x")], [TOOL], model="m"))
    assert seen[0]["stream"] is True
    assert "".join(c.text for c in chunks) == "Hello"
    final = chunks[-1].response
    assert final is not None
    assert final.content == "Hello"
    assert final.tool_calls == (ToolCall("call_9", "fs.read", {"path": "a"}),)
    assert final.usage.total_tokens == 8
    assert final.finish_reason == "tool_calls"
    assert final.model == "m1"


def test_unreachable_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("factory.providers.http.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError, match=r"cannot reach"):
        _provider(tmp_path, handler).complete([Message.user("x")], model="m")
