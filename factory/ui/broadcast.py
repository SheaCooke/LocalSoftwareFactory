"""Fan events out from the (threaded) orchestrator to Server-Sent-Events clients.

`publish` is an EventBus subscriber and may be called from any thread. Each SSE
client owns an asyncio queue on the server's event loop; events are handed over
with `call_soon_threadsafe`. A slow client whose queue fills up loses events
(its activity view is refreshed from the JSONL log on reconnect) instead of
blocking a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass

from factory.core.events import Event

QUEUE_SIZE = 2000
KEEPALIVE_S = 15.0


@dataclass(eq=False)
class _Client:
    queue: asyncio.Queue[str]
    loop: asyncio.AbstractEventLoop
    run_id: str | None


class Broadcaster:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[_Client] = set()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, event: Event) -> None:
        data = event.to_json()  # redacted
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            # Run-scoped clients get their run's events plus global ones (config, status).
            if client.run_id and event.run_id and event.run_id != client.run_id:
                continue
            with contextlib.suppress(RuntimeError):  # loop already closed
                client.loop.call_soon_threadsafe(self._offer, client.queue, data)

    @staticmethod
    def _offer(queue: asyncio.Queue[str], data: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(data)

    async def stream(self, run_id: str | None, limit: int | None = None) -> AsyncIterator[str]:
        """SSE frames for one client. `limit` ends the stream after N events (tests)."""
        client = _Client(asyncio.Queue(QUEUE_SIZE), asyncio.get_running_loop(), run_id)
        with self._lock:
            self._clients.add(client)
        sent = 0
        try:
            yield "retry: 2000\n\n"
            while limit is None or sent < limit:
                try:
                    data = await asyncio.wait_for(client.queue.get(), KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {data}\n\n"
                sent += 1
        finally:
            with self._lock:
                self._clients.discard(client)
