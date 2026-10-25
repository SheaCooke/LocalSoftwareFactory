from __future__ import annotations

from pathlib import Path

import pytest

from factory.agents.loader import AgentParseError, AgentRegistry, parse_agent_file
from factory.agents.spec import IsolationLevel

REPO_AGENTS = Path(__file__).resolve().parents[1] / "agents"
SHIPPED = {
    "requirements",
    "architect",
    "planner",
    "tester",
    "implementer",
    "reviewer",
    "security",
    "docs",
}


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name / "agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


VALID = """---
name: {name}
description: test agent
model: echo
isolation_level: L1
tools: [artifact.write]
outputs: [out.md]
---
# Role
Do the thing.
"""


def test_shipped_agents_all_load() -> None:
    reg = AgentRegistry(REPO_AGENTS)
    assert reg.errors == []
    assert set(reg.agents) == SHIPPED  # _template is skipped
    assert reg.agents["reviewer"].level == IsolationLevel.L1
    assert "{workspace}/tests/**" in reg.agents["implementer"].fs_scope.deny
    assert "{workspace}/src/**" in reg.agents["tester"].fs_scope.deny
    assert reg.agents["requirements"].system_prompt.startswith("# Role")


def test_crlf_and_bom_are_tolerated(tmp_path: Path) -> None:
    path = _write(tmp_path, "crlf", "﻿" + VALID.format(name="crlf").replace("\n", "\r\n"))
    spec = parse_agent_file(path)
    assert spec.name == "crlf" and spec.outputs == ["out.md"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# no frontmatter\n", "missing YAML frontmatter"),
        ("---\nname: [unclosed\n---\nbody\n", "frontmatter YAML error"),
        ("---\nname: other\ndescription: d\n---\nbody\n", "must match its folder name"),
        ("---\ndescription: d\n---\n\n", "system prompt"),
        ("---\ndescription: d\nbogus: 1\n---\nbody\n", "bogus"),
        ("---\ndescription: d\nisolation_level: L9\n---\nbody\n", "isolation_level"),
        ("---\ndescription: d\ntools: [fs.read]\n---\nbody\n", "L0 agents may not list tools"),
        (
            "---\ndescription: d\nrole: reviewer\nisolation_level: L2\ntools: [fs.write]\n---\nb\n",
            "read-only",
        ),
        (
            "---\ndescription: d\noutputs: ['../escape.md']\n---\nbody\n",
            "not a valid artifact name",
        ),
        ("---\ndescription: d\ninputs: ['*.md']\n---\nbody\n", "not a valid artifact name"),
    ],
)
def test_invalid_definitions(tmp_path: Path, text: str, message: str) -> None:
    path = _write(tmp_path, "bad", text)
    with pytest.raises(AgentParseError, match=message):
        parse_agent_file(path)


def test_registry_collects_errors_instead_of_raising(tmp_path: Path) -> None:
    _write(tmp_path, "good", VALID.format(name="good"))
    _write(tmp_path, "bad", "no frontmatter")
    reg = AgentRegistry(tmp_path)
    assert set(reg.agents) == {"good"}
    [err] = reg.errors
    assert err.name == "bad" and "frontmatter" in err.message
    assert reg.error_for("bad") is err


def test_registry_picks_up_new_changed_and_removed_agents(tmp_path: Path) -> None:
    good = _write(tmp_path, "good", VALID.format(name="good"))
    reg = AgentRegistry(tmp_path)
    _write(tmp_path, "fresh", VALID.format(name="fresh"))
    assert reg.get("fresh") is not None
    good.write_text(
        VALID.format(name="good").replace("Do the thing.", "Do it better now."), "utf-8"
    )
    spec = reg.get("good")
    assert spec is not None and "better" in spec.system_prompt
    good.write_text("broken", "utf-8")
    assert reg.get("good") is None and reg.error_for("good") is not None
    good.unlink()
    reg.reload()
    assert reg.error_for("good") is None


def test_template_is_a_valid_definition_once_named(tmp_path: Path) -> None:
    text = (
        (REPO_AGENTS / "_template" / "agent.md")
        .read_text("utf-8")
        .replace("name: _template", "name: copied")
    )
    spec = parse_agent_file(_write(tmp_path, "copied", text))
    assert spec.name == "copied"
