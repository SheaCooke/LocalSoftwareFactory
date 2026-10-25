from __future__ import annotations

from pathlib import Path

import pytest

from factory.secrets import REDACTED, Redactor, SecretStore


def test_env_beats_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("MY_KEY=from-dotenv-123\nOTHER=dotenv-only-456\n", "utf-8")
    monkeypatch.setenv("MY_KEY", "from-env-789")
    store = SecretStore(tmp_path, use_keyring=False)
    assert store.get("MY_KEY") == "from-env-789"
    assert store.get("OTHER") == "dotenv-only-456"
    assert store.get("MISSING") is None
    assert store.get(None) is None


def test_dotenv_reread_on_change(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("K=first-value\n", "utf-8")
    store = SecretStore(tmp_path, use_keyring=False)
    assert store.get("K") == "first-value"
    env.write_text("K=second-value\n", "utf-8")
    import os

    st = env.stat()
    os.utime(env, (st.st_atime, st.st_mtime + 5))
    assert store.get("K") == "second-value"


def test_redactor() -> None:
    r = Redactor()
    r.add("supersecretvalue")
    r.add("abc")  # too short: ignored
    assert r.redact("key=supersecretvalue abc") == f"key={REDACTED} abc"
    assert r.redact_obj({"a": ["supersecretvalue", 1], "b": ("x",)}) == {
        "a": [REDACTED, 1],
        "b": ["x"],
    }


def test_values_handed_out_are_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from factory.secrets import redactor

    monkeypatch.setenv("REG_KEY", "registered-secret-xyz")
    SecretStore(tmp_path, use_keyring=False).get("REG_KEY")
    assert "registered-secret-xyz" not in redactor.redact("leak: registered-secret-xyz")
