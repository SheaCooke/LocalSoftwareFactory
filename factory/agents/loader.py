"""Discover and validate agent definitions under agents/.

Each folder `agents/<name>/` with an `agent.md` is one agent. Folders starting with
`_` (e.g. `_template`) are skipped. Invalid definitions never raise out of the
loader: they are collected as `AgentLoadError`s so the UI can show them.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from factory.agents.spec import AgentSpec

AGENT_FILE = "agent.md"
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class AgentLoadError:
    name: str
    path: Path
    message: str


class AgentParseError(Exception):
    pass


def parse_agent_file(path: Path) -> AgentSpec:
    """Parse one agent.md. Raises AgentParseError with a readable message."""
    text = path.read_text(encoding="utf-8").lstrip("﻿").replace("\r\n", "\n")
    match = _FRONTMATTER.match(text)
    if not match:
        raise AgentParseError("missing YAML frontmatter (file must start with a '---' block)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise AgentParseError(f"frontmatter YAML error: {exc}") from exc
    if not isinstance(meta, dict):
        raise AgentParseError("frontmatter must be a mapping")
    folder = path.parent.name
    if meta.get("name", folder) != folder:
        raise AgentParseError(f"name {meta.get('name')!r} must match its folder name {folder!r}")
    meta.setdefault("name", folder)
    body = match.group(2).strip()
    if not body:
        raise AgentParseError("system prompt (the Markdown body) is empty")
    try:
        return AgentSpec(**meta, system_prompt=body, path=path)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'frontmatter'}: {e['msg']}"
            for e in exc.errors()
        )
        raise AgentParseError(details) from exc


class AgentRegistry:
    """The current set of agents. `reload()` is cheap and re-reads only changed files."""

    def __init__(self, agents_dir: Path) -> None:
        self.agents_dir = agents_dir
        self._lock = threading.Lock()
        self._agents: dict[str, AgentSpec] = {}
        self._errors: dict[str, AgentLoadError] = {}
        self._stamps: dict[Path, tuple[int, int]] = {}
        self.reload()

    def reload(self) -> None:
        agents: dict[str, AgentSpec] = {}
        errors: dict[str, AgentLoadError] = {}
        stamps: dict[Path, tuple[int, int]] = {}
        files = sorted(self.agents_dir.glob(f"*/{AGENT_FILE}")) if self.agents_dir.is_dir() else []
        with self._lock:
            previous = {spec.path: spec for spec in self._agents.values()}
            prev_errors = {err.path: err for err in self._errors.values()}
            prev_stamps = dict(self._stamps)
        for path in files:
            name = path.parent.name
            if name.startswith(("_", ".")):
                continue
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            stamps[path] = stamp
            if prev_stamps.get(path) == stamp:
                if path in previous:
                    agents[name] = previous[path]
                    continue
                if path in prev_errors:
                    errors[name] = prev_errors[path]
                    continue
            try:
                agents[name] = parse_agent_file(path)
            except (AgentParseError, OSError, UnicodeDecodeError) as exc:
                errors[name] = AgentLoadError(name, path, str(exc))
        with self._lock:
            self._agents, self._errors, self._stamps = agents, errors, stamps

    @property
    def agents(self) -> dict[str, AgentSpec]:
        with self._lock:
            return dict(self._agents)

    @property
    def errors(self) -> list[AgentLoadError]:
        with self._lock:
            return list(self._errors.values())

    def get(self, name: str) -> AgentSpec | None:
        """Latest definition of `name` (re-reads changed files first)."""
        self.reload()
        with self._lock:
            return self._agents.get(name)

    def error_for(self, name: str) -> AgentLoadError | None:
        with self._lock:
            return self._errors.get(name)
