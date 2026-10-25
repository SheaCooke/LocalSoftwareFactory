"""Agent definition schema: the YAML frontmatter of agents/<name>/agent.md."""

from __future__ import annotations

import re
from enum import IntEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_ARTIFACT = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")

# Tools that change files. Read-only roles may never hold them (enforced again at runtime).
WRITE_TOOLS = frozenset({"fs.write", "fs.delete", "git.commit"})


class IsolationLevel(IntEnum):
    L0 = 0  # no tools: text in, artifact out
    L1 = 1  # read-only filesystem within scope
    L2 = 2  # read/write within its worktree + sandboxed commands
    L3 = 3  # L2 + networked sandbox commands, human-gated on first use per run


Role = Literal["general", "reviewer", "security", "tester", "implementer"]
READ_ONLY_ROLES: frozenset[str] = frozenset({"reviewer", "security"})


class FsScope(BaseModel):
    """Path globs with `{workspace}` / `{artifacts}` placeholders.

    `deny` beats `write` (an agent may read a denied path but never change it, e.g.
    the implementer reads but cannot edit tests). `deny_read` beats `read`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    model: str | None = None  # alias from models.yaml; None = global default
    role: Role = "general"
    isolation_level: Literal["L0", "L1", "L2", "L3"] = "L0"
    tools: list[str] = Field(default_factory=list)
    fs_scope: FsScope = Field(default_factory=FsScope)
    inputs: list[str] = Field(default_factory=list)  # artifact names
    outputs: list[str] = Field(default_factory=list)  # artifact names
    max_iterations: int = Field(default=20, ge=1, le=500)
    max_tokens_budget: int = Field(default=200_000, ge=1)

    # Not part of the frontmatter: filled in by the loader.
    system_prompt: str = ""
    path: Path | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, name: str) -> str:
        if not _NAME.match(name):
            raise ValueError(
                "must be lowercase letters, digits, '-' or '_' and start with a letter"
            )
        return name

    @field_validator("inputs", "outputs")
    @classmethod
    def _artifact_names(cls, names: list[str]) -> list[str]:
        for name in names:
            if not _ARTIFACT.match(name) or ".." in name.split("/"):
                raise ValueError(f"{name!r} is not a valid artifact name (no globs, '..' or '\\')")
        return names

    @model_validator(mode="after")
    def _consistent(self) -> AgentSpec:
        if self.level == IsolationLevel.L0 and self.tools:
            raise ValueError("isolation_level L0 agents may not list tools")
        if self.role in READ_ONLY_ROLES:
            writes = sorted(WRITE_TOOLS.intersection(self.tools))
            if writes or self.fs_scope.write:
                raise ValueError(
                    f"role {self.role!r} is read-only; remove {writes or 'fs_scope.write'}"
                )
        return self

    @property
    def level(self) -> IsolationLevel:
        return IsolationLevel[self.isolation_level]
