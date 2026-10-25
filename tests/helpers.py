from __future__ import annotations

from pathlib import Path
from typing import Any

from factory.agents.context import InvocationContext
from factory.config.loader import LiveConfig
from factory.core.events import Event
from factory.core.orchestrator import Orchestrator
from factory.providers.fake import FakeProvider


def write_agent(root: Path, name: str, frontmatter: str, body: str = "# Role\nTest agent.") -> Path:
    path = root / "agents" / name / "agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: test\n{frontmatter}---\n{body}\n", "utf-8")
    return path


def make_orchestrator(root: Path) -> tuple[Orchestrator, FakeProvider, list[Event]]:
    orch = Orchestrator(LiveConfig(root))
    fake = orch.resolver.provider_for(orch.resolver.resolve(run_override="echo"))
    assert isinstance(fake, FakeProvider)
    events: list[Event] = []
    orch.bus.subscribe(events.append)
    return orch, fake, events


def context(
    orch: Orchestrator, run_id: str = "r1", stage: str = "s1", **kw: Any
) -> InvocationContext:
    return InvocationContext(
        run_id=run_id,
        stage_id=stage,
        request="Build a thing",
        artifacts=orch.artifacts(run_id),
        **kw,
    )


def of_type(events: list[Event], type: str) -> list[Event]:
    return [e for e in events if e.type == type]
