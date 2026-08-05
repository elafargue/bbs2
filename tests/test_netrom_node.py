"""
tests/test_netrom_node.py — Unit tests for the NET/ROM node command layer (N2).

Covers:
  - verb resolution / BPQ-style prefix abbreviation
  - listing commands (N / R / MH / I) against a seeded router
  - the C <alias|call> flow: resolve → best_neighbor_for → connect_netrom →
    originate_circuit (happy path + every error branch), bridge stubbed
  - the two-circuit bridge directly: byte passthrough both ways, near/far close
    discrimination, teardown closes the circuit

No sockets: a fake terminal scripts command input + captures output, a fake
transport/manager/circuit stand in for the N1 stack, and a real NetromRouter is
seeded with direct + transit routes.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from bbs.core.auth import AuthLevel
from bbs.netrom.gateway import GatewayGuard, GatewayPolicy
from bbs.netrom.node import NetromNode
from bbs.netrom.router import NetromRouter, RouteEntry
from bbs.transport.base import Connection, Transport


# ─── Fakes ────────────────────────────────────────────────────────────────────

class FakeTerminal:
    """Scripts readline() input and captures all output as plain text."""

    def __init__(self, script: list[str] | None = None, width: int = 80) -> None:
        self.width = width
        self.out: list[str] = []
        self._script = list(script or [])

    async def send(self, text: str) -> None:
        self.out.append(text)

    async def sendln(self, text: str = "") -> None:
        self.out.append(text + "\n")

    async def readline(self, max_len: int = 128, timeout=None) -> str:
        return self._script.pop(0) if self._script else ""

    def text(self) -> str:
        return "".join(self.out)


class CaptureWriter:
    """Duck-typed StreamWriter that records bytes and close state."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._closing = False
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True
        self.closed = True


class FakeCircuit:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = CaptureWriter()


class FakeManager:
    def __init__(self, circuit=None, exc: Exception | None = None) -> None:
        self._circuit = circuit
        self._exc = exc
        self.originate_calls: list[tuple[str, str]] = []

    async def originate_circuit(self, dest_call, user_call, *,
                                proposed_window=4, timeout=30.0):
        self.originate_calls.append((dest_call, user_call))
        if self._exc is not None:
            raise self._exc
        return self._circuit


class FakeTransport(Transport):
    transport_id = "fake"

    def __init__(self, mgr=None, exc: Exception | None = None) -> None:
        self._mgr = mgr
        self._exc = exc
        self.connect_calls: list[str] = []

    async def start(self, on_connect) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover - unused
        pass

    async def connect_netrom(self, neighbor: str):
        self.connect_calls.append(neighbor)
        if self._exc is not None:
            raise self._exc
        return self._mgr


# ─── Fixtures / builders ──────────────────────────────────────────────────────

def _route(dest, alias, via, via_alias, q) -> RouteEntry:
    return RouteEntry(
        dest_call=dest, alias=alias, neighbor_call=dest, quality=q,
        via_call=via, via_alias=via_alias, last_seen=time.time(),
    )


def _seeded_router() -> NetromRouter:
    r = NetromRouter("W6ELA-1", "PALO")
    # JOHN is a DIRECT neighbor (via_call == dest_call).
    r._upsert_route(_route("KF6ANX-4", "JOHN", "KF6ANX-4", "JOHN", 192))
    # ROCK is another adjacent neighbor (broadcasts NODES to us directly).
    r._upsert_route(_route("K6FB-5", "ROCK", "K6FB-5", "ROCK", 192))
    # MONTC is reached via JOHN (transit — K2YE-5 is NOT an adjacent neighbor).
    r._upsert_route(_route("K2YE-5", "MONTC", "KF6ANX-4", "JOHN", 144))
    # Our own node (for the local-loop guard).
    r._upsert_route(_route("W6ELA-1", "PALO", "KF6ANX-4", "JOHN", 144))
    # N5: register the direct neighbours in the neighbour list (in production
    # _process_nodes does this; these tests seed routes directly via _upsert_route).
    for _call in ("KF6ANX-4", "K6FB-5"):
        r._get_or_create_neighbour(_call, time.time())
    return r


def _make_node(*, router=None, transport=None, script=None, **kwargs) -> NetromNode:
    term = FakeTerminal(script=script)
    reader = asyncio.StreamReader()
    reader.feed_eof()                    # command-mode: empty readline → break
    conn = Connection(
        remote_addr="N0USER-1", reader=reader, writer=CaptureWriter(),
        transport_id="test",
    )
    node = NetromNode(
        term=term, conn=conn,
        user_call="N0USER-1", node_call="W6ELA-1", node_alias="PALO",
        router=router if router is not None else _seeded_router(),
        transports=[transport] if transport is not None else [],
        **kwargs,
    )
    node._term = term  # convenience handle
    return node


# ─── Verb resolution / abbreviation ───────────────────────────────────────────

class TestResolve:
    async def test_prefix_abbreviations(self):
        n = _make_node()
        assert n._resolve("C")[0] == "CONNECT"
        assert n._resolve("CO")[0] == "CONNECT"
        assert n._resolve("CONNECT")[0] == "CONNECT"
        assert n._resolve("N")[0] == "NODES"
        assert n._resolve("R")[0] == "ROUTES"
        assert n._resolve("U")[0] == "USERS"
        assert n._resolve("I")[0] == "INFO"
        assert n._resolve("P")[0] == "PORTS"
        assert n._resolve("B")[0] == "BYE"

    async def test_aliases(self):
        n = _make_node()
        assert n._resolve("MH")[0] == "MHEARD"
        assert n._resolve("?")[0] == "HELP"
        assert n._resolve("H")[0] == "HELP"
        assert n._resolve("Q")[0] == "BYE"
        assert n._resolve("QUIT")[0] == "BYE"

    async def test_case_insensitive(self):
        assert _make_node()._resolve("c")[0] == "CONNECT"

    async def test_unknown_returns_none(self):
        assert _make_node()._resolve("ZZ") is None


# ─── Listing commands ─────────────────────────────────────────────────────────

class TestListings:
    async def test_nodes_lists_best_per_dest(self):
        n = _make_node(); t = n._term
        await n.cmd_nodes("")
        txt = t.text()
        assert "JOHN:KF6ANX-4" in txt
        assert "MONTC:K2YE-5" in txt
        assert "PALO:W6ELA-1" in txt

    async def test_nodes_filter(self):
        n = _make_node(); t = n._term
        await n.cmd_nodes("MONT")
        txt = t.text()
        assert "MONTC:K2YE-5" in txt
        assert "JOHN:KF6ANX-4" not in txt

    async def test_routes_no_arg_lists_neighbour_table(self):
        # N5c: the ROCK-style neighbour list — "[>] port call path_q use_count".
        n = _make_node(); t = n._term
        await n.cmd_routes("")
        txt = t.text()
        assert "Routes:" in txt
        assert "KF6ANX-4 192" in txt          # neighbour call + path quality
        assert "K6FB-5 192" in txt
        assert "K2YE-5" not in txt            # transit dest, not a neighbour

    async def test_routes_target_shows_quality_obs_neighbour(self):
        # N5c: "[>] quality obs port neighbour" per route to the dest.
        n = _make_node(); t = n._term
        await n.cmd_routes("MONTC")
        txt = t.text()
        assert "Routes to MONTC:K2YE-5" in txt
        assert "144 6 0 KF6ANX-4" in txt      # quality obs port neighbour

    async def test_routes_unknown(self):
        n = _make_node(); t = n._term
        await n.cmd_routes("NOPE")
        assert "No route" in t.text()

    async def test_mheard_falls_back_to_neighbours_without_heard(self):
        n = _make_node(); t = n._term       # no heard_recent wired
        await n.cmd_mheard("")
        txt = t.text()
        assert "KF6ANX-4" in txt and "K6FB-5" in txt

    async def test_mheard_uses_heard_recent_when_available(self):
        import time as _t
        n = _make_node(heard_recent=lambda: [("W1AW-3", int(_t.time()) - 120)])
        t = n._term
        await n.cmd_mheard("")
        assert "W1AW-3" in t.text()

    async def test_info(self):
        n = _make_node(); t = n._term
        await n.cmd_info("")
        assert "PALO:W6ELA-1" in t.text()


# ─── command_loop plumbing ────────────────────────────────────────────────────

class TestCommandLoop:
    async def test_banner_then_commands_then_bye(self):
        n = _make_node(script=["I", "B"])
        await n.command_loop()
        txt = n._term.text()
        assert "NET/ROM node" in txt        # banner
        assert "73 from PALO" in txt         # bye
        assert n._running is False

    async def test_invalid_command(self):
        n = _make_node(script=["ZZ", "B"])
        await n.command_loop()
        assert "Invalid command: ZZ" in n._term.text()

    async def test_eof_exits_loop(self):
        # No BYE in the script → after the one command, readline "" + at_eof breaks.
        n = _make_node(script=["I"])
        await asyncio.wait_for(n.command_loop(), timeout=1.0)


# ─── C <target> flow ──────────────────────────────────────────────────────────

class TestConnectFlow:
    def _node_with_stack(self, *, mgr=None, transport_exc=None, **node_kwargs):
        circuit = FakeCircuit()
        mgr = mgr if mgr is not None else FakeManager(circuit=circuit)
        transport = FakeTransport(mgr=mgr, exc=transport_exc)
        node = _make_node(transport=transport, **node_kwargs)
        node._bridge = AsyncMock(return_value=False)  # far-end closed → ReConnect
        return node, transport, mgr

    async def test_connect_direct_happy_path(self):
        node, transport, mgr = self._node_with_stack()
        await node.cmd_connect("JOHN")
        assert transport.connect_calls == ["KF6ANX-4"]         # neighbor (direct)
        assert mgr.originate_calls == [("KF6ANX-4", "N0USER-1")]  # L3 dest
        txt = node._term.text()
        assert "*** Connected to JOHN" in txt
        assert "*** Reconnected to PALO" in txt
        assert node._active_gateways == 0
        node._bridge.assert_awaited_once()

    async def test_connect_transit_uses_neighbor_not_dest(self):
        node, transport, mgr = self._node_with_stack()
        await node.cmd_connect("MONTC")
        assert transport.connect_calls == ["KF6ANX-4"]         # crosslink to neighbor
        assert mgr.originate_calls == [("K2YE-5", "N0USER-1")]  # L3 dest = final node

    async def test_near_close_ends_node(self):
        node, transport, mgr = self._node_with_stack()
        node._bridge = AsyncMock(return_value=True)   # user disconnected
        await node.cmd_connect("JOHN")
        assert node._running is False
        assert "*** Reconnected" not in node._term.text()

    async def test_unknown_node(self):
        node, transport, mgr = self._node_with_stack()
        await node.cmd_connect("NOPE")
        assert "Unknown node: NOPE" in node._term.text()
        assert transport.connect_calls == []

    async def test_local_loop_guard(self):
        node, transport, mgr = self._node_with_stack()
        await node.cmd_connect("PALO")
        assert "this node" in node._term.text()
        assert transport.connect_calls == []

    async def test_no_route_below_quality(self):
        node, transport, mgr = self._node_with_stack(min_quality=200)
        await node.cmd_connect("MONTC")      # q=144 < 200
        assert "No route" in node._term.text()
        assert transport.connect_calls == []

    async def test_usage_when_no_arg(self):
        node, transport, mgr = self._node_with_stack()
        await node.cmd_connect("")
        assert "Usage" in node._term.text()

    async def test_unauthorized(self):
        node, transport, mgr = self._node_with_stack(may_connect=False)
        await node.cmd_connect("JOHN")
        assert "Not authorized" in node._term.text()
        assert transport.connect_calls == []

    async def test_cap_reached(self):
        node, transport, mgr = self._node_with_stack(max_gateway_circuits=1)
        node._guard.acquire("PREFILL")          # fill the node-wide budget
        await node.cmd_connect("JOHN")
        assert "busy" in node._term.text().lower()
        assert transport.connect_calls == []

    async def test_link_failed(self):
        node, transport, mgr = self._node_with_stack(
            transport_exc=ConnectionError("no TCP")
        )
        await node.cmd_connect("JOHN")
        assert "Link to KF6ANX-4 failed" in node._term.text()

    async def test_originate_timeout(self):
        node, transport, mgr = self._node_with_stack(
            mgr=FakeManager(exc=asyncio.TimeoutError())
        )
        await node.cmd_connect("JOHN")
        assert "did not answer" in node._term.text()

    async def test_originate_refused(self):
        node, transport, mgr = self._node_with_stack(
            mgr=FakeManager(exc=ConnectionRefusedError())
        )
        await node.cmd_connect("JOHN")
        assert "refused" in node._term.text()

    async def test_no_crosslink_transport(self):
        # A router with routes but no crosslink-capable transport at all.
        node = _make_node(transport=None)
        await node.cmd_connect("JOHN")
        assert "No crosslink transport" in node._term.text()


# ─── N4a gateway-safety gates (guard wired into cmd_connect) ──────────────────

class TestGatewayGates(TestConnectFlow):
    async def test_deny_refuses(self):
        guard = GatewayGuard(GatewayPolicy(deny=frozenset({"N0USER"})))
        node, transport, mgr = self._node_with_stack(guard=guard)
        await node.cmd_connect("JOHN")            # user N0USER-1 (base N0USER)
        assert transport.connect_calls == []
        assert "denied" in node._term.text().lower()

    async def test_allow_list_closes_node(self):
        guard = GatewayGuard(GatewayPolicy(allow=frozenset({"KF6ANX"})))
        node, transport, mgr = self._node_with_stack(guard=guard)
        await node.cmd_connect("JOHN")            # not on the allow list
        assert transport.connect_calls == []
        assert "authorized" in node._term.text().lower()

    async def test_min_auth_refuses_below(self):
        guard = GatewayGuard(GatewayPolicy(min_auth=AuthLevel.AUTHENTICATED))
        node, transport, mgr = self._node_with_stack(
            guard=guard, auth_level=AuthLevel.IDENTIFIED
        )
        await node.cmd_connect("JOHN")
        assert transport.connect_calls == []
        assert "uthentication" in node._term.text()

    async def test_interlock_refuses_back_out_arrival_link(self):
        guard = GatewayGuard(GatewayPolicy(interlock=True))
        # JOHN is direct → next-hop KF6ANX-4; arrive on that same crosslink.
        node, transport, mgr = self._node_with_stack(
            guard=guard, arrival_via="KF6ANX-4"
        )
        await node.cmd_connect("JOHN")
        assert transport.connect_calls == []
        assert "nterlock" in node._term.text()

    async def test_interlock_allows_other_neighbor(self):
        guard = GatewayGuard(GatewayPolicy(interlock=True))
        node, transport, mgr = self._node_with_stack(
            guard=guard, arrival_via="K6FB-5"    # different from JOHN's next-hop
        )
        await node.cmd_connect("JOHN")
        assert transport.connect_calls == ["KF6ANX-4"]

    async def test_slot_acquired_and_released_around_connect(self):
        guard = GatewayGuard(GatewayPolicy())
        node, transport, mgr = self._node_with_stack(guard=guard)
        await node.cmd_connect("JOHN")           # bridge mocked → far-end close
        assert transport.connect_calls == ["KF6ANX-4"]
        assert guard.active_total == 0           # released after the bridge
        assert node._active_gateways == 0

    async def test_node_wide_cap_refuses(self):
        guard = GatewayGuard(GatewayPolicy(max_circuits=1))
        node, transport, mgr = self._node_with_stack(guard=guard)
        guard.acquire("SOMEONE-2")               # a different user fills the node
        await node.cmd_connect("JOHN")
        assert transport.connect_calls == []
        assert "busy" in node._term.text().lower()

    async def test_refusal_recorded_for_dashboard(self):
        guard = GatewayGuard(GatewayPolicy(deny=frozenset({"N0USER"})))
        node, transport, mgr = self._node_with_stack(guard=guard)
        await node.cmd_connect("JOHN")
        refusals = guard.stats()["recent_refusals"]
        assert len(refusals) == 1
        assert refusals[0]["user"] == "N0USER-1"
        assert refusals[0]["dest"] == "KF6ANX-4"

    async def test_current_target_cleared_after_connect(self):
        node, transport, mgr = self._node_with_stack()   # bridge → far close
        await node.cmd_connect("JOHN")
        assert node._current_target is None              # back at =>
        assert node.describe()["target"] is None


# ─── Node live-state describe() (N4c dashboard) ───────────────────────────────

class TestDescribe:
    async def test_describe_at_prompt(self):
        node = _make_node()
        d = node.describe()
        assert d["user"] == "N0USER-1"
        assert d["entry"] == "node"          # default entry label
        assert d["target"] is None           # at the => prompt
        assert d["via"] == ""                # direct (no arrival crosslink)
        assert d["idle_s"] >= 0 and d["connected_s"] >= 0

    async def test_describe_reports_arrival_and_entry(self):
        node = _make_node(arrival_via="K6FB-5", entry="native")
        d = node.describe()
        assert d["via"] == "K6FB-5"
        assert d["entry"] == "native"


# ─── Local applications (N3): C BBS / C <svc> ─────────────────────────────────

class TestLocalApps:
    def _app_node(self, apps, *, script=None, at_eof=False, **kwargs):
        term = FakeTerminal(script=script)
        reader = asyncio.StreamReader()
        if at_eof:
            reader.feed_eof()
        conn = Connection(
            remote_addr="N0USER-1", reader=reader,
            writer=CaptureWriter(), transport_id="test",
        )
        node = NetromNode(
            term=term, conn=conn, user_call="N0USER-1",
            node_call="W6ELA-5", node_alias="PALO",
            router=_seeded_router(), transports=[], apps=apps, **kwargs,
        )
        node._term = term
        node._running = True              # simulate an active command loop
        return node

    async def test_c_bbs_runs_local_app_and_reconnects(self):
        ran: list = []

        async def bbs_app(conn):
            ran.append(conn)

        node = self._app_node({"BBS": bbs_app})       # reader NOT at eof
        await node.cmd_connect("BBS")
        assert len(ran) == 1
        assert ran[0] is node.conn                     # ran on the user's conn
        assert "*** Reconnected to PALO" in node._term.text()
        assert node._running is True                   # back at =>

    async def test_c_bbs_is_case_insensitive(self):
        ran: list = []

        async def bbs_app(conn):
            ran.append(1)

        node = self._app_node({"BBS": bbs_app})
        await node.cmd_connect("bbs")
        assert ran == [1]

    async def test_local_app_wins_over_may_connect_gate(self):
        ran: list = []

        async def bbs_app(conn):
            ran.append(1)

        node = self._app_node({"BBS": bbs_app}, may_connect=False)
        await node.cmd_connect("BBS")
        assert ran == [1]                              # ran despite may_connect=False
        assert "Not authorized" not in node._term.text()

    async def test_app_eof_ends_node(self):
        async def bbs_app(conn):
            pass

        node = self._app_node({"BBS": bbs_app}, at_eof=True)
        await node.cmd_connect("BBS")
        assert node._running is False                  # user vanished inside the app
        assert "*** Reconnected" not in node._term.text()

    async def test_app_failure_reports(self):
        async def broken(conn):
            raise RuntimeError("boom")

        node = self._app_node({"BBS": broken})
        await node.cmd_connect("BBS")
        assert "BBS not available" in node._term.text()

    async def test_unknown_app_falls_through_to_netrom(self):
        # 'JOHN' is a known node, not a local app → connect-out path runs.
        circuit = FakeCircuit()
        transport = FakeTransport(mgr=FakeManager(circuit=circuit))
        term = FakeTerminal()
        conn = Connection(
            remote_addr="N0USER-1", reader=asyncio.StreamReader(),
            writer=CaptureWriter(), transport_id="test",
        )
        node = NetromNode(
            term=term, conn=conn, user_call="N0USER-1",
            node_call="W6ELA-5", node_alias="PALO",
            router=_seeded_router(), transports=[transport], apps={"BBS": None},
        )
        node._bridge = AsyncMock(return_value=False)
        await node.cmd_connect("JOHN")
        assert transport.connect_calls == ["KF6ANX-4"]

    async def test_banner_lists_applications(self):
        async def a(conn):
            pass

        node = self._app_node({"BBS": a})
        await node._send_banner()
        assert "Applications: BBS" in node._term.text()


# ─── Two-circuit bridge ───────────────────────────────────────────────────────

def _bridge_node():
    """A node whose conn has a real reader + capture writer, for _bridge tests."""
    term = FakeTerminal()
    conn = Connection(
        remote_addr="N0USER-1", reader=asyncio.StreamReader(),
        writer=CaptureWriter(), transport_id="test",
    )
    return NetromNode(
        term=term, conn=conn, user_call="N0USER-1",
        node_call="W6ELA-1", node_alias="PALO",
        router=_seeded_router(), transports=[],
    )


class TestNodeIntegration:
    async def test_full_connect_bridge_reconnect(self):
        """command_loop → C JOHN → real _bridge → far-end close → ReConnect →
        BYE, with real byte flow through the bridge (nothing stubbed)."""
        circuit = FakeCircuit()
        circuit.reader.feed_data(b"hello from JOHN")
        circuit.reader.feed_eof()                       # far end closes after greeting
        transport = FakeTransport(mgr=FakeManager(circuit=circuit))
        term = FakeTerminal(script=["C JOHN", "B"])
        conn = Connection(
            remote_addr="N0USER-1", reader=asyncio.StreamReader(),
            writer=CaptureWriter(), transport_id="test",
        )
        node = NetromNode(
            term=term, conn=conn, user_call="N0USER-1",
            node_call="W6ELA-1", node_alias="PALO",
            router=_seeded_router(), transports=[transport],
        )
        await asyncio.wait_for(node.command_loop(), timeout=1.0)
        txt = term.text()
        assert transport.connect_calls == ["KF6ANX-4"]
        assert "*** Connected to JOHN" in txt
        assert bytes(conn.writer.buf) == b"hello from JOHN"   # far → user delivered
        assert "*** Reconnected to PALO" in txt
        assert "73 from PALO" in txt                          # BYE after ReConnect
        assert circuit.writer.closed is True


class TestBridge:
    async def test_far_end_close_passthrough_and_reconnect(self):
        node = _bridge_node()
        circuit = FakeCircuit()
        # user → far (a complete line, so it flushes through the line buffer)
        node.conn.reader.feed_data(b"ping\r")
        # far → user, then far closes
        circuit.reader.feed_data(b"pong")
        circuit.reader.feed_eof()
        near_closed = await asyncio.wait_for(node._bridge(circuit), timeout=1.0)
        assert near_closed is False                     # far end closed
        assert bytes(circuit.writer.buf) == b"ping\r"    # user → far delivered
        assert bytes(node.conn.writer.buf) == b"pong"    # far → user delivered
        assert circuit.writer.closed is True             # teardown closed circuit

    async def test_near_end_close_returns_true(self):
        node = _bridge_node()
        circuit = FakeCircuit()
        node.conn.reader.feed_data(b"bye")
        node.conn.reader.feed_eof()                      # user disconnects
        near_closed = await asyncio.wait_for(node._bridge(circuit), timeout=1.0)
        assert near_closed is True
        assert bytes(circuit.writer.buf) == b"bye"
        assert circuit.writer.closed is True

    async def test_bridge_closes_circuit_even_if_already_closing(self):
        node = _bridge_node()
        circuit = FakeCircuit()
        circuit.writer._closing = True                   # pretend already closing
        node.conn.reader.feed_eof()
        near_closed = await asyncio.wait_for(node._bridge(circuit), timeout=1.0)
        assert near_closed is True
        # close() must NOT be called again when already closing (no double DISC).
        assert circuit.writer.closed is False

    def test_to_far_normalizes_to_bare_cr(self):
        # user CRLF / LF → bare CR for the AX.25 far end
        assert NetromNode._to_far(b"a\r\nb\nc\r") == b"a\rb\rc\r"

    async def test_bridge_line_buffers_user_input(self):
        """Keystrokes are coalesced into whole lines before being sent to the
        far end — a partial (newline-less) buffer is held, not forwarded, so a
        char-mode client (web) doesn't emit one INFO frame per character."""
        node = _bridge_node()
        circuit = FakeCircuit()
        task = asyncio.create_task(node._bridge(circuit))
        node.conn.reader.feed_data(b"NODE")             # partial line, no CR
        await asyncio.sleep(0.02)
        assert bytes(circuit.writer.buf) == b""         # held, not forwarded yet
        node.conn.reader.feed_data(b"S\r")              # completes the line
        await asyncio.sleep(0.02)
        assert bytes(circuit.writer.buf) == b"NODES\r"  # forwarded as one line
        node.conn.reader.feed_eof()
        await asyncio.wait_for(task, timeout=1.0)

    async def test_bridge_flushes_partial_tail_on_disconnect(self):
        """A newline-less tail is still flushed when the user disconnects."""
        node = _bridge_node()
        circuit = FakeCircuit()
        node.conn.reader.feed_data(b"bye")              # no CR
        node.conn.reader.feed_eof()
        await asyncio.wait_for(node._bridge(circuit), timeout=1.0)
        assert bytes(circuit.writer.buf) == b"bye"

    async def test_bridge_echoes_input_when_echo_local(self):
        """A web (must_echo) user gets local echo through the bridge, since the
        far NET/ROM node won't echo their input back."""
        term = FakeTerminal()
        conn = Connection(
            remote_addr="W6ELA", reader=asyncio.StreamReader(),
            writer=CaptureWriter(), transport_id="web",
        )
        node = NetromNode(
            term=term, conn=conn, user_call="W6ELA",
            node_call="W6ELA-1", node_alias="PALO",
            router=_seeded_router(), transports=[],
            user_eol="\r\n", echo_local=True,
        )
        circuit = FakeCircuit()
        node.conn.reader.feed_data(b"hi\r")            # user types (no far-end echo)
        node.conn.reader.feed_eof()
        await asyncio.wait_for(node._bridge(circuit), timeout=1.0)
        assert bytes(circuit.writer.buf) == b"hi\r"                  # forwarded (bare CR)
        assert bytes(node.conn.writer.buf) == b"hi\r\n"             # echoed to the user (CRLF)

    async def test_bridge_no_echo_by_default(self):
        """Terminals that echo locally (telnet/TNC) must NOT get bridge echo."""
        node = _bridge_node()                          # echo_local defaults False
        circuit = FakeCircuit()
        node.conn.reader.feed_data(b"hi\r")
        node.conn.reader.feed_eof()
        await asyncio.wait_for(node._bridge(circuit), timeout=1.0)
        assert bytes(circuit.writer.buf) == b"hi\r"     # forwarded
        assert bytes(node.conn.writer.buf) == b""       # NOT echoed back

    async def test_bridge_translates_line_endings_for_crlf_user(self):
        """A CRLF terminal (web/TCP): far-end bare-CR lines become CRLF so they
        don't collapse onto one line; the user's CRLF Enter becomes bare CR."""
        term = FakeTerminal()
        conn = Connection(
            remote_addr="W6ELA", reader=asyncio.StreamReader(),
            writer=CaptureWriter(), transport_id="web",
        )
        node = NetromNode(
            term=term, conn=conn, user_call="W6ELA",
            node_call="W6ELA-1", node_alias="PALO",
            router=_seeded_router(), transports=[], user_eol="\r\n",
        )
        circuit = FakeCircuit()
        node.conn.reader.feed_data(b"BYE\r\n")            # user → far
        circuit.reader.feed_data(b"line1\rline2\r")        # far → user (bare CR)
        circuit.reader.feed_eof()
        await asyncio.wait_for(node._bridge(circuit), timeout=1.0)
        assert bytes(circuit.writer.buf) == b"BYE\r"                 # normalized
        assert bytes(node.conn.writer.buf) == b"line1\r\nline2\r\n"  # CRLF for web
