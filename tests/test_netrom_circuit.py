"""
tests/test_netrom_circuit.py — Unit tests for NETROM circuit + manager.

Covers:
  - Local circuit allocation (idx / id, recycling)
  - CONNECT REQ handling: ACK fields, state, on_user_connect dispatch
  - Duplicate CONNECT REQ → re-ACK only
  - INFO handling, INFO ACK output, V(R) advance
  - MORE_FOLLOWS reassembly
  - DISC REQ from peer → ACK + EOF + circuit removed
  - BBS writer: small/large writes (fragmentation), MORE_FOLLOWS flag
  - close() → DISC REQ; DISC ACK → CLOSED
  - V(S) wraparound at 255
  - shutdown() — manager tearing down all circuits
"""
from __future__ import annotations

import asyncio

import pytest

from bbs.ax25.netrom_frame import (
    FLAG_CHOKE,
    FLAG_MORE_FOLLOWS,
    L3_HEADER_LEN,
    L3_INFO_MTU,
    OPCODE_CONNECT_ACK,
    OPCODE_CONNECT_REQ,
    OPCODE_DISCONNECT_ACK,
    OPCODE_DISCONNECT_REQ,
    OPCODE_INFORMATION,
    OPCODE_INFORMATION_ACK,
    ConnectAck,
    ConnectRequest,
    Information,
    InformationAck,
    Disconnect,
    L3Header,
    decode_l3_frame,
    decode_l3_header,
    encode_connect_request_tail,
    encode_l3_frame,
)
from bbs.netrom.circuit import (
    CircuitState,
    NetromCircuit,
    NetromCircuitManager,
)
from bbs.transport.base import Connection


# ── Test fixtures ────────────────────────────────────────────────────────────

class FakeAX25Writer:
    """Records frames written, supports drain(), is_closing() and close()."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closing = False
        self.closed = False
        self.drain_count = 0

    def write(self, data: bytes) -> None:
        self.frames.append(bytes(data))

    async def drain(self) -> None:
        self.drain_count += 1

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closed = True
        self.closing = True


def _connect_req_bytes(
    *,
    origin: str  = "N6ZX-5",
    dest:   str  = "W6ELA-1",
    ttl:    int  = 25,
    cidx:   int  = 11,
    cid:    int  = 22,
    window: int  = 4,
    user:   str  = "KN6PE-7",
    onode:  str  = "N6ZX-5",
) -> bytes:
    header = L3Header(
        origin_call  = origin,
        dest_call    = dest,
        ttl          = ttl,
        circuit_idx  = cidx,
        circuit_id   = cid,
        tx_seq       = 0,
        rx_seq       = 0,
        opcode_flags = OPCODE_CONNECT_REQ,
    )
    return encode_l3_frame(
        header, encode_connect_request_tail(window, user, onode)
    )


class CapturedSessions:
    """Collects Connections passed to on_user_connect; lets tests drive them."""

    def __init__(self) -> None:
        self.connections: list[Connection] = []
        self._gates: list[asyncio.Event] = []   # one per session, blocks until released
        self.released = 0

    async def on_user_connect(self, conn: Connection) -> None:
        self.connections.append(conn)
        gate = asyncio.Event()
        self._gates.append(gate)
        # Wait until the test explicitly releases this session.
        await gate.wait()
        self.released += 1

    def release_all(self) -> None:
        for gate in self._gates:
            gate.set()


@pytest.fixture
def setup():
    """Build a NetromCircuitManager with a FakeAX25Writer and capture sessions."""
    ax25 = FakeAX25Writer()
    sessions = CapturedSessions()
    mgr = NetromCircuitManager(
        local_call      = "W6ELA-1",
        via_node        = "N6ZX-5",
        ax25_writer     = ax25,
        on_user_connect = sessions.on_user_connect,
    )
    return mgr, ax25, sessions


# ── Local allocation ─────────────────────────────────────────────────────────

class TestAllocation:
    def test_first_alloc_is_idx_zero(self, setup):
        mgr, _, _ = setup
        assert mgr._allocate_local_idx() == 0

    def test_smallest_free_idx_returned(self, setup):
        mgr, _, _ = setup
        mgr._used_local_idx.update({0, 1, 3})
        assert mgr._allocate_local_idx() == 2

    def test_local_id_counter_advances(self, setup):
        mgr, _, _ = setup
        a = mgr._allocate_local_id()
        b = mgr._allocate_local_id()
        assert b == (a + 1) & 0xFF

    def test_local_idx_recycled_after_remove(self, setup):
        mgr, _, _ = setup
        idx = mgr._allocate_local_idx()
        mgr._used_local_idx.discard(idx)
        assert mgr._allocate_local_idx() == idx


# ── CONNECT REQUEST handling ─────────────────────────────────────────────────

class TestConnectRequest:
    async def test_circuit_created(self, setup):
        mgr, _, _ = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        assert mgr.circuit_count == 1

    async def test_connect_ack_written(self, setup):
        mgr, ax25, _ = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        # First frame written should be CONNECT ACK.
        assert len(ax25.frames) >= 1
        f = decode_l3_frame(ax25.frames[0])
        assert isinstance(f, ConnectAck)

    async def test_connect_ack_echoes_remote_idx_id(self, setup):
        mgr, ax25, _ = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes(cidx=99, cid=33)))
        header = decode_l3_header(ax25.frames[0])
        assert header.circuit_idx == 99   # remote idx echoed
        assert header.circuit_id  == 33

    async def test_connect_ack_carries_our_idx_id_in_seq_slots(self, setup):
        mgr, ax25, _ = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        header = decode_l3_header(ax25.frames[0])
        circuit = mgr.active_circuits[0]
        assert header.tx_seq == circuit.local_idx
        assert header.rx_seq == circuit.local_id

    async def test_connect_ack_window_clamped(self, setup):
        mgr, ax25, _ = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes(window=200)))
        f = decode_l3_frame(ax25.frames[0])
        assert isinstance(f, ConnectAck)
        assert f.accepted_window <= 7   # _MAX_WINDOW

    async def test_state_becomes_connected(self, setup):
        mgr, _, _ = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        assert mgr.active_circuits[0].state == CircuitState.CONNECTED

    async def test_on_user_connect_called(self, setup):
        mgr, _, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)   # let the task start
        assert len(sessions.connections) == 1
        assert sessions.connections[0].remote_addr == "KN6PE-7"
        assert sessions.connections[0].transport_id == "netrom"
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_duplicate_connect_req_does_not_double_alloc(self, setup):
        mgr, ax25, sessions = setup
        req = _connect_req_bytes(cidx=5, cid=6)
        await mgr.dispatch(decode_l3_frame(req))
        await asyncio.sleep(0)
        # Second identical CONNECT REQ
        await mgr.dispatch(decode_l3_frame(req))
        await asyncio.sleep(0)
        assert mgr.circuit_count == 1
        # Two ACKs were sent (one per REQ) but only one BBS session.
        ack_frames = [
            f for f in ax25.frames
            if (decode_l3_header(f).opcode_flags & 0x07) == OPCODE_CONNECT_ACK
        ]
        assert len(ack_frames) == 2
        assert len(sessions.connections) == 1
        sessions.release_all()
        await asyncio.sleep(0)


class TestCircuitTableFull:
    async def test_connect_req_refused_when_table_full(self, setup):
        mgr, ax25, sessions = setup
        # Saturate the 1-byte local circuit-index space.
        mgr._used_local_idx.update(range(256))
        # Must NOT raise — a raise out of dispatch would bounce the whole
        # AGWPE transport and drop every connected user.
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes(cidx=7, cid=8)))
        await asyncio.sleep(0)
        # No circuit opened, no BBS session started.
        assert mgr.circuit_count == 0
        assert len(sessions.connections) == 0
        # A single CONNECT ACK with the refusal (CHOKE) bit was sent back.
        assert len(ax25.frames) == 1
        f = decode_l3_frame(ax25.frames[0])
        assert isinstance(f, ConnectAck)
        assert f.refused
        # Refusal echoes the originator's idx/id so they can match it to
        # their pending request.
        header = decode_l3_header(ax25.frames[0])
        assert header.circuit_idx == 7
        assert header.circuit_id == 8


# ── INFORMATION handling ─────────────────────────────────────────────────────

class TestInformation:
    async def _connect(self, setup):
        mgr, ax25, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        ax25.frames.clear()   # drop CONNECT ACK noise
        return mgr.active_circuits[0]

    def _info(self, circuit: NetromCircuit, payload: bytes,
              more_follows: bool = False, tx_seq: int | None = None) -> Information:
        flags = OPCODE_INFORMATION | (FLAG_MORE_FOLLOWS if more_follows else 0)
        # A real peer sends each INFO frame with tx_seq = its V(S), which for
        # in-order delivery equals our V(R).  Default to that so successive
        # frames carry incrementing sequence numbers (the duplicate guard in
        # handle_information relies on tx_seq matching V(R)).
        if tx_seq is None:
            tx_seq = circuit.vr
        header = L3Header(
            origin_call  = circuit.origin_node_call,
            dest_call    = circuit.local_call,
            ttl          = 25,
            circuit_idx  = circuit.local_idx,
            circuit_id   = circuit.local_id,
            tx_seq       = tx_seq,
            rx_seq       = 0,
            opcode_flags = flags,
        )
        return Information(header=header, info=payload)

    async def test_payload_fed_to_reader(self, setup):
        mgr, _, sessions = setup
        circuit = await self._connect(setup)
        await mgr.dispatch(self._info(circuit, b"hello\r"))
        data = await asyncio.wait_for(circuit.reader.read(6), timeout=1.0)
        assert data == b"hello\r"
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_info_ack_emitted_after_info(self, setup):
        mgr, ax25, sessions = setup
        circuit = await self._connect(setup)
        await mgr.dispatch(self._info(circuit, b"x"))
        ack_frames = [
            f for f in ax25.frames
            if (decode_l3_header(f).opcode_flags & 0x07) == OPCODE_INFORMATION_ACK
        ]
        assert len(ack_frames) == 1
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_vr_advances(self, setup):
        mgr, _, sessions = setup
        circuit = await self._connect(setup)
        initial_vr = circuit.vr
        await mgr.dispatch(self._info(circuit, b"x"))
        assert circuit.vr == (initial_vr + 1) & 0xFF
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_more_follows_reassembled(self, setup):
        mgr, _, sessions = setup
        circuit = await self._connect(setup)
        # Two fragments, then a final without MORE_FOLLOWS.
        await mgr.dispatch(self._info(circuit, b"foo", more_follows=True))
        # Reader should not have anything yet.
        assert circuit.reader._buffer.__len__() == 0
        await mgr.dispatch(self._info(circuit, b"bar", more_follows=True))
        assert circuit.reader._buffer.__len__() == 0
        await mgr.dispatch(self._info(circuit, b"baz", more_follows=False))
        data = await asyncio.wait_for(circuit.reader.read(9), timeout=1.0)
        assert data == b"foobarbaz"
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_duplicate_info_not_redelivered(self, setup):
        """A retransmitted INFO (tx_seq already consumed) must be re-ACKed but
        NOT fed to the reader again — otherwise the far node's output (e.g. a
        node's help menu) duplicates on the user's screen."""
        mgr, ax25, sessions = setup
        circuit = await self._connect(setup)
        await mgr.dispatch(self._info(circuit, b"hello\r"))     # tx_seq = V(R)
        assert (await asyncio.wait_for(circuit.reader.read(6), timeout=1.0)) == b"hello\r"
        vr_after = circuit.vr
        ax25.frames.clear()
        # Peer didn't get our ACK → retransmits the SAME frame (stale tx_seq).
        dup = self._info(circuit, b"hello\r", tx_seq=(vr_after - 1) & 0xFF)
        await mgr.dispatch(dup)
        assert circuit.vr == vr_after                           # V(R) not advanced
        assert circuit.reader._buffer.__len__() == 0            # not re-delivered
        acks = [f for f in ax25.frames
                if (decode_l3_header(f).opcode_flags & 0x07) == OPCODE_INFORMATION_ACK]
        assert len(acks) == 1                                   # but re-ACKed
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_reassembly_buffer_is_bounded(self, setup):
        """A never-terminating MORE_FOLLOWS run must not grow memory without
        bound — the partial is dropped once it exceeds the ceiling."""
        from bbs.netrom.circuit import _MAX_REASSEMBLY_BYTES
        mgr, _, sessions = setup
        circuit = await self._connect(setup)
        chunk = b"A" * 4096
        # Feed more MORE_FOLLOWS fragments than the ceiling allows, never
        # sending a terminating fragment.
        n = (_MAX_REASSEMBLY_BYTES // len(chunk)) + 5
        for _ in range(n):
            await mgr.dispatch(self._info(circuit, chunk, more_follows=True))
            assert len(circuit._reassembly) <= _MAX_REASSEMBLY_BYTES
        # Nothing was flushed to the BBS reader (no terminating fragment).
        assert circuit.reader._buffer.__len__() == 0
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_info_for_unknown_circuit_dropped(self, setup):
        mgr, ax25, _ = setup
        # No CONNECT REQ — circuit doesn't exist.
        bogus_header = L3Header(
            origin_call  = "W1AW",
            dest_call    = "W6ELA-1",
            ttl          = 25,
            circuit_idx  = 200,
            circuit_id   = 200,
            tx_seq       = 0,
            rx_seq       = 0,
            opcode_flags = OPCODE_INFORMATION,
        )
        await mgr.dispatch(Information(header=bogus_header, info=b"x"))
        # No frames written in response.
        assert ax25.frames == []


# ── INFO ACK handling ────────────────────────────────────────────────────────

class TestInfoAck:
    async def test_info_ack_does_not_emit_anything(self, setup):
        mgr, ax25, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        ax25.frames.clear()
        circuit = mgr.active_circuits[0]
        header = L3Header(
            origin_call  = circuit.origin_node_call,
            dest_call    = circuit.local_call,
            ttl          = 25,
            circuit_idx  = circuit.local_idx,
            circuit_id   = circuit.local_id,
            tx_seq       = 0,
            rx_seq       = 1,
            opcode_flags = OPCODE_INFORMATION_ACK,
        )
        await mgr.dispatch(InformationAck(header=header))
        assert ax25.frames == []
        sessions.release_all()
        await asyncio.sleep(0)


# ── DISC REQ handling ────────────────────────────────────────────────────────

class TestDisconnectFromPeer:
    async def test_peer_disc_req_yields_ack_and_eof(self, setup):
        mgr, ax25, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        ax25.frames.clear()
        circuit = mgr.active_circuits[0]
        disc_header = L3Header(
            origin_call  = circuit.origin_node_call,
            dest_call    = circuit.local_call,
            ttl          = 25,
            circuit_idx  = circuit.local_idx,
            circuit_id   = circuit.local_id,
            tx_seq       = 0,
            rx_seq       = 0,
            opcode_flags = OPCODE_DISCONNECT_REQ,
        )
        await mgr.dispatch(Disconnect(header=disc_header))
        # DISC ACK emitted.
        ack = decode_l3_header(ax25.frames[0])
        assert (ack.opcode_flags & 0x07) == OPCODE_DISCONNECT_ACK
        # Circuit removed, state CLOSED, reader at EOF.
        assert mgr.circuit_count == 0
        assert circuit.state == CircuitState.CLOSED
        assert circuit.reader.at_eof()
        sessions.release_all()
        await asyncio.sleep(0)


# ── BBS writer (outbound INFO) ───────────────────────────────────────────────

class TestBBSWriter:
    async def _circuit_only(self, setup):
        mgr, ax25, _sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        ax25.frames.clear()
        return mgr.active_circuits[0], ax25

    async def test_small_write_one_info_frame(self, setup):
        circuit, ax25 = await self._circuit_only(setup)
        circuit.writer.write(b"hello\r")
        await circuit.writer.drain()
        assert len(ax25.frames) == 1
        f = decode_l3_frame(ax25.frames[0])
        assert isinstance(f, Information)
        assert f.info == b"hello\r"
        assert f.header.more_follows is False

    async def test_large_write_fragments_with_more_follows(self, setup):
        circuit, ax25 = await self._circuit_only(setup)
        payload = b"X" * (L3_INFO_MTU * 2 + 50)   # 522 bytes — 3 fragments
        circuit.writer.write(payload)
        await circuit.writer.drain()
        assert len(ax25.frames) == 3
        f0 = decode_l3_frame(ax25.frames[0])
        f1 = decode_l3_frame(ax25.frames[1])
        f2 = decode_l3_frame(ax25.frames[2])
        assert f0.header.more_follows is True
        assert f1.header.more_follows is True
        assert f2.header.more_follows is False
        assert len(f0.info) == L3_INFO_MTU
        assert len(f1.info) == L3_INFO_MTU
        assert len(f2.info) == 50

    async def test_writer_uses_remote_circuit_idx_id(self, setup):
        circuit, ax25 = await self._circuit_only(setup)
        circuit.writer.write(b"x")
        await circuit.writer.drain()
        header = decode_l3_header(ax25.frames[0])
        assert header.circuit_idx == circuit.remote_idx
        assert header.circuit_id  == circuit.remote_id

    async def test_writer_increments_vs(self, setup):
        circuit, ax25 = await self._circuit_only(setup)
        initial_vs = circuit.vs
        # Send exactly window-many fragments so drain() completes without
        # blocking on an ACK we wouldn't get from the fake AX.25 writer.
        per_frag = L3_INFO_MTU
        circuit.writer.write(b"a" * (per_frag * circuit.accepted_window))
        await circuit.writer.drain()
        assert circuit.vs == (initial_vs + circuit.accepted_window) & 0xFF

    async def test_write_dropped_when_disconnecting(self, setup):
        circuit, ax25 = await self._circuit_only(setup)
        circuit.state = CircuitState.DISCONNECTING
        circuit.writer.write(b"banned")
        await circuit.writer.drain()
        assert ax25.frames == []

    async def test_close_initiates_disconnect(self, setup):
        circuit, ax25 = await self._circuit_only(setup)
        circuit.writer.close()
        assert circuit.state == CircuitState.DISCONNECTING
        f = decode_l3_frame(ax25.frames[0])
        assert isinstance(f, Disconnect)
        assert f.header.opcode == OPCODE_DISCONNECT_REQ


# ── DISC ACK after we initiate ───────────────────────────────────────────────

class TestDisconnectFromUs:
    async def test_disc_ack_closes_circuit(self, setup):
        mgr, ax25, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        # We initiate disconnect.
        circuit.writer.close()
        assert circuit.state == CircuitState.DISCONNECTING
        # Peer DISC ACK arrives.
        ack_header = L3Header(
            origin_call  = circuit.origin_node_call,
            dest_call    = circuit.local_call,
            ttl          = 25,
            circuit_idx  = circuit.local_idx,
            circuit_id   = circuit.local_id,
            tx_seq       = 0,
            rx_seq       = 0,
            opcode_flags = OPCODE_DISCONNECT_ACK,
        )
        await mgr.dispatch(Disconnect(header=ack_header))
        assert circuit.state == CircuitState.CLOSED
        assert mgr.circuit_count == 0
        sessions.release_all()
        await asyncio.sleep(0)


# ── Sequence wraparound ──────────────────────────────────────────────────────

class TestSequenceWraparound:
    async def test_vs_wraps_at_256(self, setup):
        mgr, ax25, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        # Set both V(S) and V(A) at 255 so the window has 4 slots free —
        # the write can proceed without blocking on the window guard.
        circuit.vs = 255
        circuit.va = 255
        ax25.frames.clear()
        circuit.writer.write(b"x")
        await circuit.writer.drain()
        # First fragment used V(S)=255, increment wraps to 0.
        assert circuit.vs == 0
        sessions.release_all()
        await asyncio.sleep(0)


# ── Outbound window enforcement (V1.1) ───────────────────────────────────────

class TestOutboundWindow:
    """V(A) tracking and window-blocking in drain().

    NETROM L3 has its own send window, independent of AX.25's L2 window.
    Without enforcement, BBS output longer than ~window frames sees mid-
    stream chunks silently dropped at the peer (they arrive within AX.25
    window but outside NETROM window).  These tests cover the V(A) book-
    keeping that fixes it.
    """

    async def _circuit(self, setup, *, window: int = 4):
        mgr, ax25, _sessions = setup
        await mgr.dispatch(
            decode_l3_frame(_connect_req_bytes(window=window))
        )
        await asyncio.sleep(0)
        ax25.frames.clear()
        return mgr.active_circuits[0], ax25

    def _ack(self, circuit: NetromCircuit, rx_seq: int) -> InformationAck:
        header = L3Header(
            origin_call  = circuit.origin_node_call,
            dest_call    = circuit.local_call,
            ttl          = 25,
            circuit_idx  = circuit.local_idx,
            circuit_id   = circuit.local_id,
            tx_seq       = 0,
            rx_seq       = rx_seq,
            opcode_flags = OPCODE_INFORMATION_ACK,
        )
        return InformationAck(header=header)

    def _info(self, circuit: NetromCircuit, payload: bytes, rx_seq: int) -> Information:
        # Inbound INFO carrying a piggyback ACK in rx_seq.
        header = L3Header(
            origin_call  = circuit.origin_node_call,
            dest_call    = circuit.local_call,
            ttl          = 25,
            circuit_idx  = circuit.local_idx,
            circuit_id   = circuit.local_id,
            tx_seq       = 0,
            rx_seq       = rx_seq,
            opcode_flags = OPCODE_INFORMATION,
        )
        return Information(header=header, info=payload)

    async def test_initial_va_is_zero(self, setup):
        circuit, _ = await self._circuit(setup)
        assert circuit.va == 0
        assert circuit._frames_in_flight() == 0

    async def test_exactly_window_frames_send_immediately(self, setup):
        circuit, ax25 = await self._circuit(setup, window=4)
        # Queue 4 small frames (each well under the L3_INFO_MTU).
        for line in (b"a\r", b"b\r", b"c\r", b"d\r"):
            circuit.writer.write(line)
        await circuit.writer.drain()
        assert len(ax25.frames) == 4
        # All four are in-flight, waiting on V(A).
        assert circuit._frames_in_flight() == 4

    async def test_window_plus_one_blocks_until_ack(self, setup, mgr_pump=None):
        circuit, ax25 = await self._circuit(setup, window=4)
        # Queue 5 frames — one more than the window.
        for line in (b"a\r", b"b\r", b"c\r", b"d\r", b"e\r"):
            circuit.writer.write(line)
        drain_task = asyncio.create_task(circuit.writer.drain())
        # Give the drain a few scheduler ticks to flush whatever it can.
        for _ in range(5):
            await asyncio.sleep(0)
        # First four frames should be out; the fifth is blocked on the window.
        assert len(ax25.frames) == 4
        assert not drain_task.done()
        assert circuit._frames_in_flight() == 4

        # Peer ACKs the first frame → V(A) advances 0 → 1.
        circuit.handle_information_ack(self._ack(circuit, rx_seq=1))
        for _ in range(5):
            await asyncio.sleep(0)
        await drain_task   # finishes once the 5th frame is out
        assert len(ax25.frames) == 5
        assert circuit._frames_in_flight() == 4   # V(S)=5, V(A)=1

    async def test_info_ack_advances_va(self, setup):
        circuit, _ = await self._circuit(setup)
        circuit.writer.write(b"a\r")
        await circuit.writer.drain()
        assert circuit.vs == 1 and circuit.va == 0
        circuit.handle_information_ack(self._ack(circuit, rx_seq=1))
        assert circuit.va == 1

    async def test_inbound_info_piggyback_ack_advances_va(self, setup):
        """An inbound INFO frame's rx_seq is a piggyback ACK of our send
        queue and must advance V(A) just like a dedicated INFO ACK."""
        circuit, _ = await self._circuit(setup)
        circuit.writer.write(b"a\r")
        await circuit.writer.drain()
        # Peer sends us a piggyback ACK in their next INFO.
        circuit.handle_information(self._info(circuit, b"reply\r", rx_seq=1))
        assert circuit.va == 1

    async def test_close_unblocks_waiting_sender(self, setup):
        """If the circuit closes while a drain is blocked on the window,
        the drain must observe the state change and bail out cleanly
        rather than waiting forever for an ACK that will never come."""
        circuit, ax25 = await self._circuit(setup, window=2)
        circuit.writer.write(b"a\r")
        circuit.writer.write(b"b\r")
        circuit.writer.write(b"c\r")   # 3rd frame will block on window
        drain_task = asyncio.create_task(circuit.writer.drain())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not drain_task.done()
        assert len(ax25.frames) == 2

        # Circuit closes (e.g. peer DISC arrived) — drain should return.
        circuit._set_closed()
        await asyncio.wait_for(drain_task, timeout=1.0)
        # Pending queue was cleared on the way out — no 3rd frame on wire.
        assert len(ax25.frames) == 2

    async def test_va_wraparound_keeps_in_flight_correct(self, setup):
        circuit, _ = await self._circuit(setup)
        # Simulate state near the wraparound: V(S)=2, V(A)=254 means we
        # sent frames 254 and 255 (which wrapped through 0,1 to 2) — well,
        # really 254, 255, 0, 1 → 4 in flight.
        circuit.vs = 2
        circuit.va = 254
        assert circuit._frames_in_flight() == 4

    async def test_in_flight_count_post_ack(self, setup):
        circuit, _ = await self._circuit(setup)
        for _ in range(3):
            circuit.writer.write(b"x\r")
        await circuit.writer.drain()
        assert circuit._frames_in_flight() == 3
        circuit.handle_information_ack(self._ack(circuit, rx_seq=2))
        assert circuit._frames_in_flight() == 1


# ── Outbound CONNECT REQUEST (originator) ────────────────────────────────────

class TestOriginate:
    """``NetromCircuitManager.originate_circuit()`` — outbound L3 CONNECT.

    Covers:
      - CONNECT REQ frame structure on the wire (header + tail)
      - Returns the circuit after CONNECT ACK arrives
      - Learns peer's idx/id from CONNECT ACK bytes 17/18
      - Window downgrade by responder
      - Refused CONNECT ACK raises ConnectionRefusedError
      - Timeout raises asyncio.TimeoutError + cleans up the circuit
      - INFO frames after CONNECT use peer's learned idx/id
    """

    def _ack(
        self,
        circuit: NetromCircuit,
        *,
        accepted_window: int  = 4,
        refused:         bool = False,
        responder_idx:   int  = 7,
        responder_id:    int  = 42,
    ) -> ConnectAck:
        """Build a CONNECT ACK as the responder would, addressed at *circuit*."""
        flags = OPCODE_CONNECT_ACK | (0x80 if refused else 0)
        header = L3Header(
            origin_call  = circuit.origin_node_call,
            dest_call    = circuit.local_call,
            ttl          = 25,
            circuit_idx  = circuit.local_idx,    # echoes originator's
            circuit_id   = circuit.local_id,
            tx_seq       = responder_idx,        # responder's idx/id in
            rx_seq       = responder_id,         # the tx_seq / rx_seq slots
            opcode_flags = flags,
        )
        return ConnectAck(
            header          = header,
            accepted_window = accepted_window,
            refused         = refused,
        )

    async def test_originate_returns_connected_circuit(self, setup):
        mgr, ax25, _ = setup
        task = asyncio.create_task(
            mgr.originate_circuit("PALO", "W6ELA", timeout=2.0)
        )
        await asyncio.sleep(0)
        # CONNECT REQ should be on the wire by now.
        assert len(ax25.frames) == 1
        f = decode_l3_frame(ax25.frames[0])
        assert isinstance(f, ConnectRequest)
        circuit = mgr.active_circuits[0]
        await mgr.dispatch(self._ack(circuit))
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is circuit
        assert result.state == CircuitState.CONNECTED

    async def test_connect_req_structure(self, setup):
        mgr, ax25, _ = setup
        task = asyncio.create_task(
            mgr.originate_circuit("PALO", "W6ELA", proposed_window=4)
        )
        await asyncio.sleep(0)
        f = decode_l3_frame(ax25.frames[0])
        assert isinstance(f, ConnectRequest)
        # Header: origin = us, dest = the node we're connecting to.
        assert f.header.origin_call == "W6ELA-1"
        assert f.header.dest_call   == "PALO"
        assert f.header.tx_seq      == 0
        assert f.header.rx_seq      == 0
        # Tail: user, then origin node (us).
        assert f.user_call          == "W6ELA"
        assert f.origin_node_call   == "W6ELA-1"
        assert f.proposed_window    == 4
        circuit = mgr.active_circuits[0]
        await mgr.dispatch(self._ack(circuit))
        await task

    async def test_originate_learns_remote_idx_id(self, setup):
        mgr, _, _ = setup
        task = asyncio.create_task(mgr.originate_circuit("PALO", "W6ELA"))
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        await mgr.dispatch(self._ack(
            circuit, responder_idx=11, responder_id=99,
        ))
        await task
        assert circuit.remote_idx == 11
        assert circuit.remote_id  == 99

    async def test_originate_window_clamped_to_responder(self, setup):
        mgr, _, _ = setup
        task = asyncio.create_task(
            mgr.originate_circuit("PALO", "W6ELA", proposed_window=7)
        )
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        # Responder downgrades to 2.
        await mgr.dispatch(self._ack(circuit, accepted_window=2))
        await task
        assert circuit.accepted_window == 2

    async def test_originate_refused_raises_connection_refused(self, setup):
        mgr, _, _ = setup
        task = asyncio.create_task(
            mgr.originate_circuit("PALO", "W6ELA", timeout=2.0)
        )
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        await mgr.dispatch(self._ack(circuit, refused=True))
        with pytest.raises(ConnectionRefusedError):
            await asyncio.wait_for(task, timeout=1.0)
        # Circuit gets cleaned up.
        assert mgr.circuit_count == 0

    async def test_originate_timeout_cleans_up(self, setup):
        mgr, _, _ = setup
        with pytest.raises(asyncio.TimeoutError):
            await mgr.originate_circuit("PALO", "W6ELA", timeout=0.05)
        assert mgr.circuit_count == 0

    async def test_post_connect_info_uses_remote_idx_id(self, setup):
        mgr, ax25, _ = setup
        task = asyncio.create_task(mgr.originate_circuit("PALO", "W6ELA"))
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        await mgr.dispatch(self._ack(
            circuit, responder_idx=11, responder_id=99,
        ))
        await task
        ax25.frames.clear()
        circuit.writer.write(b"hi\r")
        await circuit.writer.drain()
        header = decode_l3_header(ax25.frames[0])
        # Outbound INFO addresses the recipient with THEIR idx/id.
        assert header.circuit_idx == 11
        assert header.circuit_id  == 99


# ── Manager shutdown ─────────────────────────────────────────────────────────

class TestShutdown:
    async def test_shutdown_closes_all_circuits(self, setup):
        mgr, _, sessions = setup
        # Open two circuits from two different remote idx/ids.
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes(cidx=1, cid=1)))
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes(cidx=2, cid=2,
                                                              user="W1AW")))
        await asyncio.sleep(0)
        assert mgr.circuit_count == 2
        circuits = list(mgr.active_circuits)
        mgr.shutdown()
        assert mgr.circuit_count == 0
        for c in circuits:
            assert c.state == CircuitState.CLOSED
            assert c.reader.at_eof()
        sessions.release_all()
        await asyncio.sleep(0)


# ── Writer is_closing() reflects circuit state ───────────────────────────────

class TestWriterIsClosing:
    async def test_open_circuit_is_not_closing(self, setup):
        mgr, _, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        assert circuit.writer.is_closing() is False
        sessions.release_all()
        await asyncio.sleep(0)

    async def test_disconnecting_is_closing(self, setup):
        mgr, _, sessions = setup
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
        await asyncio.sleep(0)
        circuit = mgr.active_circuits[0]
        circuit.state = CircuitState.DISCONNECTING
        assert circuit.writer.is_closing() is True
        sessions.release_all()
        await asyncio.sleep(0)


# ── Idle-crosslink reaper (mirrors the Linux AX.25 IDLE timer) ────────────────

def _mgr_with_idle(timeout: float):
    """A manager whose crosslink self-disconnects after `timeout` idle seconds."""
    ax25 = FakeAX25Writer()
    sessions = CapturedSessions()
    mgr = NetromCircuitManager(
        local_call        = "W6ELA-1",
        via_node          = "N6ZX-5",
        ax25_writer       = ax25,
        on_user_connect   = sessions.on_user_connect,
        link_idle_timeout = timeout,
    )
    return mgr, ax25, sessions


def _disc_req_frame(circuit: NetromCircuit) -> Disconnect:
    """A peer DISC REQ addressed at *circuit* (closes it → removes it)."""
    header = L3Header(
        origin_call  = circuit.origin_node_call,
        dest_call    = circuit.local_call,
        ttl          = 25,
        circuit_idx  = circuit.local_idx,
        circuit_id   = circuit.local_id,
        tx_seq       = 0,
        rx_seq       = 0,
        opcode_flags = OPCODE_DISCONNECT_REQ,
    )
    return Disconnect(header=header)


async def _open_one(mgr) -> NetromCircuit:
    await mgr.dispatch(decode_l3_frame(_connect_req_bytes()))
    await asyncio.sleep(0)
    return mgr.active_circuits[0]


class TestIdleReaper:
    async def test_construction_arms_reaper_for_circuitless_crosslink(self):
        """A crosslink born with zero circuits (e.g. an outbound connect_out
        link before/without originate_circuit) must arm the reaper at
        construction so it self-disconnects instead of leaking."""
        mgr, ax25, sessions = _mgr_with_idle(60.0)
        assert mgr._idle_handle is not None        # armed immediately, no circuit
        mgr._reap_idle()                           # circuit-less → reaps
        assert ax25.closed is True
        mgr._cancel_idle_timer()                   # don't leak the pending timer

    async def test_construction_no_arm_when_disabled(self):
        """link_idle_timeout <= 0 keeps the crosslink up indefinitely, even
        when born circuit-less."""
        mgr, ax25, sessions = _mgr_with_idle(0.0)
        assert mgr._idle_handle is None
        assert ax25.closed is False

    async def test_first_circuit_cancels_construction_timer(self):
        """Opening the first circuit on a freshly-built crosslink cancels the
        construction-armed reaper."""
        mgr, ax25, sessions = _mgr_with_idle(60.0)
        assert mgr._idle_handle is not None
        await _open_one(mgr)
        assert mgr._idle_handle is None            # first circuit cancelled it
        sessions.release_all(); await asyncio.sleep(0)

    async def test_last_circuit_close_arms_timer(self):
        mgr, ax25, sessions = _mgr_with_idle(60.0)
        c = await _open_one(mgr)
        assert mgr._idle_handle is None            # active circuit → not armed
        await mgr.dispatch(_disc_req_frame(c))     # peer disconnects
        assert mgr.circuit_count == 0
        assert mgr._idle_handle is not None        # armed once circuit-less
        mgr._cancel_idle_timer()                   # don't leak the pending timer
        sessions.release_all(); await asyncio.sleep(0)

    async def test_new_circuit_cancels_timer(self):
        mgr, ax25, sessions = _mgr_with_idle(60.0)
        c = await _open_one(mgr)
        await mgr.dispatch(_disc_req_frame(c))
        assert mgr._idle_handle is not None
        # A fresh inbound circuit before the timer fires cancels the reaper.
        await mgr.dispatch(decode_l3_frame(_connect_req_bytes(cidx=50, cid=51, user="W1AW")))
        await asyncio.sleep(0)
        assert mgr.circuit_count == 1
        assert mgr._idle_handle is None
        sessions.release_all(); await asyncio.sleep(0)

    async def test_reap_disconnects_crosslink(self):
        mgr, ax25, sessions = _mgr_with_idle(60.0)
        c = await _open_one(mgr)
        await mgr.dispatch(_disc_req_frame(c))
        mgr._reap_idle()                           # fire the reaper directly
        assert ax25.closed is True                 # crosslink 'd' sent
        sessions.release_all(); await asyncio.sleep(0)

    async def test_reap_noop_when_circuit_active(self):
        mgr, ax25, sessions = _mgr_with_idle(60.0)
        await _open_one(mgr)                        # circuit still open
        mgr._reap_idle()                           # stray/late fire
        assert ax25.closed is False                # must NOT cut a live circuit
        sessions.release_all(); await asyncio.sleep(0)

    async def test_disabled_never_arms(self):
        mgr, ax25, sessions = _mgr_with_idle(0.0)   # 0 = disabled
        c = await _open_one(mgr)
        await mgr.dispatch(_disc_req_frame(c))
        assert mgr._idle_handle is None
        assert ax25.closed is False
        sessions.release_all(); await asyncio.sleep(0)

    async def test_timer_fires_and_disconnects(self):
        mgr, ax25, sessions = _mgr_with_idle(0.05)  # 50 ms
        c = await _open_one(mgr)
        await mgr.dispatch(_disc_req_frame(c))
        assert ax25.closed is False
        await asyncio.sleep(0.12)                    # let the real timer fire
        assert ax25.closed is True
        sessions.release_all(); await asyncio.sleep(0)

    async def test_shutdown_cancels_timer(self):
        mgr, ax25, sessions = _mgr_with_idle(60.0)
        c = await _open_one(mgr)
        await mgr.dispatch(_disc_req_frame(c))
        assert mgr._idle_handle is not None
        mgr.shutdown()
        assert mgr._idle_handle is None
        assert mgr._closed is True
