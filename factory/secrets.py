"""Provider key lookup and redaction.

Keys are looked up in order: process environment, `.env` in the factory root, then
Windows Credential Manager (or the platform keyring) if the optional `keyring`
package is installed. Every value handed out is registered with the global
`redactor`, which must be applied to anything written to logs, events or transcripts.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

KEYRING_SERVICE = "software-factory"
REDACTED = "[REDACTED]"
_MIN_SECRET_LEN = 6  # shorter values would cause false-positive redactions


class Redactor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= _MIN_SECRET_LEN:
            with self._lock:
                self._secrets.add(secret)

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def redact_obj(self, obj: Any) -> Any:
        """Recursively redact strings inside dicts/lists/tuples (for JSON events)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, Mapping):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.redact_obj(v) for v in obj]
        return obj


redactor = Redactor()


class SecretStore:
    def __init__(self, root: Path, *, use_keyring: bool = True) -> None:
        self._env_file = root / ".env"
        self._use_keyring = use_keyring
        self._dotenv: dict[str, str] = {}
        self._dotenv_mtime: float | None = None

    def get(self, name: str | None) -> str | None:
        if not name:
            return None
        value = os.environ.get(name) or self._from_dotenv(name) or self._from_keyring(name)
        redactor.add(value)
        return value

    def _from_dotenv(self, name: str) -> str | None:
        try:
            mtime = self._env_file.stat().st_mtime
        except FileNotFoundError:
            return None
        if mtime != self._dotenv_mtime:  # re-read on change so key edits need no restart
            self._dotenv = {k: v for k, v in dotenv_values(self._env_file).items() if v}
            self._dotenv_mtime = mtime
        return self._dotenv.get(name)

    def _from_keyring(self, name: str) -> str | None:
        if not self._use_keyring:
            return None
        try:
            import keyring
        except ImportError:
            return None
        try:
            value = keyring.get_password(KEYRING_SERVICE, name)
        except Exception:  # backend unavailable: treat as "not stored"
            return None
        return value if isinstance(value, str) and value else None
