"""Evaluate pipeline `when` / `pass_when` conditions against run artifacts."""

from __future__ import annotations

import json
import re
from typing import Any

from factory.core.artifacts import ArtifactStore
from factory.core.pipeline import Condition

_MISSING = object()
_FENCE = re.compile(r"\A\s*```[\w-]*\n(.*)\n```\s*\Z", re.DOTALL)


def load_json_artifact(store: ArtifactStore, name: str) -> Any:
    """Parse a JSON artifact, tolerating a surrounding ``` fence. Raises ValueError."""
    text = store.read_optional(name)
    if text is None:
        raise ValueError(f"artifact {name!r} does not exist")
    fenced = _FENCE.match(text)
    try:
        return json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"artifact {name!r} is not valid JSON: {exc}") from exc


def _lookup(doc: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(doc, dict):
            doc = doc.get(part, _MISSING)
        elif isinstance(doc, list) and part.isdigit() and int(part) < len(doc):
            doc = doc[int(part)]
        else:
            return _MISSING
        if doc is _MISSING:
            return _MISSING
    return doc


def evaluate(cond: Condition, store: ArtifactStore) -> tuple[bool, str]:
    """Return (holds, explanation)."""
    if cond.exists:
        ok = store.exists(cond.exists)
        return ok, f"{cond.describe()}: {'yes' if ok else 'no'}"
    assert cond.artifact is not None
    try:
        doc = load_json_artifact(store, cond.artifact)
    except ValueError as exc:
        return False, f"{cond.describe()}: {exc}"
    value = _lookup(doc, cond.path)
    if value is _MISSING:
        return False, f"{cond.describe()}: path {cond.path!r} not found"
    if "equals" in cond.model_fields_set:
        ok = value == cond.equals
    elif "not_equals" in cond.model_fields_set:
        ok = value != cond.not_equals
    else:
        ok = value in (cond.in_ or [])
    return ok, f"{cond.describe()}: actual {value!r}"


def evaluate_all(conds: list[Condition], store: ArtifactStore) -> tuple[bool, list[str]]:
    results = [evaluate(c, store) for c in conds]
    return all(ok for ok, _ in results), [why for ok, why in results if not ok]
