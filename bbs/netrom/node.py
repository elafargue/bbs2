"""
bbs/netrom/node.py — NetromNode: the NET/ROM node switch / command layer (N2).

Sits *above* the transport + circuit layers.  Presents a ``=>`` command prompt
on a connected session and lets the user:

  • list the network (``N`` nodes / ``R`` routes / ``MH`` heard) from the
    router + heard data we already maintain, and
  • **connect onward** (``C <alias|call>``) to another node/BBS via an outbound
    NET/ROM circuit — resolve the destination, pick the adjacent next-hop
    (N0b), open/reuse the AX.25 crosslink (N1 ``connect_netrom``), originate an
    L3 circuit (``originate_circuit``), and **bridge** the user's session to it
    byte-for-byte until the far end disconnects (then ReConnect to ``=>``).

Transport-agnostic: it only ever deals in "a link to neighbor X" + circuits, so
future gateway types (AXUDP, Telnet-out) slot in without touching this file.

Entry is via a thin BBS-menu plugin (``bbs/plugins/node``) which constructs a
NetromNode on the live session and runs :meth:`command_loop`.  The node never
closes the connection itself — ``command_loop`` returns on ``BYE`` and the
caller decides what that means (back to the BBS menu, or disconnect).

No ANSI colour is emitted on this interface (plain ``send``/``sendln`` only).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from bbs.netrom.gateway import GatewayGuard, GatewayPolicy
from bbs.netrom.router import NetromRouter
from bbs.transport.base import Connection, Transport

logger = logging.getLogger(__name__)

# A local application the node offers via ``C <app>`` (N3): run the app on the
# user's own connection and return when it exits (the node resumes its ``=>``
# prompt).  Built by the engine — the BBS as ``BBS``, each ax25d-style service
# by its called SSID.
LocalAppRunner = Callable[[Connection], Awaitable[None]]

# Touch the node's activity this often while a local app runs, so the node's
# idle watchdog does not evict a user who is busy inside an application (the
# app governs its own idle timeout; see _run_local_app).
_APP_KEEPALIVE_SECS = 30.0

_BRIDGE_CHUNK = 4096
# User→far input is coalesced into whole lines before being sent as NET/ROM L3
# INFO frames.  A char-mode client (web xterm sends one keystroke per event)
# would otherwise produce one INFO frame per character — flooding the link with
# tiny frames + ACK churn and making the line-oriented far node respond to
# partial input.  Flush a partial buffer this large even without a newline so a
# paste / newline-less stream can't be hoarded forever.
_BRIDGE_LINE_MAX = 256


class NetromNode:
    """The ``=>`` command loop + outbound gateway for one connected session."""

    def __init__(
        self,
        *,
        term: Any,                 # bbs.core.terminal.Terminal (duck-typed for tests)
        conn: Connection,          # raw byte streams for the two-circuit bridge
        user_call: str,            # the connected user's callsign
        node_call: str,            # our node callsign (e.g. W6ELA-1)
        node_alias: str,           # our node alias (e.g. PALO)
        router: NetromRouter,
        transports: list[Transport],
        heard_recent: Optional[Callable[[], list]] = None,  # MH: recent RF-heard stations
        apps: Optional[dict[str, LocalAppRunner]] = None,  # local apps: C BBS / C <svc>
        may_connect: bool = True,           # gateway auth gate (full ACL is N4)
        connect_timeout: float = 60.0,
        min_quality: int = 1,
        max_gateway_circuits: int = 4,
        guard: Optional[GatewayGuard] = None,   # shared node-wide safety authority (N4a)
        auth_level: Any = None,                 # caller AuthLevel (for min_auth)
        arrival_via: str = "",                  # crosslink that carried us (INTERLOCK)
        entry: str = "node",                    # "menu" (@) | "native" (node SSID)
        reconnect: bool = True,             # always return to => on far-end close
        idle_timeout: Optional[float] = None,
        on_activity: Optional[Callable[[], None]] = None,
        user_eol: str = "\r",               # the user terminal's line ending
        echo_local: bool = False,           # echo user input during the bridge
    ) -> None:
        self.term = term
        self.conn = conn
        # The far end (an AX.25 NET/ROM node) speaks bare-CR line endings; the
        # user's terminal may not (web/TCP want CRLF).  The bridge translates
        # between the two so the far end's output doesn't collapse onto one line
        # and our forwarded input carries the CR the far end expects.
        self._user_eol = (user_eol or "\r").encode("ascii", errors="replace") or b"\r"
        self.user_call = user_call.upper()
        self.node_call = node_call.upper()
        self.node_alias = (node_alias or node_call).upper()
        self.router = router
        self.transports = list(transports)
        # MH source: () -> list[(callsign, unix_ts)] of recently RF-heard
        # stations, most-recent first (heard plugin).  None ⇒ MH falls back to
        # the neighbour list.
        self._heard_recent = heard_recent
        # Local applications reachable via ``C <name>`` (N3): the built-in BBS
        # as ``BBS`` plus each ax25d-style service by its called SSID.  Keyed
        # uppercase; resolved BEFORE NET/ROM node resolution in cmd_connect so a
        # local app name always wins over a same-named remote node (documented
        # precedence).  Empty when the engine wires no apps (e.g. tests).
        self._apps: dict[str, LocalAppRunner] = {
            k.upper(): v for k, v in (apps or {}).items()
        }
        self.may_connect = may_connect
        self.connect_timeout = connect_timeout
        self.min_quality = max(1, int(min_quality))
        self.max_gateway_circuits = max(1, int(max_gateway_circuits))
        # Gateway safety (N4a).  A shared GatewayGuard (from the node plugin) is
        # the node-wide authority for ACL / INTERLOCK / rate limits / circuit
        # caps.  Constructed without one (direct use / tests) ⇒ a permissive
        # per-instance guard whose only limit is the legacy max_gateway_circuits
        # budget, so gating is then done solely by may_connect (pre-N4a behavior).
        self.auth_level = auth_level
        self.arrival_via = (arrival_via or "").upper()
        self.entry = entry
        self._guard = guard if guard is not None else GatewayGuard(
            GatewayPolicy.permissive(self.max_gateway_circuits)
        )
        # Live-state for the web node dashboard (N4c): when connected onward the
        # current target alias/call, else None (at the => prompt).
        self.connected_at = time.time()
        self._last_activity = self.connected_at
        self._current_target: Optional[str] = None
        self.reconnect = reconnect
        self.idle_timeout = idle_timeout
        self._on_activity = on_activity
        # Web (xterm.js) has no local echo and NET/ROM nodes don't echo, so a
        # web user would type blind through the bridge — echo their input back
        # ourselves.  Terminals that echo locally (telnet client / TNC) leave
        # this False to avoid double echo.
        self._echo_local = echo_local

        self.prompt = f"{self.node_alias}:{self.node_call}}} "
        self._running = False
        self._active_gateways = 0

    # ── Command table (BPQ-style shortest-unambiguous-prefix abbreviation) ─────
    # (canonical name, handler attribute, takes an argument)
    _COMMANDS: list[tuple[str, str, bool]] = [
        ("CONNECT", "cmd_connect", True),
        ("NODES",   "cmd_nodes",   True),
        ("ROUTES",  "cmd_routes",  True),
        ("USERS",   "cmd_users",   False),
        ("INFO",    "cmd_info",    False),
        ("MHEARD",  "cmd_mheard",  False),
        ("PORTS",   "cmd_ports",   False),
        ("BYE",     "cmd_bye",     False),
        ("HELP",    "cmd_help",    False),
    ]
    _ALIASES: dict[str, str] = {
        "MH": "MHEARD", "?": "HELP", "H": "HELP", "Q": "BYE", "QUIT": "BYE",
    }

    def _resolve(self, verb: str) -> Optional[tuple[str, str, bool]]:
        """Map a typed verb to a command tuple, or None if unknown/ambiguous.

        Exact aliases win first; otherwise the verb must be a prefix of exactly
        one canonical command name (case-insensitive).
        """
        verb = verb.upper()
        canonical = self._ALIASES.get(verb)
        if canonical is not None:
            return next(c for c in self._COMMANDS if c[0] == canonical)
        matches = [c for c in self._COMMANDS if c[0].startswith(verb)]
        if len(matches) == 1:
            return matches[0]
        return None  # 0 = unknown, >1 = ambiguous

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def command_loop(self) -> None:
        """Run the ``=>`` prompt until BYE or the user disconnects.

        Returns control to the caller; does NOT close the connection.
        """
        self._running = True
        await self._send_banner()
        while self._running:
            await self.term.send(self.prompt)
            line = await self.term.readline(max_len=128, timeout=self.idle_timeout)
            if not line:
                if self.conn.reader.at_eof():
                    break
                continue  # bare Enter / idle tick → re-prompt
            self._touch()
            verb, _, arg = line.strip().partition(" ")
            if not verb:
                continue
            cmd = self._resolve(verb)
            if cmd is None:
                await self.term.sendln(f"Invalid command: {verb}   (? for help)")
                continue
            handler = getattr(self, cmd[1])
            try:
                await handler(arg.strip())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("netrom node: error handling %r from %s",
                                 verb, self.user_call)
                await self.term.sendln("Command failed.")

    def _touch(self) -> None:
        self._last_activity = time.time()
        if self._on_activity is not None:
            try:
                self._on_activity()
            except Exception:
                pass

    def describe(self) -> dict:
        """A JSON-serializable snapshot of this session's live state for the web
        node dashboard (N4c)."""
        now = time.time()
        return {
            "user": self.user_call,
            "entry": self.entry,               # "menu" (@) | "native"
            "via": self.arrival_via,           # arrival crosslink neighbor ("" = direct)
            "target": self._current_target,    # None at =>, else what it's bridged to
            "connected_s": int(now - self.connected_at),
            "idle_s": int(now - self._last_activity),
        }

    # ── Commands: listings ────────────────────────────────────────────────────

    async def _send_banner(self) -> None:
        await self.term.sendln()
        await self.term.sendln(f"{self.node_alias}:{self.node_call} NET/ROM node")
        await self.term.sendln(
            "Commands: C <node>  N  R  U  I  MH  P  B   (? for help)"
        )
        if self._apps:
            await self.term.sendln(
                f"Applications: {'  '.join(sorted(self._apps))}   (C <name>)"
            )

    async def cmd_help(self, _arg: str = "") -> None:
        for line in (
            "C <node|call>  Connect onward to a node/BBS (or a local application)",
            "N [pattern]    List known nodes",
            "R [node]       Routes table (neighbors); or routes to a node",
            "U              Users / active gateway circuits",
            "I              Node info",
            "MH             Nodes heard directly",
            "P              Ports (transports)",
            "B / Q          Bye",
            "?              This help",
        ):
            await self.term.sendln(line)
        if self._apps:
            await self.term.sendln(
                f"Applications (C <name>): {'  '.join(sorted(self._apps))}"
            )

    async def cmd_info(self, _arg: str = "") -> None:
        await self.term.sendln(f"{self.node_alias}:{self.node_call}  NET/ROM node (bbs2)")
        await self.term.sendln(
            f"Nodes known: {self.router.node_count}   "
            f"Neighbors: {len(self.router.adjacent_neighbors)}"
        )

    async def cmd_nodes(self, arg: str = "") -> None:
        """List known nodes (best route per destination), optionally filtered."""
        pat = arg.strip().upper()
        seen: set[str] = set()
        tokens: list[str] = []
        for r in self.router.routing_table:          # sorted alias, dest, -quality
            key = r.dest_call.upper()
            if key in seen:
                continue                              # first per dest = best
            seen.add(key)
            if pat and pat not in r.alias.upper() and pat not in key:
                continue
            tokens.append(f"{r.alias or '-'}:{r.dest_call}")
        if not tokens:
            await self.term.sendln("No nodes." if not pat else f"No nodes match {pat}.")
            return
        await self._send_columns(tokens)

    async def cmd_routes(self, arg: str = "") -> None:
        """Routes table (1987 manual p.22 / p.18 layout, matching peer nodes).

        With **no argument** — the neighbour list: one row per adjacent node as
        ``[>] port neighbour path-quality use-count [!]`` (``>`` = a live
        crosslink, ``!`` = operator-locked).
        With a **target** — up to three routes to that destination as
        ``[>] quality obs port neighbour`` (``>`` = the route in use).
        """
        target = arg.strip()
        if not target:
            neighbours = self.router.neighbour_list
            if not neighbours:
                await self.term.sendln("No routes.")
                return
            await self.term.sendln("Routes:")
            for n in neighbours:
                flag = ">" if n.crosslink else " "
                lock = " !" if n.locked else ""
                await self.term.sendln(
                    f" {flag} {n.port or '0'} {n.call} {n.path_quality} "
                    f"{n.use_count}{lock}"
                )
            return
        routes = self.router.get_routes(target)
        if not routes:
            await self.term.sendln(f"No route to {target.upper()}.")
            return
        best = routes[0]
        await self.term.sendln(f"Routes to {best.alias or '-'}:{best.dest_call}:")
        for r in routes:
            cur = ">" if r is best else " "
            await self.term.sendln(
                f" {cur} {r.quality} {r.obs_count} 0 {r.via_call}"
            )

    async def cmd_mheard(self, _arg: str = "") -> None:
        """Recently heard stations (MHEARD) — callsigns we've heard *directly* on
        the air (RF beacons / IDs), most-recent first, from the heard plugin.

        Distinct from ``R`` (the neighbour list of NODES broadcasters).  Falls
        back to the neighbour list when the heard plugin is unavailable."""
        if self._heard_recent is not None:
            try:
                heard = self._heard_recent()
            except Exception:
                heard = []
            if heard:
                now = time.time()
                tokens = [
                    f"{call}({int(max(0, now - ts) // 60)}m)"
                    for call, ts in heard
                ]
                await self._send_columns(tokens)
                return
        # Fallback: the neighbour list (nodes whose NODES we hear directly).
        tokens = [n.call for n in self.router.neighbour_list]
        if not tokens:
            await self.term.sendln("Nothing heard.")
            return
        await self._send_columns(tokens)

    async def cmd_users(self, _arg: str = "") -> None:
        await self.term.sendln(f"{self.node_alias}:{self.node_call}")
        await self.term.sendln(f"  {self.user_call} (you)")
        await self.term.sendln(
            f"Gateway circuits: {self._guard.active_for(self.user_call)} "
            f"(node total {self._guard.active_total})"
        )

    async def cmd_ports(self, _arg: str = "") -> None:
        for i, t in enumerate(self.transports):
            cap = " (crosslink)" if self._is_crosslink_capable(t) else ""
            await self.term.sendln(f"{i} {t.transport_id}{cap}")

    async def cmd_bye(self, _arg: str = "") -> None:
        await self.term.sendln(f"73 from {self.node_alias}")
        self._running = False

    async def _send_columns(self, tokens: list[str]) -> None:
        """Pack *tokens* into space-efficient lines within the terminal width."""
        width = getattr(self.term, "width", 80) or 80
        colw = max((len(t) for t in tokens), default=1) + 2
        per_line = max(1, width // colw)
        for i in range(0, len(tokens), per_line):
            row = tokens[i:i + per_line]
            await self.term.sendln("".join(t.ljust(colw) for t in row).rstrip())

    # ── Command: CONNECT — the crux ───────────────────────────────────────────

    async def cmd_connect(self, arg: str) -> None:
        target = arg.strip().split()[0] if arg.strip() else ""
        if not target:
            await self.term.sendln("Usage: C <node|call>")
            return

        # 0. Local application first (N3) — 'C BBS' / 'C <service>'.  A local
        #    app name wins over a same-named remote node (documented
        #    precedence) and does not go through the connect-out auth gate: the
        #    app enforces its own access (the BBS re-identifies; a service has
        #    its own min_auth).
        app = self._apps.get(target.upper())
        if app is not None:
            await self._run_local_app(target.upper(), app)
            return

        if not self.may_connect:
            await self.term.sendln("Not authorized to connect out.")
            return

        # 1. Resolve target → destination node.
        route = self.router.get_route(target)
        if route is None:
            await self.term.sendln(f"Unknown node: {target.upper()}")
            return
        dest_call = route.dest_call.upper()
        alias = (route.alias or dest_call).upper()
        if dest_call == self.node_call:
            await self.term.sendln("That's this node.")
            return

        # 2. Pick the adjacent next-hop neighbor (N0b) and a transport for it.
        neighbor = self.router.best_neighbor_for(target, min_quality=self.min_quality)
        if neighbor is None:
            await self.term.sendln(f"No route to {dest_call}.")
            return
        transport = self._crosslink_transport(neighbor)
        if transport is None:
            await self.term.sendln("No crosslink transport available.")
            return

        # 3. Gateway-safety gates (N4a): ACL / rate / INTERLOCK, then reserve a
        #    circuit slot (per-user + node-wide caps).  The shared guard is the
        #    single authority; a refusal returns its user-facing reason.
        reason = self._guard.check(
            user_call=self.user_call, auth_level=self.auth_level,
            dest_call=dest_call, neighbor=neighbor, arrival_via=self.arrival_via,
        )
        if reason:
            self._guard.note_refusal(self.user_call, reason, dest_call)
            await self.term.sendln(reason)
            return
        if not self._guard.acquire(self.user_call):
            self._guard.note_refusal(
                self.user_call, "Node busy — circuit cap", dest_call
            )
            await self.term.sendln("Node busy — too many circuits. Try later.")
            return

        # Everything past the reservation must release the slot on every exit
        # path (link failure, refusal, bridge end), so it lives in one finally.
        self._active_gateways += 1        # local mirror for the U listing
        self._current_target = alias      # dashboard: what we're bridged to
        near_closed = False
        try:
            # 4. Open (or reuse) the AX.25 crosslink to the neighbor (N1).
            await self.term.sendln(
                f"Connecting to {alias} ({dest_call}) via {neighbor} ..."
            )
            try:
                mgr = await transport.connect_netrom(neighbor)
            except (ConnectionError, asyncio.TimeoutError) as exc:
                logger.info("netrom node: crosslink to %s failed: %s", neighbor, exc)
                await self.term.sendln(f"Link to {neighbor} failed.")
                return
            if mgr is None:
                await self.term.sendln("No crosslink transport available.")
                return

            # 5. Originate the L3 circuit to the destination through the crosslink.
            try:
                circuit = await mgr.originate_circuit(
                    dest_call, self.user_call, timeout=self.connect_timeout
                )
            except asyncio.TimeoutError:
                await self.term.sendln(f"{alias} did not answer.")
                return
            except ConnectionRefusedError:
                await self.term.sendln(f"{alias} refused the connection.")
                return
            except (ConnectionError, Exception) as exc:  # defensive
                logger.info("netrom node: originate to %s failed: %s", dest_call, exc)
                await self.term.sendln(f"Could not connect to {alias}.")
                return

            # 6. Bridge the two sessions until one side closes.
            await self.term.sendln(f"*** Connected to {alias}")
            near_closed = await self._bridge(circuit)
        finally:
            self._guard.release(self.user_call)
            self._active_gateways -= 1
            self._current_target = None   # back at the => prompt

        # 7. ReConnect (or end if the user vanished).
        if near_closed:
            self._running = False   # user disconnected — leave the node loop
        elif self.reconnect:
            await self.term.sendln(f"*** Reconnected to {self.node_alias}")

    # ── Local applications (N3) ───────────────────────────────────────────────

    async def _run_local_app(self, name: str, runner: LocalAppRunner) -> None:
        """Run a local application (the BBS or a service) on the user's own
        connection, then return to the ``=>`` prompt.

        Same "run then ReConnect" shape as the outbound gateway bridge: the app
        owns the session while it runs (the ``=>`` interpreter is suspended);
        when it exits we resume the node prompt, or end the node if the user
        disconnected inside the app.  The app runs on ``self.conn`` but the
        engine wraps it so the app's own teardown does not close the underlying
        link — only the node loop's caller does that.

        While the app runs we periodically refresh the node's activity clock so
        the node's idle watchdog does not evict a user who is busy inside the
        application; the app enforces its own idle policy (the BBS has its own
        idle watchdog; a service has its ``idle_timeout``)."""
        logger.info("netrom node: %s → local app %s", self.user_call, name)
        keepalive: Optional[asyncio.Task[None]] = None
        if self._on_activity is not None:
            async def _keepalive() -> None:
                try:
                    while True:
                        await asyncio.sleep(_APP_KEEPALIVE_SECS)
                        self._touch()
                except asyncio.CancelledError:
                    pass
            keepalive = asyncio.create_task(
                _keepalive(), name=f"node-app-keepalive:{self.user_call}"
            )
        try:
            await runner(self.conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "netrom node: local app %s failed for %s", name, self.user_call
            )
            await self.term.sendln(f"{name} not available.")
            return
        finally:
            if keepalive is not None:
                keepalive.cancel()
                await asyncio.gather(keepalive, return_exceptions=True)
        self._touch()
        if self.conn.reader.at_eof():
            self._running = False          # user disconnected inside the app
        elif self.reconnect:
            await self.term.sendln(f"*** Reconnected to {self.node_alias}")

    # ── Two-circuit bridge ────────────────────────────────────────────────────

    @staticmethod
    def _to_far(data: bytes) -> bytes:
        """User → far end: normalize the user's line endings to bare CR, the
        AX.25 NET/ROM convention (so a web/TCP client's CRLF/LF doesn't send a
        stray LF the far node treats as a second, empty command line)."""
        return data.replace(b"\r\n", b"\r").replace(b"\n", b"\r")

    def _to_user(self, data: bytes) -> bytes:
        """Far end → user: translate the far node's bare CR (or CRLF) line
        endings to the user terminal's EOL, so its output doesn't collapse onto
        a single line on a CRLF terminal."""
        normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return normalized.replace(b"\n", self._user_eol)

    async def _bridge(self, circuit: Any) -> bool:
        """Pump bytes user-session ⇄ outbound circuit until one side closes.

        Returns True iff the NEAR side (user) closed first — the caller then
        ends the node loop.  Far-end close returns False (→ ReConnect).  Line
        endings are translated between the user's terminal EOL and the far
        end's bare-CR AX.25 convention (see :meth:`_to_far` / :meth:`_to_user`).
        """
        a_reader, a_writer = self.conn.reader, self.conn.writer
        b_reader, b_writer = circuit.reader, circuit.writer

        async def a_to_b() -> None:          # user → far end (line-buffered)
            pending = bytearray()
            while True:
                data = await a_reader.read(_BRIDGE_CHUNK)
                if not data:
                    if pending:              # flush the tail on disconnect
                        b_writer.write(self._to_far(bytes(pending)))
                        await b_writer.drain()
                    break
                self._touch()
                if self._echo_local:
                    # Immediate local echo for terminals that don't echo
                    # themselves (web); the far NET/ROM node won't echo for us.
                    a_writer.write(self._to_user(data))
                    await a_writer.drain()
                pending.extend(data)
                # Flush only whole lines (up to the last CR/LF), so a typed
                # command goes as ONE INFO frame — not one frame per keystroke.
                nl = max(pending.rfind(b"\r"), pending.rfind(b"\n"))
                if nl >= 0:
                    line = bytes(pending[:nl + 1])
                    del pending[:nl + 1]
                    b_writer.write(self._to_far(line))
                    await b_writer.drain()
                elif len(pending) >= _BRIDGE_LINE_MAX:
                    chunk = bytes(pending)
                    pending.clear()
                    b_writer.write(self._to_far(chunk))
                    await b_writer.drain()

        async def b_to_a() -> None:          # far end → user
            while True:
                data = await b_reader.read(_BRIDGE_CHUNK)
                if not data:
                    break
                self._touch()
                a_writer.write(self._to_user(data))
                await a_writer.drain()

        down = asyncio.create_task(a_to_b(), name=f"nodebridge-in:{self.user_call}")
        up = asyncio.create_task(b_to_a(), name=f"nodebridge-out:{self.user_call}")
        try:
            done, _pending = await asyncio.wait(
                {down, up}, return_when=asyncio.FIRST_COMPLETED
            )
            near_closed = down in done
        finally:
            # Tear down the far circuit (NETROM DISC REQ; the manager reaps the
            # now-circuit-less crosslink after its idle window) and stop pumps.
            try:
                if not circuit.writer.is_closing():
                    circuit.writer.close()
            except Exception:
                pass
            for t in (up, down):
                if not t.done():
                    t.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
        return near_closed

    # ── Transport selection ───────────────────────────────────────────────────

    @staticmethod
    def _is_crosslink_capable(t: Transport) -> bool:
        """True iff *t* overrides the base (no-op) connect_netrom — i.e. it can
        actually originate an outbound NET/ROM crosslink (AGWPE today)."""
        return type(t).connect_netrom is not Transport.connect_netrom

    def _crosslink_transport(self, _neighbor: str) -> Optional[Transport]:
        """Pick the transport to open a crosslink to *neighbor*.

        N2: the first crosslink-capable transport (AGWPE).  Per-neighbor /
        multi-transport selection is N4.
        """
        for t in self.transports:
            if self._is_crosslink_capable(t):
                return t
        return None
