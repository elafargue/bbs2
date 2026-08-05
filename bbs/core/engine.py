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
from concurrent.futures import Future as ConcurrentFuture

from bbs.config import BBSConfig
from bbs.core.auth import AuthLevel, AuthService
from bbs.core.plugin_registry import PluginRegistry
from bbs.core.session import BBSSession, SessionState
from bbs.db.connections import prune_old_connections, upsert_connection
from bbs.db.schema import init_db
from bbs.netrom.router import NetromRouter
from bbs.services.bridge import run_service
from bbs.services.dispatcher import ServiceAction, ServiceDispatcher, ServiceRoute
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

    def __init__(self, cfg: BBSConfig, cfg_path: str = "config/bbs.yaml") -> None:
        self.cfg = cfg
        self.cfg_path = cfg_path  # path to the yaml file, used for live config updates
        self.auth_service = AuthService(cfg)
        self.plugin_registry = PluginRegistry(cfg)

        # Active sessions: session_id → BBSSession
        self._sessions: dict[str, BBSSession] = {}
        self._session_tasks: dict[str, asyncio.Task[None]] = {}

        # ax25d-style external-service hosting (built in run()).
        self._services: Optional[ServiceDispatcher] = None
        # Active external-service sessions: id → (Connection, ServiceRoute)
        self._service_sessions: dict[str, Any] = {}
        # Live transports (set in run()) — used by reload_services() to refresh
        # the registered service SSIDs when the config changes via the web UI.
        self._transports: list = []

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

        # Set once run() starts; used to schedule work from Flask threads.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Maps Socket.IO SID → BBS session_id for web terminal sessions.
        self._web_session_map: dict[str, str] = {}

    # ── Startup & shutdown ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the engine; returns when stop() is called."""
        # Create the stop event here so it is bound to the running loop
        # (fixes Python 3.9 "Future attached to a different loop" error).
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()

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
        self._transports = transports
        if not transports:
            logger.warning("No transports enabled! Check bbs.yaml.")

        # Wire heard-station observer onto supporting transports
        heard_plugin = self.plugin_registry.get("heard")
        if heard_plugin is not None and heard_plugin.enabled:
            for t in transports:
                t.set_heard_observer(heard_plugin.on_heard)  # type: ignore[attr-defined]
            logger.info(
                "Heard-station observer registered on %d transport(s)", len(transports)
            )

        # Wire NETROM router onto supporting transports.
        # Listening (RX) requires only the netrom section to be present.
        # Broadcasting (TX) additionally requires an alias to be set.
        netrom_router = None
        netrom_cfg = self.cfg.netrom or {}
        if netrom_cfg:
            netrom_alias = str(netrom_cfg.get("alias", "")).strip().upper()
            netrom_router = NetromRouter(
                self.cfg.full_callsign,
                netrom_alias,
                route_ttl_seconds=int(
                    netrom_cfg.get("route_ttl_minutes", 180)
                ) * 60,
                hop_cost=int(netrom_cfg.get("hop_cost", 25)),
                min_advert_quality=int(netrom_cfg.get("min_advert_quality", 10)),
                advertise_self_only=bool(
                    netrom_cfg.get("advertise_self_only", True)
                ),
                direct_heard_ttl_seconds=int(
                    netrom_cfg.get("direct_heard_ttl_minutes", 60)
                ) * 60,
            )
            # Seed the in-memory routing table from the heard plugin's
            # netrom_routes table so we don't begin every restart with
            # an empty router (which would suppress NODES TX for ~30 min).
            # Seeding bypasses the nodes_observer, so heard rows aren't
            # re-written.
            try:
                seeded = await netrom_router.seed_from_db(str(self.cfg.db_path))
                if seeded:
                    logger.info(
                        "NETROM router seeded from heard DB: %d route(s)",
                        seeded,
                    )
            except Exception:
                logger.warning("netrom router seed failed", exc_info=True)
            # Wire heard plugin as the NODES observer so routing table updates
            # feed into the heard_stations / netrom_routes tables and backfill
            # node aliases on previously-heard RF stations.
            heard_plugin = self.plugin_registry.get("heard")
            if heard_plugin is not None and heard_plugin.enabled:
                netrom_router.set_nodes_observer(
                    heard_plugin.on_netrom_nodes  # type: ignore[attr-defined]
                )
                logger.info("NETROM nodes observer wired to heard plugin")
                # Adjacency enrichment (N0.5): the heard plugin PUSHES RF
                # direct-hearings into the router (replaying its DB-seeded cache
                # immediately, then live), so the router — the single adjacency
                # authority — treats nodes we hear on the air as 1-hop neighbors
                # even right after a restart.  Optional: absent/disabled heard
                # just means the router relies on crosslinks + NODES routes.
                heard_plugin.set_direct_heard_observer(  # type: ignore[attr-defined]
                    netrom_router.note_heard_direct
                )
                logger.info("NETROM router direct-heard enrichment wired (push)")
            nodes_interval_s = max(1, int(netrom_cfg.get("nodes_interval", 30))) * 60
            # Outbound NETROM L3 info-MTU.  Default 108 = PACLEN 128 - 20.
            # Operators with a different direwolf.conf PACLEN must set this
            # to (PACLEN - 20) or the TNC will split L3 frames at L2 and
            # drop the headerless second half on the wire.
            info_mtu = int(netrom_cfg.get("info_mtu", 108))
            # Idle-crosslink reaper: disconnect a NETROM crosslink after this
            # many seconds with no circuits (0 = keep it up indefinitely, like
            # the Linux AX.25 IDLE=0 default). Frees the link and stops needless
            # T3 keepalives once the last circuit closes.
            link_idle_timeout = float(netrom_cfg.get("link_idle_timeout", 900))
            # Classification of incoming AX.25 connections as NETROM vs. direct
            # BBS delegates to the router's single adjacency authority
            # (is_direct_neighbor): live crosslink, or a known node heard
            # directly / with a fresh direct route.
            for t in transports:
                t.set_netrom_observer(netrom_router.on_netrom_frame)
                t.set_netrom_nodes_interval(nodes_interval_s)
                # Accept inbound NETROM L3 crosslinks on transports that can
                # demultiplex them (AGWPE today; no-op elsewhere).
                t.set_netrom_crosslink_enabled(True)
                t.set_netrom_neighbor_check(netrom_router.is_direct_neighbor)
                # A live crosslink is proof of adjacency — feed it back in.
                t.set_netrom_crosslink_observer(netrom_router.note_crosslink)
                t.set_netrom_info_mtu(info_mtu)
                t.set_netrom_link_idle_timeout(link_idle_timeout)
                if netrom_alias:
                    # Only register the builder (and thus start the broadcast
                    # loop) when we have a node alias to advertise.
                    t.set_netrom_nodes_builder(netrom_router.build_nodes_payload)
            logger.info(
                "NETROM router %s wired onto %d transport(s) — alias: %s, "
                "classifier: router-lookup",
                self.cfg.full_callsign, len(transports),
                netrom_alias if netrom_alias else "(listen-only, no alias set)",
            )

            # Wire the NET/ROM node command layer (N2): bind the '@' menu plugin
            # to the router + crosslink-capable transports so users can enter the
            # node and connect onward.  Binding also enables the plugin (it stays
            # disabled/hidden on stations without a netrom: block).
            node_plugin = self.plugin_registry.get("node")
            if node_plugin is not None:
                node_plugin.bind(  # type: ignore[attr-defined]
                    router=netrom_router,
                    transports=transports,
                    node_call=self.cfg.full_callsign,
                    node_alias=netrom_alias or self.cfg.full_callsign,
                    connect_timeout=float(netrom_cfg.get("connect_timeout", 60.0)),
                    min_quality=int(netrom_cfg.get("connect_min_quality", 1)),
                    max_gateways=int(netrom_cfg.get("max_gateway_circuits", 4)),
                )

        # Wire ax25d-style external-service hosting.  The dispatcher routes an
        # inbound connection (by called SSID) to an external program instead of
        # the internal BBS; register its service SSIDs so transports accept
        # connects to them (must happen before start()).
        self._services = ServiceDispatcher(self.cfg.services or {})
        if self._services.enabled:
            svc_calls = self._services.route_callsigns()
            for t in transports:
                t.set_extra_callsigns(svc_calls)
            logger.info(
                "services: external-service dispatch enabled — %d route(s): %s",
                len(svc_calls), ", ".join(svc_calls),
            )

        transport_tasks = [
            asyncio.create_task(t.start(self._on_connection), name=f"transport:{t.transport_id}")
            for t in transports
        ]

        # Periodic NETROM route expiry — only runs when the router is wired.
        netrom_prune_task: asyncio.Task | None = None
        if netrom_router is not None:
            async def _netrom_prune_loop(router=netrom_router) -> None:
                while True:
                    await asyncio.sleep(60)
                    n = router.prune_stale_routes()
                    if n:
                        logger.info("netrom: pruned %d stale route(s)", n)
            netrom_prune_task = asyncio.create_task(
                _netrom_prune_loop(), name="netrom:prune"
            )

        self._emit_log(f"BBS {self.cfg.full_callsign} online — {len(transports)} transport(s)")
        logger.info("BBS engine running")

        # Wait until stop is requested, or a KeyboardInterrupt cancels our task.
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass

        # Graceful shutdown
        logger.info("BBS engine shutting down…")
        if netrom_prune_task is not None:
            netrom_prune_task.cancel()
        for task in transport_tasks:
            task.cancel()

        # Disconnect all sessions
        for task in list(self._session_tasks.values()):
            task.cancel()

        # Wait for transports (and their sessions) to fully tear down before
        # calling plugin shutdown, so plugins see a consistent state.
        await asyncio.gather(*transport_tasks, return_exceptions=True)

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
        # ax25d-style dispatch: route to an external program by called SSID,
        # BEFORE any BBS banner/menu is emitted.
        if self._services is not None and self._services.enabled:
            decision = self._services.match(conn)
            if decision.action is ServiceAction.REFUSE:
                logger.info(
                    "services: refusing %s → %s (%s)",
                    conn.remote_addr, conn.local_addr, decision.reason,
                )
                await conn.close()
                return
            if decision.action is ServiceAction.EXEC and decision.route is not None:
                await self._run_external_service(conn, decision.route)
                return
            # PASS → fall through to the internal BBS below.

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
        await self.plugin_registry.event_bus.publish("session.connected", {
            "remote_addr": session.remote_addr,
            "transport": session.conn.transport_id,
            "timestamp": time.time(),
        })

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
            await self.plugin_registry.event_bus.publish("session.disconnected", {
                "callsign": session.auth.callsign or session.remote_addr,
                "remote_addr": session.remote_addr,
                "transport": session.conn.transport_id,
                "duration": time.time() - session.connected_at,
                "auth_level": session.auth.level.name,
                "timestamp": time.time(),
            })
            self._emit_log(
                f"DISCONNECT {session.remote_addr} "
                f"(online {int(time.time() - session.connected_at)}s)"
            )

    async def _run_external_service(self, conn: Connection, route: ServiceRoute) -> None:
        """Bridge *conn* to an external program (ax25d-style), enforcing the
        service-session cap and journaling the connection (unless quiet)."""
        assert self._services is not None
        if len(self._service_sessions) >= self._services.max_sessions:
            logger.warning(
                "services: max_sessions (%d) reached — refusing %s → %s",
                self._services.max_sessions, conn.remote_addr, route.called,
            )
            await conn.close()
            return

        argv = self._services.build_argv(route, conn)
        sid = f"svc-{id(conn):x}"
        self._service_sessions[sid] = (conn, route)
        connected_at = time.time()
        if not route.quiet:
            self._emit_log(
                f"SERVICE {conn.remote_addr} → {route.called} exec {route.exec_path}"
            )
        try:
            await run_service(conn, route, argv)
        finally:
            self._service_sessions.pop(sid, None)
            if not route.quiet:
                if conn.remote_addr and self.cfg.connection_log_days != 0:
                    try:
                        await upsert_connection(
                            str(self.cfg.db_path),
                            callsign=conn.remote_addr,
                            transport=f"service:{route.called}",
                            connected_at=connected_at,
                            auth_level=AuthLevel.IDENTIFIED.value,
                            connected=0,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to record service connection for %s", conn.remote_addr
                        )
                self._emit_log(
                    f"SERVICE-END {conn.remote_addr} → {route.called} "
                    f"(online {int(time.time() - connected_at)}s)"
                )

    def reload_services(self) -> None:
        """Rebuild the service dispatcher from ``self.cfg.services`` and refresh
        the SSID registration list on transports.

        Safe to call from the web thread: the dispatcher is swapped by an
        atomic reference assignment, so an in-flight ``match()`` on the asyncio
        loop always sees a consistent table.  Routing/lockout/flag changes take
        effect immediately; a *newly added* service SSID is registered with the
        radio on the next transport (re)connect (or a restart).
        """
        self._services = ServiceDispatcher(self.cfg.services or {})
        svc_calls = self._services.route_callsigns() if self._services.enabled else []
        for t in self._transports:
            t.set_extra_callsigns(svc_calls)
        logger.info("services: reloaded — %d route(s)", len(svc_calls))

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

    # ── Web terminal API (called from Flask-SocketIO threads) ─────────────────

    async def _create_and_run_web_session(
        self,
        sid: str,
        output_queue: "stdlib_queue.Queue[bytes | None]",
    ) -> asyncio.StreamReader:
        """
        Run inside the asyncio event loop.  Creates a synthetic Connection for
        a web terminal, registers it, and fires off the session as a new task.
        Returns the StreamReader so callers can feed keystrokes into it.
        """
        from bbs.transport.web import WebWriter

        reader = asyncio.StreamReader()
        writer = WebWriter(output_queue)
        conn = Connection(
            remote_addr=f"ws:{sid[:8]}",
            reader=reader,
            writer=writer,
            transport_id="web",
        )

        if self.cfg.max_users > 0 and len(self._sessions) >= self.cfg.max_users:
            writer.write(b"\r\nSorry, the BBS is full. Try again later.\r\n")
            output_queue.put_nowait(None)  # stop drain thread
            return reader

        session = BBSSession(
            conn=conn,
            cfg=self.cfg,
            auth_service=self.auth_service,
            plugin_registry=self.plugin_registry,
        )
        self._sessions[session.session_id] = session
        self._web_session_map[sid] = session.session_id

        task = asyncio.create_task(
            self._run_web_session_task(session, sid, output_queue),
            name=f"web-session:{sid[:8]}",
        )
        self._session_tasks[session.session_id] = task
        return reader

    async def _run_web_session_task(
        self,
        session: BBSSession,
        sid: str,
        output_queue: "stdlib_queue.Queue[bytes | None]",
    ) -> None:
        """Wrapper task: run a web session then signal the drain thread."""
        try:
            await self._run_session(session)
        finally:
            self._web_session_map.pop(sid, None)
            output_queue.put_nowait(None)  # sentinel → drain thread exits

    def start_web_session(
        self,
        sid: str,
        output_queue: "stdlib_queue.Queue[bytes | None]",
    ) -> asyncio.StreamReader:
        """
        Thread-safe.  Start a web BBS session and return the reader that
        callers use to feed keystrokes.  Blocks until the asyncio setup is
        complete (fast; the session itself runs as a background task).
        """
        if self._loop is None:
            raise RuntimeError("BBSEngine not running")
        future = asyncio.run_coroutine_threadsafe(
            self._create_and_run_web_session(sid, output_queue), self._loop
        )
        return future.result(timeout=5.0)

    def feed_web_input(self, reader: asyncio.StreamReader, data: bytes) -> None:
        """Thread-safe.  Push bytes from the browser into the session's reader."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(reader.feed_data, data)

    def close_web_input(self, reader: asyncio.StreamReader) -> None:
        """Thread-safe.  Send EOF to the session reader, ending the session."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(reader.feed_eof)

    def resize_web_session(self, sid: str, cols: int, rows: int) -> None:
        """
        Thread-safe.  Update terminal dimensions for a running web session.
        Simple attribute writes are safe under the CPython GIL.
        """
        session_id = self._web_session_map.get(sid)
        if session_id:
            session = self._sessions.get(session_id)
            if session and hasattr(session, "term"):
                session.term.width = cols
                session.term.height = rows
