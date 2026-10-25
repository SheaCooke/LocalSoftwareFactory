"""Filesystem tools. Every path goes through the agent's PathPolicy (fs_scope)."""

from __future__ import annotations

import os
from typing import Any

from factory.tools.registry import ToolContext, ToolError, tool

_MAX_READ_BYTES = 1_000_000
_MAX_WRITE_CHARS = 2_000_000
_MAX_LIST_ENTRIES = 2_000
_PATH = {"type": "string", "description": "Path relative to your workspace root, e.g. src/app.py"}


@tool(
    "fs.read",
    {
        "type": "object",
        "properties": {
            "path": _PATH,
            "offset": {"type": "integer", "description": "First line to return (1-based)"},
            "limit": {"type": "integer", "description": "Maximum number of lines"},
        },
        "required": ["path"],
    },
    required_isolation_level="L1",
)
def fs_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Read a UTF-8 text file in your workspace."""
    path = ctx.check_path("fs.read", args["path"], "read")
    if not path.is_file():
        raise ToolError(f"{args['path']!r} is not a file")
    if path.stat().st_size > _MAX_READ_BYTES:
        raise ToolError(f"{args['path']!r} is larger than {_MAX_READ_BYTES} bytes")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError(f"{args['path']!r} looks like a binary file")
    text = data.decode("utf-8", errors="replace")
    offset, limit = args.get("offset"), args.get("limit")
    if offset is None and limit is None:
        return text
    lines = text.splitlines(keepends=True)
    start = max(int(offset or 1), 1) - 1
    end = start + int(limit) if limit else len(lines)
    return "".join(lines[start:end])


@tool(
    "fs.write",
    {
        "type": "object",
        "properties": {"path": _PATH, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
    required_isolation_level="L2",
)
def fs_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Create or replace a text file in your workspace (parent folders are created)."""
    content: str = args["content"]
    if len(content) > _MAX_WRITE_CHARS:
        raise ToolError(f"content exceeds {_MAX_WRITE_CHARS} characters")
    path = ctx.check_path("fs.write", args["path"], "write")
    if path.is_dir():
        raise ToolError(f"{args['path']!r} is a directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.factory-tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    os.replace(tmp, path)
    return f"wrote {ctx.policy.display(path) if ctx.policy else path} ({len(content)} chars)"


@tool(
    "fs.list",
    {
        "type": "object",
        "properties": {
            "path": {**_PATH, "description": "Directory relative to your workspace (default '.')"},
            "recursive": {"type": "boolean"},
        },
    },
    required_isolation_level="L1",
)
def fs_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    """List files you are allowed to read. Directories end with '/'."""
    root = ctx.check_path("fs.list", args.get("path") or ".", "read")
    if not root.is_dir():
        raise ToolError(f"{args.get('path')!r} is not a directory")
    assert ctx.policy is not None
    entries = root.rglob("*") if args.get("recursive") else root.iterdir()
    shown: list[str] = []
    for entry in sorted(entries):
        if not ctx.policy.allows(entry, "read"):
            continue  # .git, denied and out-of-scope entries are invisible
        shown.append(ctx.policy.display(entry) + ("/" if entry.is_dir() else ""))
        if len(shown) >= _MAX_LIST_ENTRIES:
            shown.append(f"... (truncated at {_MAX_LIST_ENTRIES} entries)")
            break
    return "\n".join(shown) or "(empty)"
