"""
bbs/transport/web.py — Synthetic writer for web-terminal sessions.

Web sessions are not initiated by a listening Transport; instead the engine
creates them on demand when a sysop connects via the browser xterm.js UI.
The actual bytes flow through a threading.Queue so that the asyncio BBS world
and the Flask-SocketIO threading world can exchange data safely.

WebWriter duck-types asyncio.StreamWriter:
  write(data)        — put bytes on the output queue
  drain()            — no-op coroutine (queue absorbs bursts)
  is_closing()       — bool flag
  close()            — set flag (session task puts sentinel via _run_web_session_task)
  wait_closed()      — no-op coroutine
"""
from __future__ import annotations

import queue as stdlib_queue


class WebWriter:
    """
    Duck-typed asyncio.StreamWriter that routes BBS output into a
    threading.Queue.  A drain thread in handlers.py empties that queue and
    emits Socket.IO 'web_terminal_output' events to the browser.

    The queue is intentionally unbounded so that put_nowait() never raises
    queue.Full — important because the asyncio event loop must not block.
    A None sentinel in the queue signals the drain thread to stop.
    """

    def __init__(self, output_queue: "stdlib_queue.Queue[bytes | None]") -> None:
        self._queue = output_queue
        self._closing = False

    # ── StreamWriter interface ────────────────────────────────────────────────

    def write(self, data: bytes) -> None:
        if not self._closing:
            self._queue.put_nowait(data)

    async def drain(self) -> None:
        """No-op: the queue absorbs bursts; no backpressure needed."""

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        """Mark as closing.  The sentinel is placed by _run_web_session_task."""
        self._closing = True

    async def wait_closed(self) -> None:
        """No-op: the drain thread handles teardown asynchronously."""
