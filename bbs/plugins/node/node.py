"""
bbs/plugins/node/node.py — BBS-menu entry into the NET/ROM node interface (N2).

A thin plugin: it owns the ``@`` menu key and, on selection, constructs a
:class:`bbs.netrom.node.NetromNode` on the live session and runs its ``=>``
command loop.  All the switch/command/bridge logic lives in ``NetromNode``; this
file only wires the session + the engine-injected router/transports together.

The plugin starts **disabled** and is enabled only when the engine calls
:meth:`bind` (i.e. only when NET/ROM is configured), so ``@`` stays out of the
menu on stations without a ``netrom:`` block.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from bbs.core.plugin_registry import BBSPlugin
from bbs.netrom.gateway import GatewayGuard, GatewayPolicy
from bbs.netrom.node import LocalAppRunner, NetromNode

if TYPE_CHECKING:
    from bbs.core.session import BBSSession
    from bbs.netrom.router import NetromRouter
    from bbs.transport.base import Connection, Transport

logger = logging.getLogger(__name__)


class NetromNodePlugin(BBSPlugin):
    """BBS-menu entry into the NET/ROM node (``=>``) interface."""

    name = "node"
    display_name = "Node (NET/ROM)"
    menu_key = "@"
    help_text = "Enter the NET/ROM node — connect onward to other nodes/BBSes"
    min_auth_level_name = "IDENTIFIED"

    def __init__(self) -> None:
        super().__init__()
        # Disabled until the engine binds router + transports (NET/ROM active).
        self.enabled = False
        self._router: Optional["NetromRouter"] = None
        self._transports: list["Transport"] = []
        self._node_call: str = ""
        self._node_alias: str = ""
        self._apps: dict[str, "LocalAppRunner"] = {}
        self._connect_timeout: float = 60.0
        self._min_quality: int = 1
        self._max_gateways: int = 4
        # Shared node-wide gateway-safety authority (N4a); one guard for every
        # session so ACL/rate/caps accounting is node-wide, not per-session.
        self._guard: GatewayGuard = GatewayGuard()
        # Live NetromNode sessions, for the web node dashboard (N4c).
        self._live: list[NetromNode] = []

    def bind(
        self,
        *,
        router: "NetromRouter",
        transports: list["Transport"],
        node_call: str,
        node_alias: str,
        apps: Optional[dict[str, "LocalAppRunner"]] = None,
        gateway_policy: Optional[GatewayPolicy] = None,
        connect_timeout: float = 60.0,
        min_quality: int = 1,
        max_gateways: int = 4,
    ) -> None:
        """Inject NET/ROM dependencies and enable the ``@`` menu entry."""
        self._router = router
        self._transports = list(transports)
        self._node_call = node_call
        self._node_alias = node_alias
        self._apps = dict(apps or {})
        self._guard = GatewayGuard(gateway_policy or GatewayPolicy())
        self._connect_timeout = float(connect_timeout)
        self._min_quality = int(min_quality)
        self._max_gateways = int(max_gateways)
        self.enabled = True
        logger.info(
            "netrom node plugin bound: %s:%s, %d transport(s), %d app(s)",
            node_alias, node_call, len(self._transports), len(self._apps),
        )

    def _make_node(
        self,
        *,
        term: Any,
        conn: "Connection",
        user_call: str,
        may_connect: bool,
        idle_timeout: Optional[float],
        on_activity: Optional[Callable[[], None]],
        auth_level: Any = None,
        entry: str = "node",
    ) -> NetromNode:
        """Construct a NetromNode with the bound dependencies (one place both
        entry points — the ``@`` BBS-menu item and the native node-SSID landing
        — share)."""
        assert self._router is not None
        return NetromNode(
            term=term,
            conn=conn,
            user_call=user_call,
            node_call=self._node_call,
            node_alias=self._node_alias,
            router=self._router,
            transports=self._transports,
            apps=self._apps,
            guard=self._guard,
            auth_level=auth_level,
            entry=entry,
            # The crosslink neighbor that carried an inbound NET/ROM circuit (if
            # any) — the node's INTERLOCK guard refuses routing back out it.
            arrival_via=getattr(conn, "netrom_via", ""),
            may_connect=may_connect,
            connect_timeout=self._connect_timeout,
            min_quality=self._min_quality,
            max_gateway_circuits=self._max_gateways,
            idle_timeout=idle_timeout,
            on_activity=on_activity,
            user_eol=getattr(term, "_eol", "\r"),
            # Web (xterm.js) has no local echo → echo input through the bridge.
            echo_local=getattr(term, "_must_echo", False),
        )

    async def handle_session(self, session: "BBSSession") -> None:
        """``@`` BBS-menu entry: run the node on the live BBS session; on BYE
        return to the BBS menu (the caller decides — see the N2 exit contract)."""
        # The main-menu dispatcher does NOT re-check auth level, so verify here.
        if not session.auth.is_identified:
            await session.term.sendln("You must identify (A) to use the node.")
            return
        if self._router is None:
            await session.term.sendln("NET/ROM node not available.")
            return
        node = self._make_node(
            term=session.term,
            conn=session.conn,
            user_call=session.auth.callsign or session.remote_addr,
            may_connect=session.auth.is_identified,
            idle_timeout=session.cfg.idle_timeout or None,
            on_activity=session.touch,
            auth_level=session.auth.level,
            entry="menu",
        )
        logger.info("session %s entering NET/ROM node", session.session_id)
        await self._run_tracked(node)

    async def run_native(
        self,
        *,
        term: Any,
        conn: "Connection",
        user_call: str,
        may_connect: bool = True,
        idle_timeout: Optional[float] = None,
        on_activity: Optional[Callable[[], None]] = None,
        auth_level: Any = None,
    ) -> None:
        """Native node-SSID landing (N3): a user who connected to the node SSID
        lands at ``=>`` directly (no BBS menu).

        The BBS and services become applications reachable via ``C BBS`` /
        ``C <svc>``.  Returns on BYE; the caller (the engine) then closes the
        connection — the native-landing half of the N2 exit contract."""
        if self._router is None:
            await term.sendln("NET/ROM node not available.")
            return
        node = self._make_node(
            term=term,
            conn=conn,
            user_call=user_call,
            may_connect=may_connect,
            idle_timeout=idle_timeout,
            on_activity=on_activity,
            auth_level=auth_level,
            entry="native",
        )
        await self._run_tracked(node)

    async def _run_tracked(self, node: NetromNode) -> None:
        """Run a node session's command loop while it is listed in the live-session
        registry (for the web dashboard), removing it on exit."""
        self._live.append(node)
        try:
            await node.command_loop()
        finally:
            try:
                self._live.remove(node)
            except ValueError:
                pass

    def activity_snapshot(self) -> dict:
        """JSON-serializable snapshot of live node sessions + gateway-safety
        state, for the web node dashboard (N4c).  Thread-safe (reads only; the
        live list is copied)."""
        return {
            "enabled": self.enabled,
            "node_call": self._node_call,
            "node_alias": self._node_alias,
            "sessions": [n.describe() for n in list(self._live)],
            "gateway": self._guard.stats(),
        }
