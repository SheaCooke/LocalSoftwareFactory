from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from factory.config.loader import ConfigError, LiveConfig, load_settings
from factory.config.schema import Settings


def _edit(path: Path, **changes: object) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_repo_config_loads(factory_root: Path) -> None:
    settings = load_settings(factory_root)
    assert settings.default_model == "echo"
    assert {"echo", "fast", "smart", "local-coder"} <= set(settings.models.models)
    assert settings.factory.ui.host == "127.0.0.1"
    assert settings.path("runs") == factory_root / "runs"


def test_missing_required_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"missing config file"):
        load_settings(tmp_path)


def test_unknown_provider_rejected(factory_root: Path) -> None:
    models = factory_root / "config" / "models.yaml"
    data = yaml.safe_load(models.read_text(encoding="utf-8"))
    data["models"]["broken"] = {"provider": "nope", "model": "x"}
    models.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"unknown provider 'nope'"):
        load_settings(factory_root)


def test_unknown_default_alias_rejected(factory_root: Path) -> None:
    _edit(factory_root / "config" / "factory.yaml", default_model="does-not-exist")
    with pytest.raises(ConfigError, match=r"unknown model alias 'does-not-exist'"):
        load_settings(factory_root)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_ui_must_bind_loopback(factory_root: Path, host: str) -> None:
    _edit(factory_root / "config" / "factory.yaml", ui={"host": host, "port": 8765})
    with pytest.raises(ConfigError, match=r"loopback"):
        load_settings(factory_root)


def test_unknown_keys_rejected(factory_root: Path) -> None:
    _edit(factory_root / "config" / "factory.yaml", defualt_model="typo")
    with pytest.raises(ConfigError, match=r"defualt_model"):
        load_settings(factory_root)


def test_reload_applies_valid_edit(live: LiveConfig) -> None:
    seen: list[str] = []
    live.subscribe(lambda s, err: seen.append(s.default_model))
    _edit(live.root / "config" / "factory.yaml", default_model="local-coder")
    assert live.reload() is True
    assert live.settings.default_model == "local-coder"
    assert live.error is None
    assert seen == ["local-coder"]


def test_reload_keeps_last_good_on_bad_edit(live: LiveConfig) -> None:
    before: Settings = live.settings
    (live.root / "config" / "models.yaml").write_text("models: [unclosed", encoding="utf-8")
    assert live.reload() is False
    assert live.settings is before
    assert live.error is not None and "YAML syntax error" in str(live.error)
    # Fixing the file clears the error.
    _edit(live.root / "config" / "factory.yaml", default_model="echo")
    (live.root / "config" / "models.yaml").write_text(
        yaml.safe_dump(before.models.model_dump()), encoding="utf-8"
    )
    assert live.reload() is True
    assert live.error is None


def test_set_model_writes_overrides(live: LiveConfig) -> None:
    live.set_model("smart")
    live.set_model("local-coder", agent="implementer")
    assert live.settings.default_model == "smart"
    assert live.settings.agent_overrides["implementer"] == "local-coder"
    overrides = yaml.safe_load((live.root / "config" / "overrides.yaml").read_text("utf-8"))
    assert overrides == {
        "default_model": "smart",
        "agent_overrides": {"implementer": "local-coder"},
    }
    # factory.yaml (human-owned, commented) is untouched.
    assert "default_model: echo" in (live.root / "config" / "factory.yaml").read_text("utf-8")


def test_set_model_rejects_unknown_alias(live: LiveConfig) -> None:
    with pytest.raises(ConfigError, match=r"unknown model alias 'gpt-17'"):
        live.set_model("gpt-17")
    assert not (live.root / "config" / "overrides.yaml").exists()


def test_clear_model(live: LiveConfig) -> None:
    live.set_model("smart")
    live.set_model("fast", agent="planner")
    live.clear_model(agent="planner")
    assert "planner" not in live.settings.agent_overrides
    assert live.settings.default_model == "smart"
    live.clear_model()
    assert live.settings.default_model == "echo"
    live.set_model("fast", agent="docs")
    live.clear_model(all_agents=True)
    assert live.settings.agent_overrides == {}
