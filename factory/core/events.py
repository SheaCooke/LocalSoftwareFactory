"""In-process event bus and the per-run JSONL event log.

Event types (stable; the UI and tests depend on them):
    run.created  run.status  stage.transition  agent.started  agent.finished
    agent.message  llm.call  tool.call  permission.decision  artifact.written
    config.reloaded  config.error
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from factory.secrets import redactor


@dataclass(frozen=True)
class Event:
    type: str
    run_id: str | None = None
    stage: str | None = None
    agent: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(redactor.redact_obj(asdict(self)), ensure_ascii=False, default=str)


Subscriber = Callable[[Event], None]


class EventBus:
    """Synchronous fan-out. A failing subscriber never breaks the publisher."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            # Observability must never take the run down.
            with contextlib.suppress(Exception):
                fn(event)

    def emit(self, type: str, **kwargs: Any) -> Event:
        event = Event(type, **kwargs)
        self.publish(event)
        return event


class JsonlEventLog:
    """Appends every event that has a run_id to runs/<run_id>/events.jsonl (redacted)."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self._lock = threading.Lock()

    def path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "events.jsonl"

    def __call__(self, event: Event) -> None:
        if not event.run_id:
            return
        path = self.path(event.run_id)
        line = event.to_json() + "\n"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)

    def read(self, run_id: str) -> list[dict[str, Any]]:
        path = self.path(run_id)
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
