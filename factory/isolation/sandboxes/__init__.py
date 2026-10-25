"""Pluggable sandbox backends for shell.run_sandboxed.

Built in: `docker` (full isolation) and `subprocess` (reduced isolation fallback).
A new backend (e.g. Windows Sandbox or WSL-native) is a module here that registers a
`SandboxBackend` subclass with `@register_sandbox("<name>")`; select it with
`sandbox.backend: <name>` in config/factory.yaml.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass

from factory.config.schema import SandboxConfig
from factory.isolation.sandboxes.base import (
    SandboxBackend,
    SandboxError,
    SandboxResult,
    backends,
    register_sandbox,
)

__all__ = [
    "SandboxBackend",
    "SandboxError",
    "SandboxResult",
    "SandboxSelection",
    "register_sandbox",
    "select_backend",
]


def _discover() -> None:
    for info in pkgutil.iter_modules(__path__):
        if info.name != "base":
            importlib.import_module(f"{__name__}.{info.name}")


@dataclass(frozen=True)
class SandboxSelection:
    backend: SandboxBackend
    reduced_isolation: bool
    reason: str | None  # why the preferred backend was not used

    def describe(self) -> str:
        if not self.reduced_isolation:
            return f"{self.backend.name} sandbox"
        return f"REDUCED ISOLATION ({self.backend.name}): {self.reason or 'by configuration'}"


def select_backend(config: SandboxConfig) -> SandboxSelection:
    """Pick the configured backend; `auto` prefers Docker and falls back to subprocess."""
    _discover()
    available = backends()
    if config.backend != "auto":
        cls = available.get(config.backend)
        if cls is None:
            names = ", ".join(sorted(available))
            raise SandboxError(f"unknown sandbox backend {config.backend!r}; available: {names}")
        reason = cls.availability(config)
        if reason:
            raise SandboxError(f"sandbox backend {config.backend!r} unavailable: {reason}")
        return SandboxSelection(cls(config), cls.reduced_isolation, None)
    docker = available["docker"]
    reason = docker.availability(config)
    if reason is None:
        return SandboxSelection(docker(config), False, None)
    fallback = available["subprocess"]
    return SandboxSelection(fallback(config), True, f"Docker unavailable: {reason}")
