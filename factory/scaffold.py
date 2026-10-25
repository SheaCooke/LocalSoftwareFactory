"""`factory new ...`: create agents and pipelines from templates."""

from __future__ import annotations

import re
from pathlib import Path

from factory.agents.loader import AGENT_FILE, AgentParseError, parse_agent_file
from factory.core.pipeline import PipelineError, parse_pipeline

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
TEMPLATE_DIR = "_template"

_FALLBACK_AGENT = """\
---
name: _template
description: One line saying what this agent does
model: fast
isolation_level: L0
inputs: [request.md]
outputs: [example.md]
max_iterations: 10
max_tokens_budget: 100000
---
# Role
Describe the single job this agent owns.
"""

_STARTER_PIPELINE = """\
name: {name}
description: {description}
stages:
  - id: requirements
    agent: requirements
    gate: human_approval
  - id: plan
    agent: planner
"""


class ScaffoldError(ValueError):
    pass


def _check_name(kind: str, name: str) -> None:
    if not _NAME.match(name):
        raise ScaffoldError(
            f"{kind} name {name!r} must be lowercase letters, digits, '-' or '_' "
            "and start with a letter"
        )


def _set_field(text: str, key: str, value: str) -> str:
    """Replace the first top-level `key: ...` frontmatter line (keeping its comment)."""
    pattern = re.compile(rf"^{re.escape(key)}:[^\n#]*?(\s*#.*)?$", re.MULTILINE)
    if not pattern.search(text):
        raise ScaffoldError(f"template has no `{key}:` line")
    return pattern.sub(lambda m: f"{key}: {value}{m.group(1) or ''}", text, count=1)


def new_agent(
    agents_dir: Path,
    name: str,
    *,
    description: str | None = None,
    model: str | None = None,
) -> Path:
    """Copy agents/_template/agent.md to agents/<name>/agent.md and validate it."""
    _check_name("agent", name)
    target_dir = agents_dir / name
    if target_dir.exists():
        raise ScaffoldError(f"{target_dir} already exists")
    template = agents_dir / TEMPLATE_DIR / AGENT_FILE
    text = template.read_text("utf-8") if template.is_file() else _FALLBACK_AGENT
    text = _set_field(text, "name", name)
    if description:
        text = _set_field(text, "description", description.replace("\n", " "))
    if model:
        text = _set_field(text, "model", model)
    target_dir.mkdir(parents=True)
    path = target_dir / AGENT_FILE
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        parse_agent_file(path)
    except AgentParseError as exc:
        path.unlink()
        target_dir.rmdir()
        raise ScaffoldError(f"generated agent is invalid: {exc}") from exc
    return path


def new_pipeline(
    pipelines_dir: Path,
    name: str,
    *,
    copy_from: str | None = None,
    description: str | None = None,
) -> Path:
    """Create config/pipelines/<name>.yaml from an existing pipeline or a starter."""
    _check_name("pipeline", name)
    path = pipelines_dir / f"{name}.yaml"
    if path.exists():
        raise ScaffoldError(f"{path} already exists")
    if copy_from:
        source = pipelines_dir / f"{copy_from}.yaml"
        if not source.is_file():
            raise ScaffoldError(f"pipeline {copy_from!r} not found in {pipelines_dir}")
        text = _set_field(source.read_text("utf-8"), "name", name)
        if description:
            text = _set_field(text, "description", description)
    else:
        text = _STARTER_PIPELINE.format(name=name, description=description or "My pipeline")
    try:
        parse_pipeline(text, path.name)
    except PipelineError as exc:
        raise ScaffoldError(f"generated pipeline is invalid: {exc}") from exc
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
