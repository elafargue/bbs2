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
from dataclasses import replace
from typing import Any, Awaitable, Callable, Optional
import queue as stdlib_queue
from concurrent.futures import Future as ConcurrentFuture

from bbs.config import BBSConfig
from bbs.core.auth import AuthLevel, AuthService
from bbs.core.plugin_registry import PluginRegistry
from bbs.core.session import BBSSession, SessionState
from bbs.db.connections import prune_old_connections, upsert_connection
from bbs.db.schema import init_db
from bbs.netrom.gateway import GatewayPolicy
from bbs.netrom.router import NetromRouter
from bbs.services.bridge import run_service
from bbs.services.dispatcher import ServiceAction, ServiceDispatcher, ServiceRoute
from bbs.transport import build_transports
from bbs.transport.base import Connection

logger = logging.getLogger(__name__)

# Maximum log lines kept in the in-memory ring buffer (web dashboard)
LOG_BUFFER_SIZE = 500


class _NonClosingWriter:
    """Wrap a StreamWriter so ``close()``/``wait_closed()`` are no-ops.

    Lets a sub-application (the BBS or an ax25d-style service) run on a shared
    link from inside the NET/ROM node (N3, ``C BBS`` / ``C <svc>``) and exit
    without tearing down the underlying transport — so the node can resume its
    ``=>`` prompt.  ``write``/``drain`` delegate; ``is_closing`` reflects the
    real writer so output stops if the link actually drops."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def write(self, data: bytes) -> None:
        self._inner.write(data)

    async def drain(self) -> None:
        await self._inner.drain()

    def is_closing(self) -> bool:
        return self._inner.is_closing()

    def close(self) -> None:
        pass  # deliberately a no-op — the node owns the real link's lifetime

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, key: str, default: Any = None) -> Any:
        return self._inner.get_extra_info(key, default)


def _non_closing_conn(conn: Connection) -> Connection:
    """A view of *conn* whose writer's ``close()`` is a no-op (see
    :class:`_NonClosingWriter`) — for running a sub-app from the node."""
    return replace(conn, writer=_NonClosingWriter(conn.writer))  # type: ignore[arg-type]


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

        # NET/ROM node identity (N3).  self._netrom_node_call is the OPT-IN
        # switch: None (default) means the node runs on the BBS SSID (today's
        # behavior) and there is NO native => landing; a value (the node SSID,
        # e.g. "W6ELA-5") routes inbound connects to that SSID straight to the
        # node prompt in _on_connection.  self._netrom_apps is the node's
        # local-application registry (BBS + services) reachable via C <app>.
        self._netrom_node_call: Optional[str] = None
        # The node alias (e.g. "PALO").  When the node has its own SSID identity,
        # inbound connects to the alias land in the node prompt exactly like the
        # node SSID (registered as a listener on the transport).  "" = none.
        self._netrom_node_alias: str = ""
        self._netrom_apps: dict[str, "Callable[[Connection], Awaitable[None]]"] = {}

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

        # Persist per-transport broadcast timestamps beside the DB so a restart
        # honours the beacon / NODES cadence instead of transmitting immediately.
        _state_dir = self.cfg.db_path.parent
        for t in transports:
            t.set_broadcast_state_path(
                str(_state_dir / f"broadcast_state_{t.transport_id}.json")
            )

        # Wire heard-station observer onto supporting transports
        heard_plugin = self.plugin_registry.get("heard")
        if heard_plugin is not None and heard_plugin.enabled:
            for t in transports:
                t.set_heard_observer(heard_plugin.on_heard)  # type: ignore[attr-defined]
            logger.info(
                "Heard-station observer registered on %d transport(s)", len(transports)
            )

        # Build the ax25d-style service dispatcher EARLY (before the NET/ROM
        # node binds) so the node's local-application registry — 'C BBS' /
        # 'C <svc>' — can enumerate the configured service routes.  The SSID
        # registration with the radio still happens further below (it must
        # precede start()).
        self._services = ServiceDispatcher(self.cfg.services or {})

        # Wire NETROM router onto supporting transports.
        # Listening (RX) requires only the netrom section to be present.
        # Broadcasting (TX) additionally requires an alias to be set.
        netrom_router = None
        netrom_cfg = self.cfg.netrom or {}
        if netrom_cfg:
            netrom_alias = str(netrom_cfg.get("alias", "")).strip().upper()
            # N3: the effective node identity.  When netrom.node_ssid is set
            # (and valid) the node presents on <callsign>-<node_ssid> and that
            # SSID gets the native => landing; otherwise the node runs on the
            # BBS callsign (today's behavior) and self._netrom_node_call stays
            # None so _on_connection never diverts to the native landing.
            self._netrom_node_call = self.cfg.netrom_node_call
            effective_node_call = self._netrom_node_call or self.cfg.full_callsign
            # The alias lands in the node prompt like the node SSID, so only wire
            # it when the node has its own SSID identity (native landing active).
            self._netrom_node_alias = netrom_alias if self._netrom_node_call else ""
            netrom_router = NetromRouter(
                effective_node_call,
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
                # N5 routing fidelity (1987 PARMS): compose advertised route
                # qualities through our per-neighbour link (path) quality.
                channel_quality=int(netrom_cfg.get("channel_quality", 192)),
                neighbour_quality=netrom_cfg.get("neighbour_quality") or {},
                worst_quality=int(netrom_cfg.get("worst_quality", 1)),
                max_destinations=int(netrom_cfg.get("max_destinations", 200)),
                obs_initializer=int(netrom_cfg.get("obs_initializer", 6)),
                obs_min_to_broadcast=int(netrom_cfg.get("obs_min_to_broadcast", 5)),
            )
            # N5b/b2: the router owns and persists the netrom_routes +
            # netrom_neighbours tables (composed quality + obsolescence), so
            # adjacency + link qualities survive a restart.
            netrom_router.set_db_path(str(self.cfg.db_path))
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
            link_idle_timeout = float(netrom_cfg.get("link_idle_timeout", 300))
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
                # N3: source outbound crosslinks + NODES from the node identity
                # (the node SSID when configured, else the BBS callsign) and, on
                # AGWPE, register that SSID so inbound connects to it reach us.
                t.set_netrom_node_call(effective_node_call)
                if self._netrom_node_alias:
                    t.set_netrom_node_alias(self._netrom_node_alias)
                if netrom_alias:
                    # Only register the builder (and thus start the broadcast
                    # loop) when we have a node alias to advertise.
                    t.set_netrom_nodes_builder(netrom_router.build_nodes_payloads)
            logger.info(
                "NETROM router %s wired onto %d transport(s) — alias: %s, "
                "classifier: router-lookup%s",
                effective_node_call, len(transports),
                netrom_alias if netrom_alias else "(listen-only, no alias set)",
                (f", node SSID {effective_node_call} (BBS on {self.cfg.full_callsign})"
                 if self._netrom_node_call else ""),
            )

            # N3: build the node's local-application registry (the BBS as 'BBS'
            # plus each configured service) so the node offers 'C <app>'.  The
            # same registry backs both the '@' entry and the native landing.
            self._netrom_apps = self._build_netrom_apps()

            # Wire the NET/ROM node command layer (N2): bind the '@' menu plugin
            # to the router + crosslink-capable transports so users can enter the
            # node and connect onward.  Binding also enables the plugin (it stays
            # disabled/hidden on stations without a netrom: block).  With a node
            # SSID set (N3) the node presents its own identity and the BBS
            # becomes an application.
            node_plugin = self.plugin_registry.get("node")
            if node_plugin is not None:
                node_plugin.bind(  # type: ignore[attr-defined]
                    router=netrom_router,
                    transports=transports,
                    node_call=effective_node_call,
                    node_alias=netrom_alias or effective_node_call,
                    apps=self._netrom_apps,
                    # N4a: gateway-safety policy (ACL / INTERLOCK / rate / caps).
                    gateway_policy=GatewayPolicy.from_netrom_cfg(netrom_cfg),
                    # N5c: MH source — recently RF-heard stations (heard plugin).
                    heard_recent=(
                        heard_plugin.recent_direct_heard  # type: ignore[union-attr]
                        if heard_plugin is not None and heard_plugin.enabled else None
                    ),
                    connect_timeout=float(netrom_cfg.get("connect_timeout", 60.0)),
                    min_quality=int(netrom_cfg.get("connect_min_quality", 1)),
                    max_gateways=int(netrom_cfg.get("max_gateway_circuits", 4)),
                )

        # Register ax25d-style external-service SSIDs so transports accept
        # connects to them (must happen before start()).  The dispatcher itself
        # was built earlier so the NET/ROM node app-registry could see it.
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

        # Periodic NETROM obsolescence-count decay (N5b) — replaces the hard-TTL
        # prune.  Runs at the NODES broadcast cadence (manual p.66: the scan
        # "occurs with the same frequency as routing broadcasts"): each route's
        # obs count is decremented and routes at 0 are deleted, while
        # actively-broadcast routes stay refreshed.  Only runs when wired.
        netrom_prune_task: asyncio.Task | None = None
        if netrom_router is not None:
            async def _netrom_decay_loop(
                router=netrom_router, interval=nodes_interval_s,
            ) -> None:
                while True:
                    await asyncio.sleep(interval)
                    n = router.decay_obsolescence()
                    if n:
                        logger.info("netrom: obsolescence scan deleted %d route(s)", n)
                    await router.persist()   # snapshot after decay (prunes the DB)
            netrom_prune_task = asyncio.create_task(
                _netrom_decay_loop(), name="netrom:decay"
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
        # N3: native NET/ROM node landing.  A user who dialed our node SSID
        # lands at the '=>' prompt (the BBS + services become 'applications'
        # reachable via C BBS / C <svc>).  Opt-in — only when netrom.node_ssid
        # is configured (self._netrom_node_call set), so every station keeps
        # today's behavior on upgrade.  Checked FIRST so the node SSID wins over
        # service dispatch and the BBS.  (A neighbor *crosslink* on this SSID
        # never reaches here — the transport classifies it via is_direct_neighbor
        # and never calls back into the engine; only genuine user sessions and
        # per-user NET/ROM circuits addressed to the node SSID arrive here.)
        if self._netrom_node_call:
            _called = (conn.local_addr or "").upper()
            # Connects to the node SSID *or* its alias (e.g. PALO) land natively.
            if _called == self._netrom_node_call.upper() or (
                self._netrom_node_alias and _called == self._netrom_node_alias
            ):
                await self._run_node_native(conn)
                return

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

    # ── NET/ROM node: native landing + local applications (N3) ────────────────

    async def _run_node_native(self, conn: Connection) -> None:
        """Run the NET/ROM node (``=>``) natively for a user who connected to
        the node SSID (N3).  BYE ⇒ disconnect (the native-landing exit
        contract); the node itself never closes the connection.

        A lightweight idle watchdog evicts an idle native user (the node's
        command loop only re-prompts on idle); activity inside a ``C <app>``
        sub-session keeps it fresh via the node's keepalive (see NetromNode)."""
        node_plugin = self.plugin_registry.get("node")
        if node_plugin is None or not getattr(node_plugin, "enabled", False):
            # netrom.node_ssid set but the node plugin isn't bound — shouldn't
            # happen (both are wired together in run()); fail safe.
            logger.warning("node SSID connect but node plugin unavailable — closing")
            await conn.close()
            return

        # Native node users count toward max_users.
        if self.cfg.max_users > 0 and len(self._sessions) >= self.cfg.max_users:
            logger.warning(
                "Max users (%d) reached; rejecting node connect from %s",
                self.cfg.max_users, conn.remote_addr,
            )
            try:
                conn.writer.write(
                    f"\r\nSorry, {self.cfg.name} is full "
                    f"({self.cfg.max_users} users max). Try again later.\r\n".encode()
                )
                await conn.writer.drain()
            except Exception:
                pass
            await conn.close()
            return

        from bbs.core.terminal import Terminal

        _is_ax25 = conn.transport_id in (
            "kernel_ax25", "kiss_tcp", "kiss_serial", "agwpe", "netrom"
        )
        term = await Terminal.create(
            conn.reader,
            conn.writer,
            echo=not _is_ax25,
            # AX.25/NET/ROM peers want bare CR; other transports get CRLF.
            eol="\r" if _is_ax25 else "\r\n",
            write_timeout=self.cfg.write_timeout,
        )
        user_call = (conn.remote_addr or "").upper().strip()
        idle_to = float(self.cfg.idle_timeout) if self.cfg.idle_timeout else 0.0
        connected_at = time.time()

        last_activity = time.monotonic()

        def _touch() -> None:
            nonlocal last_activity
            last_activity = time.monotonic()

        self._emit_event({
            "type": "user_connected",
            "session_id": f"node:{conn.remote_addr}:{connected_at:.3f}",
            "remote_addr": conn.remote_addr,
            "transport": conn.transport_id,
            "timestamp": connected_at,
        })
        self._emit_log(f"NODE {conn.remote_addr} → {self._netrom_node_call}")

        loop_task = asyncio.create_task(
            node_plugin.run_native(  # type: ignore[attr-defined]
                term=term, conn=conn, user_call=user_call,
                idle_timeout=(idle_to or None), on_activity=_touch,
                # A native-landing radio caller is identified by their AX.25
                # callsign; the gateway ACL's min_auth is checked against this.
                auth_level=AuthLevel.IDENTIFIED,
            ),
            name=f"node-native:{conn.remote_addr}",
        )
        watchdog: Optional[asyncio.Task[None]] = None
        if idle_to > 0:
            async def _idle_watchdog() -> None:
                interval = min(10.0, idle_to / 3.0)
                try:
                    while True:
                        await asyncio.sleep(interval)
                        if time.monotonic() - last_activity > idle_to:
                            logger.info(
                                "node %s: idle watchdog fired after %.0fs — closing",
                                conn.remote_addr, idle_to,
                            )
                            loop_task.cancel()
                            return
                except asyncio.CancelledError:
                    pass
            watchdog = asyncio.create_task(
                _idle_watchdog(), name=f"node-native:wd:{conn.remote_addr}"
            )

        try:
            # asyncio.wait (not `await loop_task`) so an idle-cancel of the loop
            # task returns here normally, while a real cancel of THIS task (engine
            # shutdown) propagates out of wait() — then the finally tears down.
            await asyncio.wait({loop_task})
        finally:
            if not loop_task.done():
                loop_task.cancel()
            if watchdog is not None:
                watchdog.cancel()
            await asyncio.gather(
                loop_task, *( [watchdog] if watchdog is not None else [] ),
                return_exceptions=True,
            )
            if conn.remote_addr and self.cfg.connection_log_days != 0:
                try:
                    await upsert_connection(
                        str(self.cfg.db_path),
                        callsign=conn.remote_addr,
                        transport=f"node:{self._netrom_node_call}",
                        connected_at=connected_at,
                        auth_level=AuthLevel.IDENTIFIED.value,
                        connected=0,
                    )
                except Exception:
                    logger.exception(
                        "Failed to record node session for %s", conn.remote_addr
                    )
            self._emit_event({
                "type": "user_disconnected",
                "session_id": f"node:{conn.remote_addr}:{connected_at:.3f}",
                "remote_addr": conn.remote_addr,
                "transport": conn.transport_id,
                "timestamp": time.time(),
            })
            self._emit_log(
                f"NODE-END {conn.remote_addr} "
                f"(online {int(time.time() - connected_at)}s)"
            )
            await conn.close()

    def _build_netrom_apps(self) -> "dict[str, Callable[[Connection], Awaitable[None]]]":
        """Build the NET/ROM node's local-application registry (N3): the names
        the node offers via ``C <app>``.

        Always includes ``BBS`` (the built-in BBS as an application); adds each
        configured ax25d-style service by its called SSID.  Each value runs the
        app on the user's own connection (wrapped so the app's own teardown
        won't close the underlying link) and returns when the app exits — the
        node then resumes its ``=>`` prompt."""
        apps: dict[str, Callable[[Connection], Awaitable[None]]] = {
            "BBS": self._run_bbs_app,
        }
        if self._services is not None and self._services.enabled:
            for route in self._services.routes():
                apps[route.called.upper()] = self._make_service_app(route)
        return apps

    async def _run_bbs_app(self, conn: Connection) -> None:
        """Run the built-in BBS as an application on *conn* (N3, ``C BBS``).

        A fresh BBSSession runs on a non-closing view of the link, as its own
        task so its idle watchdog cancels only the app (returning the user to
        ``=>``), not the whole node session.  We call ``session.run()`` directly
        rather than ``_run_session`` — the physical connection is already
        journaled + evented by the native landing, so routing the sub-app
        through ``_run_session`` would double-count it."""
        session = BBSSession(
            conn=_non_closing_conn(conn),
            cfg=self.cfg,
            auth_service=self.auth_service,
            plugin_registry=self.plugin_registry,
        )
        task = asyncio.create_task(
            session.run(), name=f"node-bbs-app:{conn.remote_addr}"
        )
        try:
            await task
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _make_service_app(
        self, route: ServiceRoute
    ) -> "Callable[[Connection], Awaitable[None]]":
        """Build a runner that hosts ax25d-style service *route* as a node
        application (N3, ``C <svc>``), on a non-closing view of the link."""
        async def _run(conn: Connection) -> None:
            app_conn = _non_closing_conn(conn)
            argv = self._services.build_argv(route, app_conn)  # type: ignore[union-attr]
            await run_service(app_conn, route, argv)
        return _run

    def reload_services(self) -> None:
        """Rebuild the service dispatcher from ``self.cfg.services`` and refresh
        the SSID registration list on transports.

        Safe to call from the web thread: the dispatcher is swapped by an
        atomic reference assignment, so an in-flight ``match()`` on the asyncio
        loop always sees a consistent table.  Routing/lockout/flag changes take
        effect immediately; a *newly added* service SSID is registered with the
        radio on the next transport (re)connect (or a restart).

        Note (N3): the NET/ROM node's ``C <app>`` registry is snapshotted at
        startup (bind time), so a service added here becomes reachable via the
        node after a restart — it is reachable by direct-connect immediately.
        Live app-registry reload is folded into the N4 unified-application work.
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

    def netrom_snapshot(self) -> dict[str, Any]:
        """Thread-safe snapshot of live NET/ROM node activity — active node
        sessions (who's at ``=>`` or bridged onward) plus gateway-safety state
        (caps, live count, recent refusals) — for the web node dashboard (N4c).
        Returns ``{"enabled": False}`` when the node isn't running."""
        node_plugin = self.plugin_registry.get("node")
        if node_plugin is None or not getattr(node_plugin, "enabled", False):
            return {"enabled": False}
        try:
            return node_plugin.activity_snapshot()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("netrom_snapshot failed")
            return {"enabled": False}

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
