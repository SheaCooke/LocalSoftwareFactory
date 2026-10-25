"""Resolve which model an LLM call uses, at the moment of the call.

Precedence (first match wins):
    per-run override → per-agent override (overrides.yaml, then factory.yaml)
    → the agent's agent.md `model:` → global default_model

Because resolution reads `LiveConfig.settings` on every call, switching the model in
the UI, via `factory model use`, or by editing YAML applies to the very next call.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from factory.config.loader import ConfigError, LiveConfig
from factory.config.schema import ModelConfig, ProviderConfig
from factory.providers import tool_protocol
from factory.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    Response,
    StreamChunk,
    ToolSpec,
)
from factory.providers.registry import get_provider_class
from factory.secrets import SecretStore

Source = Literal["run_override", "agent_override", "agent_default", "global_default"]


@dataclass(frozen=True)
class ResolvedModel:
    alias: str
    source: Source
    provider_name: str
    provider: ProviderConfig
    model: ModelConfig

    @property
    def model_id(self) -> str:
        return self.model.model


class ModelResolver:
    def __init__(self, live: LiveConfig, secrets: SecretStore | None = None) -> None:
        self.live = live
        self.secrets = secrets or SecretStore(live.root)
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str], LLMProvider] = {}

    def resolve(
        self,
        *,
        agent: str | None = None,
        agent_default: str | None = None,
        run_override: str | None = None,
    ) -> ResolvedModel:
        self.live.refresh()
        settings = self.live.settings
        candidates: list[tuple[Source, str | None]] = [
            ("run_override", run_override),
            ("agent_override", settings.agent_overrides.get(agent) if agent else None),
            ("agent_default", agent_default),
            ("global_default", settings.default_model),
        ]
        source, alias = next((s, a) for s, a in candidates if a)
        assert alias is not None
        model = settings.models.models.get(alias)
        if model is None:
            where = f"agent {agent!r} " if agent else ""
            raise ConfigError(
                f"{where}{source.replace('_', ' ')} refers to unknown model alias {alias!r}; "
                f"known aliases: {', '.join(sorted(settings.models.models))}"
            )
        provider = settings.models.providers[model.provider]
        return ResolvedModel(alias, source, model.provider, provider, model)

    def provider_for(self, resolved: ResolvedModel) -> LLMProvider:
        """Cached adapter instance; rebuilt when the provider's config or key changes."""
        key_env = resolved.provider.api_key_env
        api_key = self.secrets.get(key_env)
        if key_env and not api_key:
            raise ProviderError(
                f"model {resolved.alias!r} needs {key_env}: set it in the environment, in .env, "
                f"or in Windows Credential Manager (service 'software-factory')"
            )
        fingerprint = hashlib.sha256(
            (resolved.provider.model_dump_json() + (api_key or "")).encode()
        ).hexdigest()
        key = (resolved.provider_name, fingerprint)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                # Drop (don't close) stale clients: a parallel call may still be using one.
                for stale in [k for k in self._clients if k[0] == resolved.provider_name]:
                    del self._clients[stale]
                cls = get_provider_class(resolved.provider.type)
                client = cls(resolved.provider, api_key, root=self.live.root)
                self._clients[key] = client
            return client

    def _prepare(
        self,
        resolved: ResolvedModel,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        params: dict[str, Any],
    ) -> tuple[Sequence[Message], Sequence[ToolSpec] | None, dict[str, Any], bool]:
        merged = {**resolved.model.params, **params}
        if tools and not resolved.model.supports_tools:
            return tool_protocol.prepare(messages, tools), None, merged, True
        return messages, tools, merged, False

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        agent: str | None = None,
        agent_default: str | None = None,
        run_override: str | None = None,
        **params: Any,
    ) -> tuple[ResolvedModel, Response]:
        resolved = self.resolve(agent=agent, agent_default=agent_default, run_override=run_override)
        msgs, wire_tools, merged, json_tools = self._prepare(resolved, messages, tools, params)
        response = self.provider_for(resolved).complete(
            msgs, wire_tools, model=resolved.model_id, **merged
        )
        return resolved, tool_protocol.parse(response) if json_tools else response

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        agent: str | None = None,
        agent_default: str | None = None,
        run_override: str | None = None,
        **params: Any,
    ) -> tuple[ResolvedModel, Iterator[StreamChunk]]:
        resolved = self.resolve(agent=agent, agent_default=agent_default, run_override=run_override)
        msgs, wire_tools, merged, json_tools = self._prepare(resolved, messages, tools, params)
        chunks = self.provider_for(resolved).stream(
            msgs, wire_tools, model=resolved.model_id, **merged
        )
        if not json_tools:
            return resolved, chunks

        def parsed() -> Iterator[StreamChunk]:
            # JSON tool replies cannot be shown incrementally; emit only the parsed result.
            for chunk in chunks:
                if chunk.response is not None:
                    final = tool_protocol.parse(chunk.response)
                    if final.content:
                        yield StreamChunk(text=final.content)
                    yield StreamChunk(response=final)

        return resolved, parsed()

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()
