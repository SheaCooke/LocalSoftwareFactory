"""Milestone 6: scaffolding, hot reload, and each documented extension point
(docs/EXTENDING.md) working without touching orchestrator code."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import make_orchestrator, of_type
from typer.testing import CliRunner

import factory.tools as tools_pkg
from factory.cli import app
from factory.config.loader import LiveConfig
from factory.core.events import Event
from factory.core.watcher import FileWatcher
from factory.isolation.sandboxes.docker_backend import DockerSandbox
from factory.providers.base import LLMProvider, Message, Response, Usage
from factory.providers.registry import _REGISTRY, register_provider
from factory.scaffold import ScaffoldError, new_agent, new_pipeline
from factory.tools import registry as tool_registry
from factory.ui.service import FactoryService

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerSandbox, "availability", classmethod(lambda cls, c: "disabled"))


# ------------------------------------------------------------------- scaffolding


def test_new_agent_cli(factory_root: Path) -> None:
    result = runner.invoke(
        app, ["new", "agent", "changelog", "-d", "Writes CHANGELOG entries", "-m", "fast"]
    )
    assert result.exit_code == 0, result.output
    path = factory_root / "agents" / "changelog" / "agent.md"
    text = path.read_text("utf-8")
    assert "name: changelog" in text and "description: Writes CHANGELOG entries" in text
    assert "model: fast" in text and "# Role" in text
    listing = runner.invoke(app, ["agents", "list"])
    assert "changelog" in listing.stdout
    again = runner.invoke(app, ["new", "agent", "changelog"])
    assert again.exit_code == 1 and "already exists" in again.stderr
    bad_model = runner.invoke(app, ["new", "agent", "other", "-m", "nope"])
    assert bad_model.exit_code == 1 and "unknown model alias" in bad_model.stderr


@pytest.mark.parametrize("name", ["Bad", "1st", "a/b", "_hidden", "has space"])
def test_new_agent_rejects_bad_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ScaffoldError, match="must be lowercase"):
        new_agent(tmp_path, name)


def test_new_agent_without_template_uses_builtin(tmp_path: Path) -> None:
    path = new_agent(tmp_path, "solo", description="x")
    assert "name: solo" in path.read_text("utf-8")


def test_new_pipeline(factory_root: Path) -> None:
    pipelines = factory_root / "config" / "pipelines"
    copied = new_pipeline(pipelines, "mine", copy_from="default", description="Custom flow")
    text = copied.read_text("utf-8")
    assert text.startswith("name: mine") and "description: Custom flow" in text
    assert "goto: implement" in text  # whole stage graph copied
    starter = new_pipeline(pipelines, "tiny")
    assert "agent: planner" in starter.read_text("utf-8")
    with pytest.raises(ScaffoldError, match="not found"):
        new_pipeline(pipelines, "x", copy_from="nope")
    result = runner.invoke(app, ["new", "pipeline", "mine"])
    assert result.exit_code == 1 and "already exists" in result.stderr
    assert "mine" in runner.invoke(app, ["pipelines", "list"]).stdout


# -------------------------------------------------------------------- watcher


def test_file_watcher_detects_create_modify_delete(tmp_path: Path) -> None:
    seen: list[list[Path]] = []
    watcher = FileWatcher([(tmp_path, "*.yaml")], seen.append)
    a = tmp_path / "a.yaml"
    a.write_text("x: 1", "utf-8")
    assert watcher.check() == [a]
    assert watcher.check() == []  # nothing new
    a.write_text("x: 22", "utf-8")  # size changes even if mtime granularity is coarse
    assert watcher.check() == [a]
    a.unlink()
    assert watcher.check() == [a]
    (tmp_path / "ignored.txt").write_text("no", "utf-8")
    assert watcher.check() == []
    assert len(seen) == 3


def test_file_watcher_thread(tmp_path: Path) -> None:
    seen: list[list[Path]] = []
    watcher = FileWatcher([(tmp_path, "*.md")], seen.append, interval_s=0.05)
    watcher.start()
    try:
        (tmp_path / "x.md").write_text("hi", "utf-8")
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        watcher.stop()
    assert seen and seen[0][0].name == "x.md"


@pytest.fixture
def service(factory_root: Path) -> FactoryService:
    return FactoryService(LiveConfig(factory_root))


def _events(service: FactoryService) -> list[Event]:
    seen: list[Event] = []
    service.orch.bus.subscribe(seen.append)
    return seen


def test_hot_reload_config(service: FactoryService, factory_root: Path) -> None:
    events = _events(service)
    models = factory_root / "config" / "models.yaml"
    data = yaml.safe_load(models.read_text("utf-8"))
    data["models"]["hot"] = {"provider": "fake", "model": "hot-model"}
    models.write_text(yaml.safe_dump(data), "utf-8")
    service.watcher.check()
    assert "hot" in service.live.settings.models.models
    assert of_type(events, "config.reloaded")
    assert "hot" in [a["alias"] for a in service.models()["aliases"]]

    models.write_text("models: [broken", "utf-8")
    service.watcher.check()
    assert of_type(events, "config.error")
    assert "YAML syntax error" in str(service.status()["config_error"])
    assert "hot" in service.live.settings.models.models  # last good config stays active


def test_hot_reload_agents_and_pipelines(service: FactoryService, factory_root: Path) -> None:
    events = _events(service)
    new_agent(factory_root / "agents", "fresh", description="Fresh agent")
    service.watcher.check()
    [reloaded] = of_type(events, "agents.reloaded")
    assert "fresh" in reloaded.data["agents"] and reloaded.data["changed"] == ["fresh/agent.md"]
    assert "fresh" in [a["name"] for a in service.models()["agents"]]  # visible in the UI

    (factory_root / "agents" / "fresh" / "agent.md").write_text("broken", "utf-8")
    service.watcher.check()
    latest = of_type(events, "agents.reloaded")[-1]
    assert "fresh" in latest.data["errors"]
    assert [e["name"] for e in service.status()["agent_errors"]] == ["fresh"]

    pipeline = factory_root / "config" / "pipelines" / "quick.yaml"
    pipeline.write_text(
        "name: quick\nstages:\n  - {id: a, agent: x, on_fail: {goto: z}}\n", "utf-8"
    )
    service.watcher.check()
    errors = of_type(events, "pipelines.reloaded")[-1].data["errors"]
    assert "quick.yaml" in errors and "not found" in errors["quick.yaml"]
    assert any(e["file"] == "quick.yaml" for e in service.status()["pipeline_errors"])


# ------------------------------------------------- extension points end to end


def test_new_agent_and_stage_run_without_code_changes(factory_root: Path) -> None:
    """DoD: a new agent and a new stage, by editing only agents/ and config/."""
    new_agent(
        factory_root / "agents", "summarizer", description="Summarizes the plan", model="echo"
    )
    md = factory_root / "agents" / "summarizer" / "agent.md"
    md.write_text(
        md.read_text("utf-8").replace("outputs: [example.md]", "outputs: [summary.md]"), "utf-8"
    )
    pipeline = factory_root / "config" / "pipelines" / "quick.yaml"
    pipeline.write_text(
        pipeline.read_text("utf-8") + "  - id: summary\n    agent: summarizer\n", "utf-8"
    )
    orch, _, _ = make_orchestrator(factory_root)
    run = orch.run(orch.create_run("todo app", pipeline="quick", model_override="echo").id)
    assert run.status == "passed", run.error
    assert [s.stage_id for s in orch.store.stages(run.id)][-1] == "summary"
    assert orch.artifacts(run.id).exists("summary.md")


def test_dropped_in_tool_module_is_registered(factory_root: Path) -> None:
    module = Path(tools_pkg.__file__).parent / "zz_word_count.py"
    module.write_text(
        "from typing import Any\n"
        "from factory.tools.registry import ToolContext, tool\n\n"
        '@tool("text.word_count", {"type": "object", "properties": {"text": {"type": "string"}},'
        ' "required": ["text"]}, required_isolation_level="L1")\n'
        "def word_count(ctx: ToolContext, args: dict[str, Any]) -> str:\n"
        '    """Count words in a text."""\n'
        '    return str(len(args["text"].split()))\n',
        "utf-8",
    )
    try:
        tool_registry._discovered = False
        registry = tool_registry.ToolRegistry()
        assert "text.word_count" in registry.tools
        assert registry.tools["text.word_count"].description == "Count words in a text."
    finally:
        module.unlink()
        tool_registry._TOOLS.pop("text.word_count", None)


def test_custom_provider_via_models_yaml(factory_root: Path) -> None:
    @register_provider("shouty")
    class ShoutyProvider(LLMProvider):
        def complete(
            self, messages: Any, tools: Any = None, *, model: str, **params: Any
        ) -> Response:
            return Response(content=messages[-1].content.upper(), usage=Usage(1, 1), model=model)

    try:
        models = factory_root / "config" / "models.yaml"
        data = yaml.safe_load(models.read_text("utf-8"))
        data["providers"]["shout"] = {"type": "shouty"}
        data["models"]["loud"] = {"provider": "shout", "model": "v1"}
        models.write_text(yaml.safe_dump(data), "utf-8")
        result = runner.invoke(app, ["prompt", "hello", "-m", "loud", "--no-stream"])
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == "HELLO"
        orch, _, _ = make_orchestrator(factory_root)
        _, response = orch.resolver.complete([Message.user("x")], run_override="loud")
        assert response.content == "X"
    finally:
        _REGISTRY.pop("shouty", None)
