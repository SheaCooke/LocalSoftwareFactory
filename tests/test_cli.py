from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from factory.cli import app

runner = CliRunner()


def test_prompt_echo(factory_root: Path) -> None:
    result = runner.invoke(app, ["prompt", "hello factory"])
    assert result.exit_code == 0, result.output
    assert "[echo] hello factory" in result.stdout
    assert "via global default" in result.stderr


def test_prompt_no_stream_with_override(factory_root: Path) -> None:
    result = runner.invoke(app, ["prompt", "hi", "--model", "echo", "--no-stream"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "[echo] hi"
    assert "via run override" in result.stderr


def test_model_use_then_prompt_uses_new_model(factory_root: Path) -> None:
    models = factory_root / "config" / "models.yaml"
    models.write_text(
        models.read_text("utf-8") + "\n  echo-b:\n    provider: fake\n    model: echo-b\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["model", "use", "echo-b", "--agent", "docs"]).exit_code == 0
    docs = runner.invoke(app, ["prompt", "x", "--agent", "docs"])
    assert "[echo-b] x" in docs.stdout and "via agent override" in docs.stderr
    other = runner.invoke(app, ["prompt", "x", "--agent", "planner"])
    assert "[echo] x" in other.stdout


def test_model_use_unknown_alias_fails(factory_root: Path) -> None:
    result = runner.invoke(app, ["model", "use", "nope"])
    assert result.exit_code == 1
    assert "unknown model alias 'nope'" in result.stderr


def test_models_list_marks_active(factory_root: Path) -> None:
    runner.invoke(app, ["model", "use", "local-coder"])
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 0
    active = [line for line in result.stdout.splitlines() if line.startswith("*")]
    assert len(active) == 1 and "local-coder" in active[0]
    assert "json-tools" in result.stdout  # local-plain has supports_tools: false
    runner.invoke(app, ["model", "reset"])
    assert runner.invoke(app, ["models", "list"]).stdout.count("* echo") == 1


def test_run_start_list_show_resume(factory_root: Path) -> None:
    result = runner.invoke(app, ["run", "start", "Build a todo CLI", "-p", "quick", "-m", "echo"])
    assert result.exit_code == 0, result.output
    assert "== stage plan: passed" in result.stdout
    run_id = result.stdout.split()[1]
    listing = runner.invoke(app, ["runs", "list"])
    assert run_id in listing.stdout and "passed" in listing.stdout
    shown = runner.invoke(app, ["runs", "show", run_id, "--events", "5"])
    assert shown.exit_code == 0, shown.output
    for text in ("requirements", "plan.md", "transcripts: 3", "total", "events:"):
        assert text in shown.stdout
    resumed = runner.invoke(app, ["runs", "resume", run_id, "-q"])
    assert resumed.exit_code == 0 and "passed" in resumed.stdout


def test_run_start_rejects_unsupported_pipeline(factory_root: Path) -> None:
    result = runner.invoke(app, ["run", "start", "x"])
    assert result.exit_code == 1 and "milestone 4" in result.stderr


def test_agents_and_pipelines_list(factory_root: Path) -> None:
    (factory_root / "agents" / "broken").mkdir()
    (factory_root / "agents" / "broken" / "agent.md").write_text("nope", "utf-8")
    agents = runner.invoke(app, ["agents", "list"])
    assert "implementer" in agents.stdout and "! broken" in agents.stdout
    pipelines = runner.invoke(app, ["pipelines", "list"])
    assert "quick" in pipelines.stdout and "default" in pipelines.stdout


def test_bad_root_reports_config_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["prompt", "x", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "config error: missing config file" in result.stderr


def test_missing_api_key_is_clear_error(factory_root: Path) -> None:
    result = runner.invoke(app, ["prompt", "x", "--model", "smart"])
    assert result.exit_code == 1
    assert "model 'smart' needs OPENAI_API_KEY" in result.stderr


def test_unimplemented_provider_type_is_clean_error(
    factory_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    result = runner.invoke(app, ["prompt", "x", "--model", "claude"])
    assert result.exit_code == 1
    assert "no adapter registered for provider type 'anthropic'" in result.stderr
