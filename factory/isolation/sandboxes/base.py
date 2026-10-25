"""Sandbox backend interface. Add a backend by subclassing `SandboxBackend` in this
package and registering it with `@register_sandbox("<name>")`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from factory.config.schema import SandboxConfig

MAX_OUTPUT_CHARS = 100_000


class SandboxError(Exception):
    """The command was refused or the backend could not run it."""


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    backend: str

    def render(self) -> str:
        lines = [f"exit code: {self.exit_code}" + (" (timed out)" if self.timed_out else "")]
        if self.stdout:
            lines.append(f"--- stdout ---\n{self.stdout}")
        if self.stderr:
            lines.append(f"--- stderr ---\n{self.stderr}")
        return "\n".join(lines)


def cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n... [{len(text) - MAX_OUTPUT_CHARS} chars omitted] ...\n{text[-half:]}"


class SandboxBackend(ABC):
    name: ClassVar[str] = ""
    #: True when the backend cannot enforce no-network / filesystem confinement.
    reduced_isolation: ClassVar[bool] = False

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    @classmethod
    @abstractmethod
    def availability(cls, config: SandboxConfig) -> str | None:
        """None if usable, else the reason it is not."""

    @abstractmethod
    def run(
        self, command: str, *, workdir: Path, timeout_s: int | None = None, network: bool = False
    ) -> SandboxResult: ...


_BACKENDS: dict[str, type[SandboxBackend]] = {}


def register_sandbox(name: str) -> Callable[[type[SandboxBackend]], type[SandboxBackend]]:
    def decorator(cls: type[SandboxBackend]) -> type[SandboxBackend]:
        cls.name = name
        _BACKENDS[name] = cls
        return cls

    return decorator


def backends() -> dict[str, type[SandboxBackend]]:
    return dict(_BACKENDS)
