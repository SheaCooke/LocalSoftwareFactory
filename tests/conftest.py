from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from factory.config.loader import LiveConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def factory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A throwaway factory root with copies of the repo's config/ and agents/."""
    shutil.copytree(
        REPO_ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns("overrides.yaml")
    )
    shutil.copytree(REPO_ROOT / "agents", tmp_path / "agents")
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path))
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path


@pytest.fixture
def live(factory_root: Path) -> LiveConfig:
    return LiveConfig(factory_root)
