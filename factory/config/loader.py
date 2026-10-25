"""Load config files into a Settings snapshot, and hold the live snapshot for hot reload.

Nothing caches a Settings object for long: callers read `LiveConfig.settings` at the
moment they need it (e.g. on every LLM call), so a reload is visible immediately.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from factory.config.schema import FactoryFile, ModelsFile, OverridesFile, Settings

CONFIG_DIR = "config"
MODELS_FILE = "models.yaml"
FACTORY_FILE = "factory.yaml"
OVERRIDES_FILE = "overrides.yaml"
ROOT_ENV_VAR = "FACTORY_ROOT"


class ConfigError(Exception):
    """A config file is missing, unparsable or invalid. The message is UI-ready."""

    def __init__(self, message: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path


def find_root(start: Path | None = None) -> Path:
    """Locate the factory root: $FACTORY_ROOT, else nearest ancestor with config/factory.yaml."""
    env = os.environ.get(ROOT_ENV_VAR)
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_DIR / FACTORY_FILE).is_file():
            return candidate
    # Fall back to the checkout this package lives in.
    return Path(__file__).resolve().parents[2]


def _read_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ConfigError(f"missing config file: {path}", path)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: YAML syntax error: {exc}", path) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping", path)
    return data


def _format_validation(name: str, exc: ValidationError) -> str:
    lines = [f"{name} is invalid:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_settings(root: Path) -> Settings:
    """Read and validate every config file under `root`. Raises ConfigError."""
    cfg = root / CONFIG_DIR
    parts: dict[str, Any] = {}
    for key, filename, model, required in (
        ("models", MODELS_FILE, ModelsFile, True),
        ("factory", FACTORY_FILE, FactoryFile, True),
        ("overrides", OVERRIDES_FILE, OverridesFile, False),
    ):
        path = cfg / filename
        raw = _read_yaml(path, required=required)
        try:
            parts[key] = model.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(_format_validation(filename, exc), path) from exc
    try:
        return Settings(root=root, **parts)
    except ValidationError as exc:
        raise ConfigError(_format_validation("config", exc)) from exc


Listener = Callable[[Settings, "ConfigError | None"], None]


class LiveConfig:
    """Thread-safe holder of the current Settings snapshot.

    `reload()` swaps the snapshot atomically. If the new files are invalid, the last
    good snapshot stays active and `error` describes the problem.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._stamps = self._file_stamps()
        self._settings = load_settings(root)
        self._error: ConfigError | None = None
        self._listeners: list[Listener] = []

    def _file_stamps(self) -> tuple[tuple[int, int] | None, ...]:
        stamps: list[tuple[int, int] | None] = []
        for name in (MODELS_FILE, FACTORY_FILE, OVERRIDES_FILE):
            try:
                st = (self.root / CONFIG_DIR / name).stat()
                stamps.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                stamps.append(None)
        return tuple(stamps)

    def refresh(self) -> bool:
        """Reload only if a config file changed on disk (cheap: three stat calls).

        Called before every model resolution, so edits made by another process (the
        CLI while a run is in progress, a text editor) apply to the next LLM call.
        Returns True if a reload was attempted.
        """
        stamps = self._file_stamps()
        with self._lock:
            if stamps == self._stamps:
                return False
        self.reload()
        return True

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    @property
    def error(self) -> ConfigError | None:
        with self._lock:
            return self._error

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def reload(self) -> bool:
        """Re-read config from disk. Returns True if the new config was applied."""
        stamps = self._file_stamps()
        try:
            new = load_settings(self.root)
        except ConfigError as exc:
            with self._lock:
                self._stamps = stamps
                self._error = exc
                current, listeners = self._settings, list(self._listeners)
            for listener in listeners:
                listener(current, exc)
            return False
        with self._lock:
            self._settings, self._error, self._stamps = new, None, stamps
            listeners = list(self._listeners)
        for listener in listeners:
            listener(new, None)
        return True

    # ----------------------------------------------------------------- runtime switching

    def set_model(self, alias: str, agent: str | None = None) -> None:
        """Select `alias` globally or for one agent. Takes effect on the next LLM call."""
        known = self.settings.models.models
        if alias not in known:
            raise ConfigError(
                f"unknown model alias {alias!r}; known aliases: {', '.join(sorted(known))}"
            )
        with self._lock:
            data = self._read_overrides()
            if agent:
                data.setdefault("agent_overrides", {})[agent] = alias
            else:
                data["default_model"] = alias
            self._write_overrides(data)
        self._reload_or_raise()

    def clear_model(self, agent: str | None = None, *, all_agents: bool = False) -> None:
        """Remove a runtime selection so factory.yaml / agent.md defaults apply again."""
        with self._lock:
            data = self._read_overrides()
            if all_agents:
                data = {}
            elif agent:
                data.get("agent_overrides", {}).pop(agent, None)
            else:
                data.pop("default_model", None)
            self._write_overrides(data)
        self._reload_or_raise()

    def _overrides_path(self) -> Path:
        return self.root / CONFIG_DIR / OVERRIDES_FILE

    def _read_overrides(self) -> dict[str, Any]:
        return _read_yaml(self._overrides_path(), required=False)

    def _write_overrides(self, data: dict[str, Any]) -> None:
        path = self._overrides_path()
        tmp = path.with_suffix(".yaml.tmp")
        header = "# Managed by the factory (UI model selector / `factory model use`).\n"
        tmp.write_text(header + yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)  # atomic on Windows and POSIX

    def _reload_or_raise(self) -> None:
        if not self.reload():
            err = self.error
            raise err if err else ConfigError("config reload failed")
