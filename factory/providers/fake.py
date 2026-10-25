"""Fake provider for tests and offline dry runs.

With no script it echoes the last user message, tagged with the model id, so you can
see which alias answered. With a script it returns canned responses in order, each
optionally matched on the calling agent (`metadata["agent"]`) and/or a substring of
the last user message.

Script file (YAML or JSON), set via `options.script` in models.yaml or passed in code:

    - match: { agent: planner, contains: "design.md" }
      response:
        content: "..."
        tool_calls: [{ name: artifact.write, arguments: { name: plan.md, content: "..." } }]
      repeat: false        # default false: consumed after one use
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from factory.config.schema import ProviderConfig
from factory.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    Response,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)
from factory.providers.registry import register_provider


@dataclass
class ScriptEntry:
    response: dict[str, Any]
    agent: str | None = None
    contains: str | None = None
    repeat: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScriptEntry:
        match = raw.get("match") or {}
        return cls(
            response=raw.get("response") or {},
            agent=match.get("agent"),
            contains=match.get("contains"),
            repeat=bool(raw.get("repeat", False)),
        )


@dataclass
class FakeCall:
    messages: list[Message]
    tools: list[ToolSpec]
    model: str
    params: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


def _last_user(messages: Sequence[Message]) -> str:
    return next((m.content for m in reversed(messages) if m.role in ("user", "tool")), "")


def _count_tokens(text: str) -> int:
    return max(1, len(text.split())) if text else 0


@register_provider("fake")
class FakeProvider(LLMProvider):
    def __init__(self, config: ProviderConfig, api_key: str | None = None, *, root: Path) -> None:
        super().__init__(config, api_key, root=root)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.calls: list[FakeCall] = []
        self.script: list[ScriptEntry] = []
        # A script named in models.yaml replays from the start for every run (keyed by
        # metadata["run"]), so a demo alias works for any number of runs in one process.
        self._per_run_template: list[ScriptEntry] = []
        self._per_run: dict[str, list[ScriptEntry]] = {}
        script = config.options.get("script")
        if script:
            self._per_run_template = self.read_script(root / Path(str(script)))

    @staticmethod
    def read_script(path: Path) -> list[ScriptEntry]:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        if not isinstance(raw, list):
            raise ProviderError(f"fake script {path} must be a list of entries")
        return [ScriptEntry.from_dict(e) for e in raw]

    def load_script(self, path: Path) -> None:
        """Append a script file to the shared (not per-run) queue."""
        self.add_script(self.read_script(path))

    def add_script(self, entries: Sequence[ScriptEntry | dict[str, Any]]) -> None:
        with self._lock:
            for entry in entries:
                self.script.append(
                    entry if isinstance(entry, ScriptEntry) else ScriptEntry.from_dict(entry)
                )

    @staticmethod
    def _take(queue: list[ScriptEntry], agent: str | None, prompt: str) -> dict[str, Any] | None:
        for i, entry in enumerate(queue):
            if entry.agent and entry.agent != agent:
                continue
            if entry.contains and entry.contains not in prompt:
                continue
            if not entry.repeat:
                del queue[i]
            return entry.response
        return None

    def _pick(
        self, agent: str | None, prompt: str, run: str | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            found = self._take(self.script, agent, prompt)
            if found is None and self._per_run_template:
                queue = self._per_run.setdefault(run or "", copy.deepcopy(self._per_run_template))
                found = self._take(queue, agent, prompt)
            return found

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Response:
        metadata = dict(params.pop("metadata", None) or {})
        self.calls.append(FakeCall(list(messages), list(tools or ()), model, params, metadata))
        prompt = _last_user(messages)
        scripted = self._pick(metadata.get("agent"), prompt, metadata.get("run"))
        calls: tuple[ToolCall, ...]
        if scripted is None:
            content, calls = f"[{model}] {prompt}", ()
        else:
            content = str(scripted.get("content", ""))
            calls = tuple(
                ToolCall(
                    id=str(c.get("id") or f"fake_{next(self._ids)}"),
                    name=str(c["name"]),
                    arguments=dict(c.get("arguments") or {}),
                )
                for c in scripted.get("tool_calls") or ()
            )
        usage = Usage(
            sum(_count_tokens(m.content) for m in messages), _count_tokens(content) + len(calls)
        )
        return Response(
            content=content,
            tool_calls=calls,
            usage=usage,
            model=model,
            finish_reason="tool_calls" if calls else "stop",
        )

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        model: str,
        **params: Any,
    ) -> Iterator[StreamChunk]:
        response = self.complete(messages, tools, model=model, **params)
        words = response.content.split(" ") if response.content else []
        for i, word in enumerate(words):
            yield StreamChunk(text=word if i == len(words) - 1 else word + " ")
        yield StreamChunk(response=response)
