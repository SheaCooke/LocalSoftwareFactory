"""Filesystem permission layer. Every file tool resolves paths through `PathPolicy`.

A path is allowed only if all of these hold:

1. It is syntactically safe: non-empty, no NUL, no `..` component, no UNC or device
   namespace (`\\\\server`, `\\\\?\\`), no NTFS alternate data stream (`a.py:evil`),
   no reserved device name (`CON`, `NUL`, `COM1` …), and no component ending in `.`
   or a space (Windows silently strips those, which would let `tests./x` alias `tests/x`).
2. After `Path.resolve()` (which follows symlinks and junctions), it is still inside
   the agent's workspace or the run's artifact directory.
3. It is not inside a `.git` directory and is not a `.env` file (always denied).
4. When it already exists as a file, it has no other hard links (a hard link could
   alias a file outside the scope).
5. It matches the agent's `fs_scope` globs: reads need a `read` match and no
   `deny_read` match; writes need a `write` match and no `deny` match.

Glob matching is case-insensitive on Windows because NTFS is.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from factory.agents.spec import FsScope

Mode = Literal["read", "write"]

_CASE_FLAGS = re.IGNORECASE if os.name == "nt" else 0
_DEVICE = re.compile(r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I)
_DRIVE = re.compile(r"^[A-Za-z]:$")
_PLACEHOLDER = re.compile(r"(\{workspace\}|\{artifacts\})")


class PermissionDenied(Exception):
    pass


def _translate(glob: str) -> str:
    """Glob → regex fragment. `**` spans directories, `*` and `?` do not."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("/**", i) and (i + 3 == len(glob) or glob[i + 3] == "/"):
            out.append("(?:/.*)?")  # "dir/**" also matches "dir" itself
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def compile_glob(pattern: str, roots: dict[str, Path]) -> re.Pattern[str]:
    """Compile a scope glob. Placeholders expand to (escaped) resolved roots;
    a pattern without a placeholder is relative to `{workspace}`."""
    pattern = pattern.replace("\\", "/")
    if not _PLACEHOLDER.match(pattern):
        pattern = "{workspace}/" + pattern.lstrip("/")
    parts: list[str] = []
    for piece in _PLACEHOLDER.split(pattern):
        if piece in ("{workspace}", "{artifacts}"):
            root = roots.get(piece[1:-1])
            # An unavailable root makes the pattern match nothing.
            parts.append(re.escape(root.as_posix()) if root else "(?!)")
        else:
            parts.append(_translate(piece))
    return re.compile("".join(parts) + r"\Z", _CASE_FLAGS)


class PathPolicy:
    def __init__(self, scope: FsScope, *, workspace: Path | None, artifacts: Path | None) -> None:
        self.workspace = workspace.resolve() if workspace else None
        self.artifacts = artifacts.resolve() if artifacts else None
        roots = {
            k: v for k, v in (("workspace", self.workspace), ("artifacts", self.artifacts)) if v
        }
        self._read = [compile_glob(p, roots) for p in scope.read]
        self._write = [compile_glob(p, roots) for p in scope.write]
        self._deny = [compile_glob(p, roots) for p in scope.deny]
        self._deny_read = [compile_glob(p, roots) for p in scope.deny_read]

    # ------------------------------------------------------------------------ public

    def check(self, raw: str, mode: Mode) -> Path:
        """Return the resolved path, or raise PermissionDenied with the reason."""
        resolved = self._resolve(raw)
        root = self._root_of(resolved)
        if root is None:
            raise PermissionDenied(f"{raw!r} is outside the agent's scope (resolves to {resolved})")
        rel = resolved.relative_to(root).parts
        if any(p.casefold() == ".git" for p in rel):
            raise PermissionDenied(f"{raw!r} is inside a .git directory")
        if resolved.name.casefold() == ".env" or resolved.name.casefold().startswith(".env."):
            raise PermissionDenied(f"{raw!r} is a secrets file")
        if resolved.is_file() and resolved.stat().st_nlink > 1:
            raise PermissionDenied(f"{raw!r} has multiple hard links")
        target = resolved.as_posix()
        # Deny is checked first: it wins over any allow glob.
        if mode == "read":
            if any(p.match(target) for p in self._deny_read):
                raise PermissionDenied(f"read of {raw!r} is denied by fs_scope.deny_read")
            if not any(p.match(target) for p in self._read):
                raise PermissionDenied(f"read of {raw!r} is not allowed by fs_scope.read")
        else:
            if any(p.match(target) for p in self._deny):
                raise PermissionDenied(f"write to {raw!r} is denied by fs_scope.deny")
            if not any(p.match(target) for p in self._write):
                raise PermissionDenied(f"write to {raw!r} is not allowed by fs_scope.write")
        return resolved

    def allows(self, raw: str | Path, mode: Mode) -> bool:
        try:
            self.check(str(raw), mode)
        except PermissionDenied:
            return False
        return True

    def display(self, path: Path) -> str:
        """Workspace-relative POSIX path for messages shown to the model."""
        if self.workspace and path.is_relative_to(self.workspace):
            return path.relative_to(self.workspace).as_posix() or "."
        return path.as_posix()

    # ---------------------------------------------------------------------- internals

    def _resolve(self, raw: str) -> Path:
        if not raw or "\x00" in raw:
            raise PermissionDenied("empty path or NUL character")
        text = raw.replace("\\", "/")
        if text.startswith("//"):
            raise PermissionDenied(f"{raw!r}: UNC and device-namespace paths are not allowed")
        components = [c for c in text.split("/") if c]
        if components and _DRIVE.match(components[0]):
            components = components[1:]
        for comp in components:
            if comp == "..":
                raise PermissionDenied(f"{raw!r}: path traversal ('..') is not allowed")
            if comp == ".":
                continue
            if ":" in comp:
                raise PermissionDenied(f"{raw!r}: ':' (alternate data streams) is not allowed")
            if comp.endswith((".", " ")):
                raise PermissionDenied(f"{raw!r}: components may not end with '.' or a space")
            if _DEVICE.match(comp):
                raise PermissionDenied(f"{raw!r}: reserved device name {comp!r}")
        path = Path(raw)
        if path.is_absolute():
            candidate = path
        elif path.anchor:  # "\\foo" or "C:foo": rooted or drive-relative, ambiguous
            raise PermissionDenied(f"{raw!r}: drive-relative or rooted paths are not allowed")
        elif self.workspace is None:
            raise PermissionDenied(f"{raw!r}: this agent has no workspace for relative paths")
        else:
            candidate = self.workspace / path
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:  # symlink loops, permission errors
            raise PermissionDenied(f"{raw!r} cannot be resolved: {exc}") from exc

    def _root_of(self, resolved: Path) -> Path | None:
        for root in (self.workspace, self.artifacts):
            if root is not None and resolved.is_relative_to(root):
                return root
        return None
