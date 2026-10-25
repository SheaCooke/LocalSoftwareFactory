"""Per-run artifact store: the only channel through which agents communicate.

Artifacts live in runs/<run_id>/artifacts/. Names are relative, forward-slash paths
(e.g. "plan.md", "reports/lint.json"). Anything absolute, containing "..", or
resolving outside the store is rejected.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

_VALID = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")


class ArtifactError(ValueError):
    pass


class ArtifactStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        if not _VALID.match(name) or any(p in (".", "..") for p in name.split("/")):
            raise ArtifactError(f"invalid artifact name {name!r}")
        root = self.directory.resolve()
        target = (root / Path(*PurePosixPath(name).parts)).resolve()
        if not target.is_relative_to(root):
            raise ArtifactError(f"artifact {name!r} resolves outside the artifact store")
        return target

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def read(self, name: str) -> str:
        path = self.path(name)
        if not path.is_file():
            raise ArtifactError(f"artifact {name!r} does not exist")
        return path.read_text(encoding="utf-8")

    def read_optional(self, name: str) -> str | None:
        return self.read(name) if self.exists(name) else None

    def write(self, name: str, content: str) -> Path:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        return path

    def list(self) -> list[str]:
        root = self.directory.resolve()
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not p.name.endswith(".tmp")
        )
