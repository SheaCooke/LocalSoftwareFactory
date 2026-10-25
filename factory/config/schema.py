"""Pydantic models for config/models.yaml, config/factory.yaml and config/overrides.yaml."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- models.yaml


class ProviderConfig(_Strict):
    """A named endpoint. `type` selects the adapter registered in factory/providers/."""

    type: str
    base_url: str | None = None
    api_key_env: str | None = None
    auth_header: Literal["authorization", "api-key", "x-api-key", "none"] = "authorization"
    extra_headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 120.0
    max_retries: int = Field(default=2, ge=0, le=10)
    options: dict[str, Any] = Field(default_factory=dict)  # adapter-specific settings


class Pricing(_Strict):
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


class ModelConfig(_Strict):
    """A named alias: provider + model id + default params + capabilities."""

    provider: str
    model: str
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    supports_tools: bool = True
    supports_vision: bool = False
    context_window: int = Field(default=8192, gt=0)
    pricing: Pricing = Field(default_factory=Pricing)


class ModelsFile(_Strict):
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]

    @model_validator(mode="after")
    def _providers_exist(self) -> ModelsFile:
        for alias, model in self.models.items():
            if model.provider not in self.providers:
                known = ", ".join(sorted(self.providers)) or "(none)"
                raise ValueError(
                    f"model alias {alias!r} uses unknown provider {model.provider!r}; "
                    f"known providers: {known}"
                )
        return self


# -------------------------------------------------------------------------- factory.yaml


class UIConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, gt=0, lt=65536)
    open_browser: bool = True

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, host: str) -> str:
        if host == "localhost":
            return host
        try:
            if ipaddress.ip_address(host).is_loopback:
                return host
        except ValueError:
            pass
        raise ValueError(f"ui.host must be a loopback address (got {host!r})")


class PathsConfig(_Strict):
    agents: str = "agents"
    runs: str = "runs"
    workspaces: str = "workspaces"
    pipelines: str = "config/pipelines"


class LimitsConfig(_Strict):
    max_parallel_agents: int = Field(default=4, ge=1)


class SandboxConfig(_Strict):
    backend: Literal["auto", "docker", "subprocess"] = "auto"
    image: str = "python:3.12-slim"
    cpus: float = Field(default=2.0, gt=0)
    memory: str = "2g"
    pids_limit: int = Field(default=256, gt=0)
    timeout_s: int = Field(default=300, gt=0)
    command_allowlist: list[str] = Field(default_factory=list)


class BudgetConfig(_Strict):
    per_run_tokens: int | None = None
    per_run_cost_usd: float | None = None
    per_stage_tokens: int | None = None
    per_stage_cost_usd: float | None = None


class FactoryFile(_Strict):
    default_model: str
    agent_overrides: dict[str, str] = Field(default_factory=dict)
    ui: UIConfig = Field(default_factory=UIConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)


class OverridesFile(_Strict):
    """Machine-managed runtime selections (UI dropdown, `factory model use`)."""

    default_model: str | None = None
    agent_overrides: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------------------------ combined


class Settings(_Strict):
    """One immutable, validated snapshot of every config file."""

    root: Path
    models: ModelsFile
    factory: FactoryFile
    overrides: OverridesFile = Field(default_factory=OverridesFile)

    @model_validator(mode="after")
    def _aliases_exist(self) -> Settings:
        known = self.models.models
        refs: list[tuple[str, str]] = [("factory.yaml default_model", self.factory.default_model)]
        refs += [
            (f"factory.yaml agent_overrides.{a}", m)
            for a, m in self.factory.agent_overrides.items()
        ]
        if self.overrides.default_model:
            refs.append(("overrides.yaml default_model", self.overrides.default_model))
        refs += [
            (f"overrides.yaml agent_overrides.{a}", m)
            for a, m in self.overrides.agent_overrides.items()
        ]
        for where, alias in refs:
            if alias not in known:
                raise ValueError(
                    f"{where} refers to unknown model alias {alias!r}; "
                    f"known aliases: {', '.join(sorted(known))}"
                )
        return self

    @property
    def default_model(self) -> str:
        return self.overrides.default_model or self.factory.default_model

    @property
    def agent_overrides(self) -> dict[str, str]:
        return {**self.factory.agent_overrides, **self.overrides.agent_overrides}

    def path(self, name: Literal["agents", "runs", "workspaces", "pipelines"]) -> Path:
        return self.root / Path(getattr(self.factory.paths, name))
