from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from factory.config.schema import Pricing, ProviderConfig
from factory.providers.anthropic import API_VERSION, AnthropicProvider
from factory.providers.base import Message, ProviderError, ToolCall, ToolSpec

Handler = Callable[[httpx.Request], httpx.Response]
TOOL = ToolSpec(
    "fs.read", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}}
)


def make(tmp_path: Path, handler: Handler, **cfg: Any) -> AnthropicProvider:
    config = ProviderConfig(type="anthropic", api_key_env="ANTHROPIC_API_KEY", **cfg)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AnthropicProvider(config, "sk-ant-test-123456", root=tmp_path, http_client=client)


def reply(content: list[dict[str, Any]], stop: str = "end_turn", **usage: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop,
            "usage": {"input_tokens": 10, "output_tokens": 5, **usage},
        },
    )


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def test_request_shape_and_tool_round_trip(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(
            [THINKING, {"type": "text", "text": "Reading."},
             {"type": "tool_use", "id": "toolu_1", "name": "fs__read", "input": {"path": "a.py"}}],
            stop="tool_use",
        )  # fmt: skip

    provider = make(tmp_path, handler)
    with pytest.warns(UserWarning, match="rejects temperature"):
        resp = provider.complete(
            [Message.system("sys A"), Message.system("sys B"), Message.user("go")],
            [TOOL],
            model="claude-opus-5-5",
            temperature=0.2,
            stop="END",
            output_config={"effort": "high"},
            metadata={"agent": "x"},
        )
    req = seen[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-test-123456"
    assert req.headers["anthropic-version"] == API_VERSION == "2026-10-21"
    body = json.loads(req.content)
    assert "temperature" not in body and "metadata" not in body
    assert body["system"] == "sys A\n\nsys B"
    assert body["max_tokens"] == 16000 and body["stop_sequences"] == ["END"]
    assert body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["tools"] == [
        {"name": "fs__read", "description": "Read a file", "input_schema": TOOL.parameters}
    ]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "go"}]}]

    assert resp.content == "Reading." and resp.finish_reason == "tool_calls"
    assert resp.tool_calls == (ToolCall("toolu_1", "fs.read", {"path": "a.py"}),)

    # Next turn: Claude's blocks go back verbatim; parallel tool results share one message.
    seen.clear()
    second = ToolCall("toolu_2", "fs.read", {"path": "b.py"})
    history = [
        Message.user("go"),
        resp.as_message(),
        Message.tool_result(resp.tool_calls[0], "A"),
        Message.tool_result(second, "B"),
    ]
    provider.complete(history, [TOOL], model="claude-opus-5-5")
    turns = json.loads(seen[0].content)["messages"]
    assert turns[1]["role"] == "assistant" and turns[1]["content"][0] == THINKING
    assert turns[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "A"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "B"},
        ],
    }


def test_assistant_without_native_content(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    provider = make(tmp_path, lambda r: seen.append(r) or reply([{"type": "text", "text": "ok"}]))
    call = ToolCall("t1", "fs.read", {"path": "x"})
    provider.complete(
        [Message.user("q"), Message.assistant("hm", [call]), Message.tool_result(call, "r")],
        [TOOL],
        model="m",
    )
    turns = json.loads(seen[0].content)["messages"]
    assert turns[1]["content"] == [
        {"type": "text", "text": "hm"},
        {"type": "tool_use", "id": "t1", "name": "fs__read", "input": {"path": "x"}},
    ]


def test_usage_includes_cache_and_cost(tmp_path: Path) -> None:
    provider = make(
        tmp_path,
        lambda r: reply(
            [{"type": "text", "text": "x"}],
            cache_read_input_tokens=1000,
            cache_creation_input_tokens=100,
        ),
    )
    usage = provider.complete([Message.user("q")], model="m").usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        1110,
        1000,
        100,
    )
    cost = Pricing(input_per_mtok=4.0, output_per_mtok=20.0).cost(
        usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens
    )
    expected = (10 + 1000 * 0.1 + 100 * 1.25) * 4.0 / 1e6 + 5 * 20.0 / 1e6
    assert cost == pytest.approx(expected)


@pytest.mark.parametrize(
    ("stop", "finish"), [("refusal", "refusal"), ("max_tokens", "length"), ("end_turn", "stop")]
)
def test_stop_reasons(tmp_path: Path, stop: str, finish: str) -> None:
    provider = make(tmp_path, lambda r: reply([], stop=stop))
    assert provider.complete([Message.user("q")], model="m").finish_reason == finish


def test_legacy_version_keeps_sampling(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    provider = make(
        tmp_path,
        lambda r: seen.append(r) or reply([{"type": "text", "text": "x"}]),
        options={"anthropic_version": "2023-06-01", "prompt_caching": False},
    )
    provider.complete([Message.user("q")], model="m", temperature=0.3)
    body = json.loads(seen[0].content)
    assert body["temperature"] == 0.3 and "cache_control" not in body
    assert seen[0].headers["anthropic-version"] == "2023-06-01"


def test_overloaded_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("factory.providers.http.time.sleep", lambda s: None)
    statuses = iter([529, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        if next(statuses) == 529:
            return httpx.Response(
                529, json={"type": "error", "error": {"type": "overloaded_error"}}
            )
        return reply([{"type": "text", "text": "ok"}])

    assert make(tmp_path, handler).complete([Message.user("q")], model="m").content == "ok"


def test_bad_request_not_retried(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad"}})

    with pytest.raises(ProviderError) as info:
        make(tmp_path, handler).complete([Message.user("q")], model="m")
    assert info.value.status == 400 and len(calls) == 1


def sse(events: list[dict[str, Any]]) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def test_stream_accumulates_blocks(tmp_path: Path) -> None:
    events = [
        {
            "type": "message_start",
            "message": {
                "model": "claude-opus-5-5",
                "usage": {"input_tokens": 7, "cache_read_input_tokens": 3},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_9", "name": "fs__read", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"pa'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": 'th": "z"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 12},
        },
        {"type": "message_stop"},
    ]
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=sse(events), headers={"content-type": "text/event-stream"})

    chunks = list(make(tmp_path, handler).stream([Message.user("q")], [TOOL], model="m"))
    assert bodies[0]["stream"] is True
    assert "".join(c.text for c in chunks) == "Hello"
    final = chunks[-1].response
    assert final is not None
    assert final.tool_calls == (ToolCall("toolu_9", "fs.read", {"path": "z"}),)
    assert final.finish_reason == "tool_calls"
    assert (final.usage.input_tokens, final.usage.output_tokens, final.usage.cache_read_tokens) == (
        10,
        12,
        3,
    )
    native = final.native[0][1]
    assert native[0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert native[2]["input"] == {"path": "z"}


def test_stream_error_event(tmp_path: Path) -> None:
    events = [{"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}]
    provider = make(tmp_path, lambda r: httpx.Response(200, text=sse(events)))
    with pytest.raises(ProviderError, match="overloaded") as info:
        list(provider.stream([Message.user("q")], model="m"))
    assert info.value.retryable
