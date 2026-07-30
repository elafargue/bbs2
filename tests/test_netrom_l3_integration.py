"""
tests/test_netrom_l3_integration.py — AGWPE classifier + NETROM crosslink
integration tests.

Exercises the full path from an AGWPE 'C' / 'D' / 'd' frame sequence through
the classifier to either:
  - the existing direct-BBS path (no regression), or
  - a NetromCircuitManager that demuxes per-user NETROM circuits.

Uses _FakeWriter to capture outbound AGWPE frames; NETROM L3 frames are
extracted from the 'D' frame payload and decoded with the L3 codec to
verify CONNECT ACK / INFO content on the wire.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from bbs.ax25.netrom_frame import (
    L3_HEADER_LEN,
    OPCODE_CONNECT_ACK,
    OPCODE_DISCONNECT_REQ,
    OPCODE_INFORMATION,
    OPCODE_INFORMATION_ACK,
    ConnectAck,
    Information,
    L3Header,
    decode_l3_frame,
    decode_l3_header,
    encode_connect_request_tail,
    encode_l3_frame,
    OPCODE_CONNECT_REQ,
)
from bbs.netrom.circuit import CircuitState
from bbs.transport.agwpe import AGWPETransport
from bbs.transport.base import Connection


# ── Helpers ──────────────────────────────────────────────────────────────────

_AGWPE_HEADER_FMT = "<BBBBBBBB10s10sii"
_AGWPE_HEADER_LEN = struct.calcsize(_AGWPE_HEADER_FMT)


def _extract_d_pids(buf: bytes) -> list[int]:
    """Pull the AGWPE 'D' header PID byte from each frame in *buf*."""
    pids: list[int] = []
    offset = 0
    while offset + _AGWPE_HEADER_LEN <= len(buf):
        fields = struct.unpack_from(_AGWPE_HEADER_FMT, buf, offset)
        kind_byte = fields[4]
        pid_byte  = fields[6]
        data_len  = fields[10]
        body_end = offset + _AGWPE_HEADER_LEN + data_len
        if body_end > len(buf):
            break
        if chr(kind_byte) == "D":
            pids.append(pid_byte)
        offset = body_end
    return pids


def _make_transport(
    *,
    netrom: bool = False,
    known_neighbors: "list[str] | None" = None,
) -> AGWPETransport:
    """Build a test AGWPETransport.

    ``known_neighbors`` becomes the set the in-test neighbor-check
    predicate consults — pass the caller's callsign to exercise the
    "classify as NETROM crosslink on 'C'" path; omit to exercise the
    cold-start fallback path (caller treated as direct BBS, late-PID
    detection on first 'D').
    """
    t = AGWPETransport({}, "W6ELA-1")
    t._running = True
    t._drain_lock = asyncio.Lock()
    if netrom:
        t.set_netrom_crosslink_enabled(True)
        neighbors = {n.upper() for n in (known_neighbors or [])}
        t.set_netrom_neighbor_check(lambda c: c.upper() in neighbors)
    return t


class _FakeWriter:
    def __init__(self) -> None:
        self.written = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True


def _extract_agwpe_frames(buf: bytes) -> list[tuple[str, bytes]]:
    """Split a fake-writer buffer into a list of (kind, data) tuples."""
    out: list[tuple[str, bytes]] = []
    offset = 0
    while offset + _AGWPE_HEADER_LEN <= len(buf):
        fields = struct.unpack_from(_AGWPE_HEADER_FMT, buf, offset)
        kind_byte = fields[4]
        data_len  = fields[10]
        body_start = offset + _AGWPE_HEADER_LEN
        body_end   = body_start + data_len
        if body_end > len(buf):
            break
        out.append((chr(kind_byte), bytes(buf[body_start:body_end])))
        offset = body_end
    return out


def _outbound_d_payloads(writer: _FakeWriter) -> list[bytes]:
    """Return just the data portions of outbound 'D' frames."""
    return [data for kind, data in _extract_agwpe_frames(writer.written) if kind == "D"]


def _build_netrom_connect_req(
    *,
    origin: str   = "N6ZX-5",
    dest:   str   = "W6ELA-1",
    cidx:   int   = 11,
    cid:    int   = 22,
    window: int   = 4,
    user:   str   = "KN6PE-7",
    onode:  str   = "N6ZX-5",
) -> bytes:
    header = L3Header(
        origin_call  = origin,
        dest_call    = dest,
        ttl          = 25,
        circuit_idx  = cidx,
        circuit_id   = cid,
        tx_seq       = 0,
        rx_seq       = 0,
        opcode_flags = OPCODE_CONNECT_REQ,
    )
    return encode_l3_frame(header, encode_connect_request_tail(window, user, onode))


class _Sessions:
    """Collects Connections handed to _on_connect; lets the test release each."""
    def __init__(self) -> None:
        self.conns: list[Connection] = []
        self.gates: list[asyncio.Event] = []
        self.user_input: list[bytes] = []

    async def on_connect(self, conn: Connection) -> None:
        self.conns.append(conn)
        gate = asyncio.Event()
        self.gates.append(gate)
        # Buffer one chunk of inbound user data, then wait.
        try:
            data = await asyncio.wait_for(conn.reader.read(256), timeout=0.5)
        except asyncio.TimeoutError:
            data = b""
        self.user_input.append(data)
        await gate.wait()

    def release_all(self) -> None:
        for g in self.gates:
            g.set()


# ── No NETROM: confirm the direct-user path is unchanged ─────────────────────

class TestNoNetromRegression:
    async def test_C_immediately_starts_session(self):
        t = _make_transport(netrom=False)
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "W6ELA-7", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0)   # let the session task start

        assert (0, "W6ELA-7") in t._sessions
        sess = t._sessions[(0, "W6ELA-7")]
        assert sess.netrom_manager is None
        assert len(sessions.conns) == 1
        assert sessions.conns[0].remote_addr == "W6ELA-7"
        assert sessions.conns[0].transport_id == "agwpe"
        sessions.release_all()
        await asyncio.sleep(0)


# ── NETROM enabled — direct-user path still works ───────────────────────────

class TestNetromEnabledDirectUser:
    async def test_unknown_caller_starts_bbs_immediately(self):
        """Caller not in router's neighbor set → direct BBS session.
        With router-lookup classification, the decision is made on 'C',
        so the BBS task starts without waiting for any 'D' frame."""
        t = _make_transport(netrom=True)   # no known neighbors
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "W1AW-3", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0)  # let session task pick up

        sess = t._sessions[(0, "W1AW-3")]
        assert sess.netrom_manager is None
        assert len(sessions.conns) == 1
        assert sessions.conns[0].remote_addr == "W1AW-3"
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_unknown_caller_ascii_data_reaches_bbs(self):
        """Caller not in neighbor set: 'D' frames with PID=0xF0 reach the
        BBS reader as user input (no NETROM hijack)."""
        t = _make_transport(netrom=True)   # no known neighbors
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "W1AW-3", "W6ELA-1", 0, b"", writer)
        await t._dispatch("D", 0, "W1AW-3", "W6ELA-1", 0xF0, b"\r", writer)
        await asyncio.sleep(0.01)

        sess = t._sessions[(0, "W1AW-3")]
        assert sess.netrom_manager is None
        assert len(sessions.conns) == 1
        assert sessions.user_input[0] == b"\r"
        sessions.release_all()
        await asyncio.sleep(0)


# ── NETROM enabled — crosslink + user session ───────────────────────────────

class TestNetromCrosslink:
    async def test_known_neighbor_C_creates_manager_with_no_bbs_task(self):
        """The big behavioral guarantee: a 'C' from a known NETROM
        neighbor produces ZERO BBS sessions and ZERO outbound bytes
        before any 'D' arrives.  This is what prevents the "9 seconds of
        banner queued into Direwolf's RF buffer" bug we hit live."""
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0.05)   # give the scheduler several ticks

        sess = t._sessions[(0, "N6ZX-5")]
        # Manager is in place immediately.
        assert sess.netrom_manager is not None
        # Crucially: no BBS session was ever started.
        assert sessions.conns == []
        # And no bytes were sent on the AGWPE TCP socket — no banner
        # could have been queued for Direwolf to transmit.
        assert writer.written == bytearray()

    async def test_connect_req_creates_netrom_manager(self):
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        # First 'D' is a NETROM CONNECT REQ.
        await t._dispatch(
            "D", 0, "N6ZX-5", "W6ELA-1", 0xCF,
            _build_netrom_connect_req(), writer,
        )
        await asyncio.sleep(0.01)

        sess = t._sessions[(0, "N6ZX-5")]
        assert sess.netrom_manager is not None
        assert sess.netrom_manager.circuit_count == 1
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_connect_ack_sent_back(self):
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        await t._dispatch(
            "D", 0, "N6ZX-5", "W6ELA-1", 0xCF,
            _build_netrom_connect_req(cidx=99, cid=33), writer,
        )
        await asyncio.sleep(0.01)

        payloads = _outbound_d_payloads(writer)
        assert len(payloads) >= 1
        ack = decode_l3_frame(payloads[0])
        assert isinstance(ack, ConnectAck)
        # Echoes their idx/id in bytes 15/16
        assert ack.header.circuit_idx == 99
        assert ack.header.circuit_id  == 33
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_on_connect_called_with_user_callsign(self):
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        await t._dispatch(
            "D", 0, "N6ZX-5", "W6ELA-1", 0xCF,
            _build_netrom_connect_req(user="KN6PE-7"), writer,
        )
        await asyncio.sleep(0.01)

        # _on_connect is called by the NetromCircuitManager with the USER
        # callsign — not the crosslink remote (N6ZX-5).
        assert len(sessions.conns) == 1
        assert sessions.conns[0].remote_addr == "KN6PE-7"
        assert sessions.conns[0].transport_id == "netrom"
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_inbound_info_reaches_bbs_reader(self):
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        await t._dispatch(
            "D", 0, "N6ZX-5", "W6ELA-1", 0xCF,
            _build_netrom_connect_req(cidx=11, cid=22), writer,
        )
        await asyncio.sleep(0.01)
        assert len(sessions.conns) == 1
        circuit = list(t._sessions[(0, "N6ZX-5")].netrom_manager.active_circuits)[0]

        # User types "HELP\r" — comes in as a NETROM INFO addressed to our
        # local idx/id.
        info_header = L3Header(
            origin_call  = "N6ZX-5",
            dest_call    = "W6ELA-1",
            ttl          = 25,
            circuit_idx  = circuit.local_idx,
            circuit_id   = circuit.local_id,
            tx_seq       = 0,
            rx_seq       = 0,
            opcode_flags = OPCODE_INFORMATION,
        )
        info_frame = encode_l3_frame(info_header, b"HELP\r")
        await t._dispatch("D", 0, "N6ZX-5", "W6ELA-1", 0xCF, info_frame, writer)
        await asyncio.sleep(0.01)

        # The user_input[0] field collected up to 256 bytes from the reader.
        # It should contain HELP\r.
        assert b"HELP\r" in sessions.user_input[0]
        sessions.release_all()
        await asyncio.sleep(0)


# ── 'd' during NETROM crosslink tears down all circuits ─────────────────────

class TestNetromDisconnect:
    async def test_ax25_drop_closes_all_circuits(self):
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        await t._dispatch(
            "D", 0, "N6ZX-5", "W6ELA-1", 0xCF,
            _build_netrom_connect_req(cidx=11, cid=22), writer,
        )
        await asyncio.sleep(0.01)
        sess = t._sessions[(0, "N6ZX-5")]
        mgr  = sess.netrom_manager
        assert mgr is not None
        circuits = list(mgr.active_circuits)
        assert len(circuits) == 1

        # AX.25 link drops.
        await t._dispatch("d", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)

        # Session gone, manager shut down, circuit closed.
        assert (0, "N6ZX-5") not in t._sessions
        assert mgr.circuit_count == 0
        assert circuits[0].state == CircuitState.CLOSED
        sessions.release_all()
        await asyncio.sleep(0)


# ── Duplicate 'C' on a NETROM crosslink ─────────────────────────────────────

class TestDuplicateConnectOnCrosslink:
    async def test_duplicate_C_shuts_down_old_netrom_manager(self):
        """When the adjacent node's TNC resets, a second 'C' must tear down
        the prior crosslink's circuits — otherwise user sessions ghost."""
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        # First connect + NETROM CONNECT REQ from N6ZX-5.
        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        await t._dispatch(
            "D", 0, "N6ZX-5", "W6ELA-1", 0xCF,
            _build_netrom_connect_req(cidx=11, cid=22, user="KN6PE-7"),
            writer,
        )
        await asyncio.sleep(0.01)
        old_sess = t._sessions[(0, "N6ZX-5")]
        old_mgr = old_sess.netrom_manager
        assert old_mgr is not None
        old_circuits = list(old_mgr.active_circuits)
        assert len(old_circuits) == 1

        # N6ZX-5's TNC resets and sends a fresh SABM.
        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)

        # New session in place; old manager shut down + circuits closed.
        new_sess = t._sessions[(0, "N6ZX-5")]
        assert new_sess is not old_sess
        assert old_mgr.circuit_count == 0
        assert old_circuits[0].state == CircuitState.CLOSED
        sessions.release_all()
        await asyncio.sleep(0)


# ── Late NETROM detection (slow peer) ───────────────────────────────────────

class TestLateNetromDetection:
    """When a NETROM peer takes longer than classify_timeout to send its
    CONNECT REQ — common with KPC-3 / TheNet stacks which routinely run
    2-4 s between AX.25 link-up and L3 — the classifier times out and we
    start a direct BBS session.  A later 'D' frame with PID=0xCF must be
    detected and promote the session into a NETROM crosslink anyway,
    otherwise the peer would see a BBS banner instead of CONNECT ACK and
    we'd feed NETROM binary bytes into the BBS reader as user input.
    """

    PID_NETROM = 0xCF
    PID_NO_L3  = 0xF0

    async def test_late_pid_promotes_to_netrom(self):
        # Very short timeout to make the test fast; the real fix is the
        # PID-on-every-D-frame check, not the timeout value.
        # No known_neighbors → caller treated as unknown → cold-start
        # late-PID path exercises here.
        t = _make_transport(netrom=True)
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        # 'C' arrives, classifier starts.
        await t._dispatch("C", 0, "KF6ANX-4", "W6ELA-1", 0, b"", writer)

        # Unknown caller → direct BBS path, BBS task starts immediately.
        await asyncio.sleep(0)
        sess = t._sessions[(0, "KF6ANX-4")]
        assert sess.netrom_manager is None
        assert len(sessions.conns) == 1
        assert sessions.conns[0].remote_addr == "KF6ANX-4"   # direct path

        # NETROM CONNECT REQ arrives later (e.g. neighbor hadn't been seen
        # in any NODES broadcast yet at startup) — PID=0xCF triggers
        # cold-start promotion.
        await t._dispatch(
            "D", 0, "KF6ANX-4", "W6ELA-1", self.PID_NETROM,
            _build_netrom_connect_req(user="KN6PE-7"), writer,
        )
        await asyncio.sleep(0.01)

        sess = t._sessions[(0, "KF6ANX-4")]
        assert sess.netrom_manager is not None
        assert sess.netrom_manager.circuit_count == 1
        # Two BBS sessions handed out: the original (KF6ANX-4 direct, cancelled)
        # and the new NETROM user session (KN6PE-7).
        assert len(sessions.conns) == 2
        netrom_conn = sessions.conns[1]
        assert netrom_conn.remote_addr == "KN6PE-7"
        assert netrom_conn.transport_id == "netrom"
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_late_pid_cancels_direct_bbs_task(self):
        # No known_neighbors → caller treated as unknown → cold-start
        # late-PID path exercises here.
        t = _make_transport(netrom=True)
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "KF6ANX-4", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0.15)
        old_task = t._session_tasks.get((0, "KF6ANX-4"))
        assert old_task is not None
        assert not old_task.done()

        await t._dispatch(
            "D", 0, "KF6ANX-4", "W6ELA-1", self.PID_NETROM,
            _build_netrom_connect_req(), writer,
        )
        await asyncio.sleep(0.01)
        assert old_task.done() or old_task.cancelled()
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_promotion_sets_outbound_pid_to_netrom(self):
        """Regression for the live KF6ANX-4 bug: after promotion, the AGWPE
        'D' frames carrying NETROM L3 payloads must have PID=0xCF on the
        AX.25 wrapper, not 0xF0.  Without this, peers' AX.25 layer routes
        the payload to their session/text handler instead of their NETROM
        stack and the CONNECT ACK never reaches the remote circuit."""
        # No known_neighbors → caller treated as unknown → cold-start
        # late-PID path exercises here.
        t = _make_transport(netrom=True)
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "KF6ANX-4", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0.15)   # classifier timeout fires → BBS task starts

        # Snapshot the pre-promotion PIDs (any BBS banner went out with 0xF0).
        pre_pids = _extract_d_pids(writer.written)

        # Now NETROM CONNECT REQ arrives → late detection promotes.
        await t._dispatch(
            "D", 0, "KF6ANX-4", "W6ELA-1", self.PID_NETROM,
            _build_netrom_connect_req(), writer,
        )
        await asyncio.sleep(0.05)

        # All NEW 'D' frames (post-promotion) must have PID=0xCF.
        post_pids = _extract_d_pids(writer.written)[len(pre_pids):]
        assert post_pids, "expected at least the CONNECT ACK on the wire"
        assert all(p == self.PID_NETROM for p in post_pids), (
            f"post-promotion D-frame PIDs must all be 0xCF, got {post_pids}"
        )
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_early_classification_sets_outbound_pid_to_netrom(self):
        """Same PID guarantee on the early-classifier path (CONNECT REQ
        arrives before the timeout)."""
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        await t._dispatch(
            "D", 0, "N6ZX-5", "W6ELA-1", self.PID_NETROM,
            _build_netrom_connect_req(), writer,
        )
        await asyncio.sleep(0.01)

        pids = _extract_d_pids(writer.written)
        assert pids, "expected at least the CONNECT ACK on the wire"
        assert all(p == self.PID_NETROM for p in pids), (
            f"all D-frame PIDs on a NETROM crosslink must be 0xCF, got {pids}"
        )
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_promotion_mutes_old_writer(self):
        """Regression for the lenient-peer bug: after promotion, ANY write
        through ``sess.writer`` (which the cancelled BBS task still holds
        a reference to via conn.writer) must NOT reach the wire.  The new
        NETROM circuit uses its own writer with its own state.

        Without muting, the cancelled BBS task's "73 de … -- disconnecting --"
        tail goes out as PID=0xCF I-frames with no NETROM L3 header.  A
        strict peer drops them as malformed L3; a lenient peer (KF6ANX-4
        in the wild) forwards them through to the user as text, jumbled
        with the proper L3-encoded output of the new circuit."""
        # No known_neighbors → caller treated as unknown → cold-start
        # late-PID path exercises here.
        t = _make_transport(netrom=True)
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "KF6ANX-4", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0.15)   # classifier timeout fires
        sess = t._sessions[(0, "KF6ANX-4")]
        old_writer = sess.writer
        await t._dispatch(
            "D", 0, "KF6ANX-4", "W6ELA-1", self.PID_NETROM,
            _build_netrom_connect_req(), writer,
        )
        await asyncio.sleep(0.05)

        # The old writer is now muted; writes to it produce nothing on
        # the underlying TCP socket.
        bytes_before = len(writer.written)
        old_writer.write(b"stale BBS tail bytes that should never reach the wire")
        assert len(writer.written) == bytes_before, (
            "old (muted) writer let bytes through to the AGWPE TCP socket"
        )
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_promotion_does_not_send_agwpe_disconnect(self):
        """Regression for live-deployment bug: when the cancelled BBS task's
        conn.close() cascade reaches _AGWPEVirtualWriter.close(), it must
        NOT send an AGWPE 'd' frame — that would tear down the AX.25 link
        the NETROM crosslink still needs."""
        # No known_neighbors → caller treated as unknown → cold-start
        # late-PID path exercises here.
        t = _make_transport(netrom=True)
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "KF6ANX-4", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0.15)   # let classifier time out
        await t._dispatch(
            "D", 0, "KF6ANX-4", "W6ELA-1", self.PID_NETROM,
            _build_netrom_connect_req(), writer,
        )
        # Let the BBS task's cancellation propagate through its finally
        # block — this is when conn.close() would historically have sent 'd'.
        await asyncio.sleep(0.05)

        # Inspect all AGWPE frames sent out: there must be no 'd' (disconnect)
        # frame for KF6ANX-4 after the promotion.
        disconnect_frames = [
            (kind, data) for kind, data in _extract_agwpe_frames(writer.written)
            if kind == "d"
        ]
        assert disconnect_frames == [], (
            f"Unexpected AGWPE 'd' frames after promotion: {disconnect_frames!r}"
        )
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_pid_F0_after_classification_stays_direct(self):
        # Regression guard: ordinary direct-user data with PID=0xF0 must
        # NOT trigger a NETROM promotion.
        # No known_neighbors → caller treated as unknown → cold-start
        # late-PID path exercises here.
        t = _make_transport(netrom=True)
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "W1AW-3", "W6ELA-1", 0, b"", writer)
        await asyncio.sleep(0.15)

        # Direct user types something.
        await t._dispatch(
            "D", 0, "W1AW-3", "W6ELA-1", self.PID_NO_L3, b"HELP\r", writer,
        )
        await asyncio.sleep(0.01)

        sess = t._sessions[(0, "W1AW-3")]
        assert sess.netrom_manager is None
        # The "HELP\r" should be fed to the BBS reader, not interpreted as
        # a NETROM frame.
        assert b"HELP\r" in sessions.user_input[0]
        sessions.release_all()
        await asyncio.sleep(0)


# ── Classifier resilience ───────────────────────────────────────────────────

class TestClassifierResilience:
    async def test_undecodable_netrom_shaped_payload_logs_but_does_not_crash(self, caplog):
        # Build something that matches the L3 header signature but is too
        # short to be a valid CONNECT REQ tail.
        t = _make_transport(netrom=True, known_neighbors=["N6ZX-5"])
        sessions = _Sessions()
        t._on_connect = sessions.on_connect
        writer = _FakeWriter()

        await t._dispatch("C", 0, "N6ZX-5", "W6ELA-1", 0, b"", writer)
        header = L3Header(
            origin_call  = "N6ZX-5",
            dest_call    = "W6ELA-1",
            ttl          = 25,
            circuit_idx  = 11,
            circuit_id   = 22,
            tx_seq       = 0,
            rx_seq       = 0,
            opcode_flags = OPCODE_CONNECT_REQ,
        )
        # Truncated: header + window byte only, missing user / origin callsigns.
        truncated = encode_l3_frame(header, bytes([4]))
        await t._dispatch("D", 0, "N6ZX-5", "W6ELA-1", 0xCF, truncated, writer)
        await asyncio.sleep(0.01)
        # Manager created (signature matched), but no circuit opened.
        sess = t._sessions[(0, "N6ZX-5")]
        assert sess.netrom_manager is not None
        assert sess.netrom_manager.circuit_count == 0
        sessions.release_all()
        await asyncio.sleep(0)
