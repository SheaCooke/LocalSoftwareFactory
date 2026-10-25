from __future__ import annotations

from pathlib import Path

import yaml

from factory.config.schema import ProviderConfig
from factory.providers.base import Message
from factory.providers.fake import FakeProvider


def _fake(tmp_path: Path, **options: object) -> FakeProvider:
    return FakeProvider(ProviderConfig(type="fake", options=options), root=tmp_path)


def test_echo(tmp_path: Path) -> None:
    resp = _fake(tmp_path).complete([Message.user("hello there")], model="echo")
    assert resp.content == "[echo] hello there"
    assert resp.usage.input_tokens == 2 and resp.usage.output_tokens > 0


def test_script_matching_and_consumption(tmp_path: Path) -> None:
    fake = _fake(tmp_path)
    fake.add_script(
        [
            {"match": {"agent": "planner"}, "response": {"content": "plan"}},
            {"match": {"contains": "review"}, "response": {"content": "lgtm"}, "repeat": True},
            {
                "response": {
                    "tool_calls": [{"name": "fs.read", "arguments": {"path": "x.py"}}],
                }
            },
        ]
    )
    meta = {"agent": "reviewer"}
    assert (
        fake.complete([Message.user("please review")], model="m", metadata=meta).content == "lgtm"
    )
    assert (
        fake.complete([Message.user("please review")], model="m", metadata=meta).content == "lgtm"
    )
    tool_resp = fake.complete([Message.user("other")], model="m", metadata=meta)
    assert tool_resp.tool_calls[0].name == "fs.read"
    assert tool_resp.finish_reason == "tool_calls"
    planner = fake.complete([Message.user("x")], model="m", metadata={"agent": "planner"})
    assert planner.content == "plan"
    # Scripted entries used up -> echo.
    assert (
        fake.complete([Message.user("x")], model="m", metadata={"agent": "planner"}).content
        == "[m] x"
    )
    assert fake.calls[0].metadata == meta
    assert "metadata" not in fake.calls[0].params


def test_script_file_relative_to_root(tmp_path: Path) -> None:
    (tmp_path / "script.yaml").write_text(
        yaml.safe_dump([{"response": {"content": "from file"}}]), encoding="utf-8"
    )
    fake = _fake(tmp_path, script="script.yaml")
    assert fake.complete([Message.user("x")], model="m").content == "from file"


def test_stream_reassembles(tmp_path: Path) -> None:
    chunks = list(_fake(tmp_path).stream([Message.user("a b c")], model="echo"))
    assert "".join(c.text for c in chunks) == "[echo] a b c"
    final = chunks[-1].response
    assert final is not None and final.content == "[echo] a b c"
