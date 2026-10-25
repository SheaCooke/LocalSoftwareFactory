"""Polling file watcher for hot reload of config, agents and pipelines.

Polls `stat()` (mtime + size) once a second instead of using inotify/watchdog: no
dependency, identical behaviour on Windows, Linux and WSL (inotify gets no events
for files on /mnt/c), and the watched set is small. Callbacks receive the changed
paths (created, modified or deleted).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

Stamp = tuple[int, int]
Callback = Callable[[list[Path]], None]


class FileWatcher:
    def __init__(
        self,
        targets: Iterable[tuple[Path, str]],
        callback: Callback,
        *,
        interval_s: float = 1.0,
    ) -> None:
        """`targets` are (directory, glob) pairs, e.g. (agents_dir, "*/agent.md")."""
        self.targets = list(targets)
        self.callback = callback
        self.interval_s = interval_s
        self._snapshot = self._scan()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _scan(self) -> dict[Path, Stamp]:
        found: dict[Path, Stamp] = {}
        for directory, pattern in self.targets:
            if not directory.is_dir():
                continue
            for path in directory.glob(pattern):
                with contextlib.suppress(OSError):  # deleted between glob and stat
                    st = path.stat()
                    if path.is_file():
                        found[path] = (st.st_mtime_ns, st.st_size)
        return found

    def check(self) -> list[Path]:
        """Rescan now; call back and return the changed paths (if any)."""
        with self._lock:
            current = self._scan()
            changed = sorted(
                {
                    p
                    for p in current.keys() | self._snapshot.keys()
                    if current.get(p) != self._snapshot.get(p)
                }
            )
            self._snapshot = current
        if changed:
            self.callback(changed)
        return changed

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="file-watcher", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            with contextlib.suppress(Exception):  # a bad callback must not kill the watcher
                self.check()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
