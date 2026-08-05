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
from typing import TYPE_CHECKING, Optional

from bbs.core.plugin_registry import BBSPlugin
from bbs.netrom.node import NetromNode

if TYPE_CHECKING:
    from bbs.core.session import BBSSession
    from bbs.netrom.router import NetromRouter
    from bbs.transport.base import Transport

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
        self._connect_timeout: float = 60.0
        self._min_quality: int = 1
        self._max_gateways: int = 4

    def bind(
        self,
        *,
        router: "NetromRouter",
        transports: list["Transport"],
        node_call: str,
        node_alias: str,
        connect_timeout: float = 60.0,
        min_quality: int = 1,
        max_gateways: int = 4,
    ) -> None:
        """Inject NET/ROM dependencies and enable the ``@`` menu entry."""
        self._router = router
        self._transports = list(transports)
        self._node_call = node_call
        self._node_alias = node_alias
        self._connect_timeout = float(connect_timeout)
        self._min_quality = int(min_quality)
        self._max_gateways = int(max_gateways)
        self.enabled = True
        logger.info(
            "netrom node plugin bound: %s:%s, %d transport(s)",
            node_alias, node_call, len(self._transports),
        )

    async def handle_session(self, session: "BBSSession") -> None:
        # The main-menu dispatcher does NOT re-check auth level, so verify here.
        if not session.auth.is_identified:
            await session.term.sendln("You must identify (A) to use the node.")
            return
        if self._router is None:
            await session.term.sendln("NET/ROM node not available.")
            return
        node = NetromNode(
            term=session.term,
            conn=session.conn,
            user_call=session.auth.callsign or session.remote_addr,
            node_call=self._node_call,
            node_alias=self._node_alias,
            router=self._router,
            transports=self._transports,
            may_connect=session.auth.is_identified,
            connect_timeout=self._connect_timeout,
            min_quality=self._min_quality,
            max_gateway_circuits=self._max_gateways,
            idle_timeout=session.cfg.idle_timeout or None,
            on_activity=session.touch,
            user_eol=getattr(session.term, "_eol", "\r"),
            # Web (xterm.js) has no local echo → echo input through the bridge.
            echo_local=getattr(session.term, "_must_echo", False),
        )
        logger.info("session %s entering NET/ROM node", session.session_id)
        await node.command_loop()
