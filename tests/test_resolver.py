from __future__ import annotations

import pytest
import yaml

from factory.config.loader import ConfigError, LiveConfig
from factory.providers.base import Message, ToolSpec
from factory.providers.fake import FakeProvider
from factory.providers.resolver import ModelResolver


@pytest.fixture
def resolver(live: LiveConfig) -> ModelResolver:
    return ModelResolver(live)


def test_precedence_chain(live: LiveConfig, resolver: ModelResolver) -> None:
    # 4. global default
    r = resolver.resolve(agent="planner")
    assert (r.alias, r.source) == ("echo", "global_default")
    # 3. agent.md default beats global
    r = resolver.resolve(agent="planner", agent_default="fast")
    assert (r.alias, r.source) == ("fast", "agent_default")
    # 2. per-agent override beats agent.md
    live.set_model("local-coder", agent="planner")
    r = resolver.resolve(agent="planner", agent_default="fast")
    assert (r.alias, r.source) == ("local-coder", "agent_override")
    # ...but only for that agent
    assert resolver.resolve(agent="docs", agent_default="fast").alias == "fast"
    # 1. per-run override beats everything
    r = resolver.resolve(agent="planner", agent_default="fast", run_override="smart")
    assert (r.alias, r.source) == ("smart", "run_override")


def test_overrides_file_beats_factory_yaml_agent_overrides(live: LiveConfig) -> None:
    path = live.root / "config" / "factory.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data["agent_overrides"] = {"planner": "fast"}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    live.reload()
    resolver = ModelResolver(live)
    assert resolver.resolve(agent="planner").alias == "fast"
    live.set_model("smart", agent="planner")
    assert resolver.resolve(agent="planner").alias == "smart"


def test_unknown_agent_default_is_clear_error(resolver: ModelResolver) -> None:
    with pytest.raises(ConfigError, match=r"agent 'x' agent default .*'nope'"):
        resolver.resolve(agent="x", agent_default="nope")


def test_switch_applies_to_next_call_without_restart(live: LiveConfig) -> None:
    models = live.root / "config" / "models.yaml"
    data = yaml.safe_load(models.read_text("utf-8"))
    data["models"]["echo-b"] = {"provider": "fake", "model": "echo-b"}
    models.write_text(yaml.safe_dump(data), encoding="utf-8")
    live.reload()

    resolver = ModelResolver(live)
    _, first = resolver.complete([Message.user("hi")], agent="implementer")
    assert first.content == "[echo] hi"
    live.set_model("echo-b", agent="implementer")
    _, second = resolver.complete([Message.user("hi")], agent="implementer")
    assert second.content == "[echo-b] hi"


def test_provider_client_is_cached_and_rebuilt_on_change(live: LiveConfig) -> None:
    resolver = ModelResolver(live)
    a = resolver.provider_for(resolver.resolve())
    assert resolver.provider_for(resolver.resolve()) is a
    models = live.root / "config" / "models.yaml"
    data = yaml.safe_load(models.read_text("utf-8"))
    data["providers"]["fake"]["options"] = {"tag": "v2"}
    models.write_text(yaml.safe_dump(data), encoding="utf-8")
    live.reload()
    assert resolver.provider_for(resolver.resolve()) is not a


def test_model_params_merge_call_params_win(live: LiveConfig) -> None:
    models = live.root / "config" / "models.yaml"
    data = yaml.safe_load(models.read_text("utf-8"))
    data["models"]["echo"]["params"] = {"temperature": 0.1, "top_p": 0.9}
    models.write_text(yaml.safe_dump(data), encoding="utf-8")
    live.reload()
    resolver = ModelResolver(live)
    resolver.complete([Message.user("x")], temperature=0.7)
    fake = resolver.provider_for(resolver.resolve())
    assert isinstance(fake, FakeProvider)
    assert fake.calls[-1].params == {"temperature": 0.7, "top_p": 0.9}


def test_json_tool_fallback_for_models_without_tools(live: LiveConfig) -> None:
    models = live.root / "config" / "models.yaml"
    data = yaml.safe_load(models.read_text("utf-8"))
    data["models"]["plain"] = {"provider": "fake", "model": "plain", "supports_tools": False}
    models.write_text(yaml.safe_dump(data), encoding="utf-8")
    live.reload()
    resolver = ModelResolver(live)
    fake = resolver.provider_for(resolver.resolve(run_override="plain"))
    assert isinstance(fake, FakeProvider)
    fake.add_script(
        [{"response": {"content": '{"tool_calls":[{"name":"fs.read","arguments":{"path":"a"}}]}'}}]
    )
    tool = ToolSpec("fs.read", "Read a file", {"type": "object"})
    _, response = resolver.complete(
        [Message.system("sys"), Message.user("go")], [tool], run_override="plain"
    )
    # The provider got no native tools; the protocol was put in the system prompt.
    assert fake.calls[-1].tools == []
    assert "fs.read" in fake.calls[-1].messages[0].content
    assert [(c.name, c.arguments) for c in response.tool_calls] == [("fs.read", {"path": "a"})]
