"""
bbs/core/engine.py — Asyncio BBS engine.

Responsibilities
----------------
- Start all configured transports and register a connection callback.
- For each accepted Connection, enforce the max-users limit, then spawn a
  BBSSession as an asyncio Task.
- Track all active sessions (for web dashboard and graceful shutdown).
- Bridge events from the asyncio world to the Flask-SocketIO thread via a
  thread-safe queue.
- Emit log records and session events to the web bridge queue.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Optional
import queue as stdlib_queue

from bbs.config import BBSConfig
from bbs.core.auth import AuthService
from bbs.core.plugin_registry import PluginRegistry
from bbs.core.session import BBSSession, SessionState
from bbs.db.connections import prune_old_connections, upsert_connection
from bbs.db.schema import init_db
from bbs.transport import build_transports
from bbs.transport.base import Connection

logger = logging.getLogger(__name__)

# Maximum log lines kept in the in-memory ring buffer (web dashboard)
LOG_BUFFER_SIZE = 500


class BBSEngine:
    """
    Central asyncio engine.  One instance per process.

    The web interface accesses public attributes directly (thread-safe reads
    for simple types; the event_queue is the only cross-thread channel).
    """

    def __init__(self, cfg: BBSConfig) -> None:
        self.cfg = cfg
        self.auth_service = AuthService(cfg)
        self.plugin_registry = PluginRegistry(cfg)

        # Active sessions: session_id → BBSSession
        self._sessions: dict[str, BBSSession] = {}
        self._session_tasks: dict[str, asyncio.Task[None]] = {}

        # Thread-safe queue → Flask-SocketIO bridge thread consumes this
        # Event dicts: {"type": "user_connected"|"user_disconnected"|"log", ...}
        self.event_queue: stdlib_queue.Queue[dict[str, Any]] = stdlib_queue.Queue(
            maxsize=1000
        )

        # Recent log lines ring buffer (web dashboard initial load)
        self.log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_SIZE)

        # Created lazily inside run() so it binds to the correct asyncio loop.
        # (Python 3.9 asyncio.Event() created before asyncio.run() binds to
        # the deprecated default loop and causes "Future attached to a different
        # loop" when awaited inside the real loop.)
        self._stop_event: Optional[asyncio.Event] = None

    # ── Startup & shutdown ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the engine; returns when stop() is called."""
        # Create the stop event here so it is bound to the running loop
        # (fixes Python 3.9 "Future attached to a different loop" error).
        self._stop_event = asyncio.Event()

        logger.info("BBS engine starting — %s", self.cfg.full_callsign)

        # Initialise database
        await init_db(str(self.cfg.db_path))

        # Prune stale connection log entries on startup
        if self.cfg.connection_log_days > 0:
            await prune_old_connections(
                str(self.cfg.db_path), self.cfg.connection_log_days
            )

        # Load plugins
        await self.plugin_registry.load_plugins()

        # Start transports
        transports = build_transports(self.cfg)
        if not transports:
            logger.warning("No transports enabled! Check bbs.yaml.")

        transport_tasks = [
            asyncio.create_task(t.start(self._on_connection), name=f"transport:{t.transport_id}")
            for t in transports
        ]

        self._emit_log(f"BBS {self.cfg.full_callsign} online — {len(transports)} transport(s)")
        logger.info("BBS engine running")

        # Wait until stop is requested
        await self._stop_event.wait()

        # Graceful shutdown
        logger.info("BBS engine shutting down…")
        for task in transport_tasks:
            task.cancel()

        # Disconnect all sessions
        for task in list(self._session_tasks.values()):
            task.cancel()

        # Shutdown plugins
        for plugin in self.plugin_registry:
            try:
                await plugin.shutdown()
            except Exception:
                logger.exception("Error shutting down plugin %s", plugin.name)

        self._emit_log(f"BBS {self.cfg.full_callsign} offline")
        logger.info("BBS engine stopped")

    def stop(self) -> None:
        """Thread-safe: request the engine to stop."""
        if self._stop_event is not None:
            self._stop_event.set()

    # ── Connection callback ───────────────────────────────────────────────────

    async def _on_connection(self, conn: Connection) -> None:
        """Called by each transport when a new connection arrives."""
        # Enforce max users
        if self.cfg.max_users > 0 and len(self._sessions) >= self.cfg.max_users:
            logger.warning(
                "Max users (%d) reached; rejecting %s",
                self.cfg.max_users,
                conn.remote_addr,
            )
            try:
                conn.writer.write(
                    f"\r\nSorry, {self.cfg.name} is full ({self.cfg.max_users} users max). Try again later.\r\n".encode()
                )
                await conn.writer.drain()
            except Exception:
                pass
            await conn.close()
            return

        session = BBSSession(
            conn=conn,
            cfg=self.cfg,
            auth_service=self.auth_service,
            plugin_registry=self.plugin_registry,
        )
        self._sessions[session.session_id] = session
        # Track the *current* task (the transport handler's task) so we can
        # cancel it during shutdown.  Do NOT create a new task — the transport
        # handler is already running in its own task and will close the socket
        # only after this coroutine returns.
        task = asyncio.current_task()
        if task is not None:
            self._session_tasks[session.session_id] = task
        await self._run_session(session)

    async def _run_session(self, session: BBSSession) -> None:
        self._emit_event({
            "type": "user_connected",
            "session_id": session.session_id,
            "remote_addr": session.remote_addr,
            "transport": session.conn.transport_id,
            "timestamp": time.time(),
        })
        self._emit_log(f"CONNECT {session.remote_addr} via {session.conn.transport_id}")

        try:
            await session.run()
        finally:
            # Record the connection in the journal (skip anonymous/unidentified).
            if session.auth.callsign and self.cfg.connection_log_days != 0:
                try:
                    await upsert_connection(
                        str(self.cfg.db_path),
                        callsign=session.auth.callsign,
                        transport=session.conn.transport_id,
                        connected_at=session.connected_at,
                        auth_level=session.auth.level.value,
                        connected=0,
                    )
                except Exception:
                    logger.exception("Failed to record connection for %s", session.auth.callsign)
            self._sessions.pop(session.session_id, None)
            self._session_tasks.pop(session.session_id, None)
            self._emit_event({
                "type": "user_disconnected",
                "session_id": session.session_id,
                "remote_addr": session.remote_addr,
                "transport": session.conn.transport_id,
                "timestamp": time.time(),
            })
            self._emit_log(
                f"DISCONNECT {session.remote_addr} "
                f"(online {int(time.time() - session.connected_at)}s)"
            )

    # ── Event bridge ──────────────────────────────────────────────────────────

    def _emit_event(self, event: dict[str, Any]) -> None:
        """Put an event on the cross-thread queue for the web bridge."""
        try:
            self.event_queue.put_nowait(event)
        except stdlib_queue.Full:
            pass  # web bridge too slow — drop event rather than blocking

    def _emit_log(self, message: str) -> None:
        """Append a log line to the ring buffer and put on the event queue."""
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        self.log_buffer.append(line)
        self._emit_event({"type": "log", "line": line, "timestamp": time.time()})

    # ── Web dashboard accessors (called from Flask thread) ────────────────────

    def connected_users_snapshot(self) -> list[dict[str, Any]]:
        """
        Snapshot of active sessions for the web dashboard.
        Safe to call from any thread (reads only).
        """
        return [
            {
                "session_id": s.session_id,
                "remote_addr": s.remote_addr,
                "callsign": s.auth.callsign or s.remote_addr,
                "transport": s.conn.transport_id,
                "auth_level": s.auth.level.name,
                "idle_seconds": round(s.idle_seconds),
                "connected_at": s.connected_at,
            }
            for s in list(self._sessions.values())
        ]

    def plugin_stats_snapshot(self) -> list[dict[str, Any]]:
        return self.plugin_registry.all_stats()

    def recent_log_lines(self, n: int = 100) -> list[str]:
        return list(self.log_buffer)[-n:]
