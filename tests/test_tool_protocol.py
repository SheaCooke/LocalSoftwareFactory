from __future__ import annotations

from factory.providers import tool_protocol
from factory.providers.base import Message, Response, ToolCall, ToolSpec

TOOLS = [ToolSpec("fs.read", "Read a file", {"type": "object"})]


def test_prepare_injects_protocol_and_flattens_history() -> None:
    call = ToolCall("c1", "fs.read", {"path": "a"})
    out = tool_protocol.prepare(
        [
            Message.system("You are X."),
            Message.user("go"),
            Message.assistant("", [call]),
            Message.tool_result(call, "file body"),
        ],
        TOOLS,
    )
    assert out[0].role == "system" and out[0].content.startswith("You are X.")
    assert "fs.read" in out[0].content and '"tool_calls"' in out[0].content
    assert out[2].role == "assistant" and not out[2].tool_calls
    assert '"fs.read"' in out[2].content
    assert out[3].role == "user" and "file body" in out[3].content


def test_prepare_adds_system_when_absent() -> None:
    out = tool_protocol.prepare([Message.user("hi")], TOOLS)
    assert out[0].role == "system" and len(out) == 2


def test_parse_plain_text_unchanged() -> None:
    resp = Response(content="All done {not json}")
    assert tool_protocol.parse(resp) is resp


def test_parse_fenced_json() -> None:
    text = '```json\n{"tool_calls": [{"name": "fs.read", "arguments": {"path": "b"}}]}\n```'
    parsed = tool_protocol.parse(Response(content=text))
    assert parsed.content == "" and parsed.finish_reason == "tool_calls"
    assert parsed.tool_calls[0].name == "fs.read"
    assert parsed.tool_calls[0].arguments == {"path": "b"}


def test_parse_json_with_preamble_and_bad_arguments() -> None:
    text = 'Sure! {"tool_calls": [{"name": "fs.read", "arguments": "oops"}, {"bad": 1}]}'
    parsed = tool_protocol.parse(Response(content=text))
    assert len(parsed.tool_calls) == 1 and parsed.tool_calls[0].arguments == {}
