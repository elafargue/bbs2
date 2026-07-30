"""
bbs/netrom/circuit.py — NETROM L3/L4 virtual circuit + per-crosslink manager.

A NETROM circuit is the connection-oriented session that rides on top of an
AX.25 connected-mode "crosslink" between two adjacent nodes.  One AX.25
crosslink can carry MULTIPLE NETROM circuits (multiple end-user sessions
multiplexed through the same neighbor).

──────────────────────────────────────────────────────────────────────────
Roles in the bbs2 implementation
──────────────────────────────────────────────────────────────────────────
We act as a RESPONDER — adjacent NETROM nodes connect TO us on behalf of
their attached users.  We do not originate outbound connections in this
milestone (Milestone 3).

The flow for one incoming user session:

    [adjacent node]                      [us, W6ELA-1]
         │                                    │
         │── CONNECT REQ ────────────────────►│  (handle_connect_request)
         │                                    │   - allocate local idx/id
         │                                    │   - spin up BBS session task
         │◄────────── CONNECT ACK ────────────│   - send CONNECT ACK
         │                                    │
         │── INFO (user keystrokes) ─────────►│  (handle_information)
         │                                    │   - feed to BBS reader
         │◄────────── INFO ACK ───────────────│   - immediate ACK
         │                                    │
         │◄────────── INFO (BBS output) ──────│  (writer.write)
         │── INFO ACK ───────────────────────►│
         │                                    │
         │── DISC REQ ───────────────────────►│  (handle_disconnect)
         │◄────────── DISC ACK ───────────────│   - tear down + EOF to BBS
         │                                    │

──────────────────────────────────────────────────────────────────────────
V1 simplifications (Milestone 3)
──────────────────────────────────────────────────────────────────────────
• No T1 retransmit timer.  NETROM L3 frames ride inside AX.25 connected
  I-frames which already provide L2 ARQ — if the underlying 'D' frame is
  delivered, the L3 frame is delivered.
• Greedy ACK: an INFO ACK is sent immediately after every received INFO.
  Not bandwidth-optimal but correct and simple.
• TX fragmentation at L3_INFO_MTU = 236 bytes.  Inbound MORE_FOLLOWS is
  reassembled.
• No CHOKE / NAK.

──────────────────────────────────────────────────────────────────────────
Outbound window enforcement (V1.1, 2026-06-12)
──────────────────────────────────────────────────────────────────────────
AX.25 ARQ at L2 guarantees byte delivery but NOT L3 frame acceptance —
peer's NETROM stack can validly drop frames that arrive *within* the
AX.25 window but *outside* the NETROM L3 send window we negotiated in
CONNECT REQ/ACK.  Symptom: BBS output longer than ~4 INFO frames sees
mid-stream chunks silently dropped at the receiver (lines missing
entirely from the user's terminal even though AX.25 RR ack'd them).

Fix: track V(A) (the last sequence the peer has acknowledged) and
block ``_NetromCircuitWriter.drain()`` when ``V(S) - V(A) >= window``.
V(A) advances on INFO ACK and on the piggyback ``rx_seq`` of any
inbound INFO frame.  The writer's synchronous ``write()`` just queues
fragments into ``_pending_outbound``; ``drain()`` pulls from that
queue, sending one frame per loop iteration through the window guard.
This is the actual L3 backpressure mechanism the NETROM spec assumes.

──────────────────────────────────────────────────────────────────────────
Circuit identifier convention
──────────────────────────────────────────────────────────────────────────
Each side maintains its own circuit table.  In an established circuit:
  - When WE send an L3 frame, header bytes 15/16 = THEIR (remote) idx/id.
  - When THEY send an L3 frame to us, header bytes 15/16 = OUR (local) idx/id.

CONNECT REQ (from them):  bytes 15/16 = their (remote) idx/id.
CONNECT ACK (from us):    bytes 15/16 echo their idx/id; bytes 17/18 carry
                          our (local) idx/id so they learn it.

Demux on receive: look up by (local_idx, local_id) — that's what arriving
frames address us with.  Duplicate-CONNECT-REQ detection uses
(remote_idx, remote_id) as the secondary index.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Optional

from bbs.ax25.netrom_frame import (
    FLAG_CHOKE,
    FLAG_MORE_FOLLOWS,
    L3_INFO_MTU,
    OPCODE_CONNECT_ACK,
    OPCODE_CONNECT_REQ,
    OPCODE_DISCONNECT_ACK,
    OPCODE_DISCONNECT_REQ,
    OPCODE_INFORMATION,
    OPCODE_INFORMATION_ACK,
    ConnectAck,
    ConnectRequest,
    Disconnect,
    Information,
    InformationAck,
    L3Frame,
    L3Header,
    encode_connect_ack_tail,
    encode_connect_request_tail,
    encode_l3_frame,
)
from bbs.transport.base import Connection

logger = logging.getLogger(__name__)

# Default TTL for outbound L3 frames we originate (CONNECT ACK, INFO, DISC).
# 25 is the live-network typical; NetRomProtocol.pdf recommends "more than
# the network diameter" — 25 amply exceeds any real-world packet network.
_DEFAULT_TTL: int = 25

# Maximum window size we accept from an originator.  AX.25 v2.0 mod-8 caps
# at 7; NetRom typically negotiates 4.  We accept the originator's proposal
# clamped to this ceiling.
_MAX_WINDOW: int = 7

# Maximum number of concurrent circuits on one crosslink — bounded by the
# 1-byte circuit-index space (0–255).  A CONNECT REQ that arrives when the
# table is full is refused with a CONNECT ACK + CHOKE (see
# NetromCircuitManager._send_connect_refusal) rather than raising, so a
# misbehaving neighbor cannot take the whole transport down and the
# originator learns immediately instead of timing out.
_MAX_LOCAL_CIRCUITS: int = 256

# Ceiling on the inbound MORE_FOLLOWS reassembly buffer.  A peer that keeps
# setting MORE_FOLLOWS and never sends a terminating fragment would otherwise
# grow the buffer without bound.  64 KiB is orders of magnitude above any
# legitimate BBS message (bulletins cap at a few KiB), so hitting it means the
# peer is broken or hostile — we drop the partial rather than accumulate.
_MAX_REASSEMBLY_BYTES: int = 64 * 1024


class CircuitState(IntEnum):
    """Lifecycle states of one NETROM circuit."""
    CONNECTING        = 1   # responder: CONNECT REQ received, CONNECT ACK not yet sent
    AWAITING_CONNECT  = 2   # originator: CONNECT REQ sent, awaiting CONNECT ACK
    CONNECTED         = 3   # active session, exchanging INFO
    DISCONNECTING     = 4   # DISC REQ sent (or queued), awaiting DISC ACK
    CLOSED            = 5   # terminal


# ── AX.25 writer protocol ────────────────────────────────────────────────────
#
# The circuit manager needs a duck-typed object that lets it push L3 frame
# bytes onto the underlying AX.25 connected-mode link.  asyncio.StreamWriter
# satisfies this naturally; _AGWPEVirtualWriter does too.

class _AX25WriterProto:
    """Minimal interface the AX.25 layer must expose to the circuit manager."""
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def is_closing(self) -> bool: ...


# ── Per-circuit writer for the BBS session ───────────────────────────────────

class _NetromCircuitWriter:
    """Duck-typed StreamWriter the BBS uses to send to the remote user.

    Bytes written here are fragmented into NETROM INFO frames (≤236 bytes
    each) and pushed to the underlying AX.25 crosslink writer.  Close
    initiates a NETROM DISCONNECT REQUEST.
    """

    def __init__(self, circuit: "NetromCircuit") -> None:
        self._circuit = circuit
        self._closing = False

    def write(self, data: bytes) -> None:
        """Queue *data* for transmission.

        Fragmentation happens here (at L3_INFO_MTU = 236 bytes) but no
        bytes touch the AX.25 writer yet — ``drain()`` is where each
        fragment passes through the V(S)/V(A) window guard before being
        actually sent.  Keeping write() synchronous preserves the
        asyncio.StreamWriter contract the BBS engine relies on.
        """
        if self._closing or not data:
            return
        if self._circuit.state != CircuitState.CONNECTED:
            logger.debug(
                "netrom write dropped — circuit %s not connected (state=%s)",
                self._circuit, self._circuit.state.name,
            )
            return
        offset = 0
        n = len(data)
        mtu = self._circuit.info_mtu
        while offset < n:
            chunk = data[offset: offset + mtu]
            offset += mtu
            self._circuit._pending_outbound.append((chunk, offset < n))

    async def drain(self) -> None:
        """Send any queued fragments through the NETROM L3 window guard.

        Blocks until either every queued fragment has been emitted onto the
        AX.25 writer, or the circuit transitions out of CONNECTED (in which
        case the queue is dropped — nothing to do on a torn-down link).
        """
        while self._circuit._pending_outbound:
            if self._circuit.state != CircuitState.CONNECTED:
                self._circuit._pending_outbound.clear()
                return
            await self._circuit._wait_for_window()
            # Re-check state after the await: the circuit could have closed
            # while we were blocked waiting for window room.
            if self._circuit.state != CircuitState.CONNECTED:
                self._circuit._pending_outbound.clear()
                return
            chunk, more_follows = self._circuit._pending_outbound.popleft()
            self._circuit._send_info_fragment(chunk, more_follows)
            # Drain the AX.25 layer underneath so we don't out-pace it either.
            await self._circuit._drain_ax25()

    def is_closing(self) -> bool:
        return self._closing or self._circuit.state in (
            CircuitState.DISCONNECTING, CircuitState.CLOSED
        )

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._circuit._initiate_disconnect()

    async def wait_closed(self) -> None:
        await self._circuit.wait_closed()

    def get_extra_info(self, key: str, default: Any = None) -> Any:
        return default


# ── NetromCircuit ────────────────────────────────────────────────────────────

@dataclass
class NetromCircuit:
    """One NETROM virtual circuit (one user session)."""

    # Identity
    local_idx:        int
    local_id:         int
    remote_idx:       int
    remote_id:        int
    local_call:       str   # our node callsign (L3 origin when we send)
    user_call:        str   # the originating user
    origin_node_call: str   # the node the user is attached to (L3 dest when we send)
    via_node:         str   # adjacent AX.25 neighbor carrying this circuit

    # State
    state: CircuitState = CircuitState.CONNECTING

    # Sequence variables — modulo 256 per NETROM spec.
    vs: int = 0    # V(S): next tx_seq we will use
    vr: int = 0    # V(R): next tx_seq we expect from them
    va: int = 0    # V(A): next tx_seq peer expects from us (= our last-ack'd + 1)

    # Negotiated window.  Used to gate outbound INFO frames: we never have
    # more than ``accepted_window`` unacknowledged INFO frames in flight.
    accepted_window: int = 4

    # Max info-field bytes per outbound INFO fragment.  Set by the
    # circuit manager from configuration to fit the AX.25 PACLEN of the
    # local TNC: a NETROM L3 frame whose total length (20-byte header +
    # info_mtu payload) exceeds PACLEN will be split by the TNC at L2,
    # producing a header-less second fragment the peer can't decode.
    # Default 108 matches the NORCAL convention of PACLEN=128.
    info_mtu: int = 108

    # Inbound MORE_FOLLOWS reassembly buffer.
    _reassembly: bytearray = field(default_factory=bytearray)

    # Outbound queue and window-room signal.  Fragments produced by
    # _NetromCircuitWriter.write() land in _pending_outbound; drain() pulls
    # one at a time, waiting on _window_event when V(S) - V(A) >= window.
    _pending_outbound: deque = field(default_factory=deque)
    _window_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Streams handed to the BBS session.
    reader: asyncio.StreamReader = field(default_factory=asyncio.StreamReader)
    # `writer` is populated post-init by the manager so it can reference the
    # circuit instance; see NetromCircuitManager._open_circuit().
    writer: Optional[_NetromCircuitWriter] = None

    # Backreference to the underlying AX.25 writer for the crosslink.
    # Populated by the manager.
    _ax25_writer: Optional[_AX25WriterProto] = None

    # Set when the circuit transitions to CLOSED.
    _closed_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Set when the circuit transitions to CONNECTED — used by originators
    # to await their outbound CONNECT REQ being accepted.
    _connected_event: asyncio.Event = field(default_factory=asyncio.Event)

    # ── Outbound: build + send L3 frames ─────────────────────────────────────

    def _build_header(self, opcode_flags: int) -> L3Header:
        return L3Header(
            origin_call  = self.local_call,
            dest_call    = self.origin_node_call,
            ttl          = _DEFAULT_TTL,
            circuit_idx  = self.remote_idx,
            circuit_id   = self.remote_id,
            tx_seq       = self.vs,
            rx_seq       = self.vr,
            opcode_flags = opcode_flags,
        )

    def _send_l3(self, frame_bytes: bytes) -> None:
        assert self._ax25_writer is not None
        if self._ax25_writer.is_closing():
            logger.debug(
                "netrom: AX.25 writer closing for circuit %s — drop L3 frame",
                self,
            )
            return
        self._ax25_writer.write(frame_bytes)

    async def _drain_ax25(self) -> None:
        if self._ax25_writer is not None and not self._ax25_writer.is_closing():
            await self._ax25_writer.drain()

    def _send_connect_request(self, proposed_window: int) -> None:
        """Send a CONNECT REQUEST from this side (we are the originator).

        Outbound-only.  Builds the L3 header with our local idx/id and the
        35-byte CONNECT REQ tail (window + user callsign + origin node).
        For an originated circuit ``origin_node_call`` holds the
        *destination* node, and ``local_call`` is the origin node (we are
        the user's NETROM entry point).
        """
        header = L3Header(
            origin_call  = self.local_call,
            dest_call    = self.origin_node_call,
            ttl          = _DEFAULT_TTL,
            circuit_idx  = self.local_idx,
            circuit_id   = self.local_id,
            tx_seq       = 0,
            rx_seq       = 0,
            opcode_flags = OPCODE_CONNECT_REQ,
        )
        tail = encode_connect_request_tail(
            proposed_window,
            self.user_call,
            self.local_call,   # we are the originating node
        )
        self._send_l3(encode_l3_frame(header, tail))
        logger.info(
            "netrom: circuit %d/%d — CONNECT REQ → %s (user %s, window %d)",
            self.local_idx, self.local_id, self.origin_node_call,
            self.user_call, proposed_window,
        )

    def _send_connect_ack(self, accepted_window: int, refused: bool = False) -> None:
        """Send the CONNECT ACK for this circuit.

        Per spec convention: bytes 15/16 echo the originator's idx/id (so they
        recognize the ACK); bytes 17/18 carry OUR idx/id (so they learn it for
        subsequent frames).
        """
        flags = OPCODE_CONNECT_ACK | (FLAG_CHOKE if refused else 0)
        header = L3Header(
            origin_call  = self.local_call,
            dest_call    = self.origin_node_call,
            ttl          = _DEFAULT_TTL,
            circuit_idx  = self.remote_idx,         # echo originator's
            circuit_id   = self.remote_id,
            tx_seq       = self.local_idx,          # ours, in the seq slots
            rx_seq       = self.local_id,
            opcode_flags = flags,
        )
        self._send_l3(encode_l3_frame(header, encode_connect_ack_tail(accepted_window)))
        if not refused:
            self.state = CircuitState.CONNECTED
            self._connected_event.set()
            logger.info(
                "netrom: circuit %d/%d to %s (user %s via %s) — CONNECT ACK sent",
                self.local_idx, self.local_id,
                self.origin_node_call, self.user_call, self.via_node,
            )

    def _send_info_fragment(self, chunk: bytes, more_follows: bool) -> None:
        flags = OPCODE_INFORMATION | (FLAG_MORE_FOLLOWS if more_follows else 0)
        header = self._build_header(flags)
        self._send_l3(encode_l3_frame(header, chunk))
        self.vs = (self.vs + 1) & 0xFF

    def _send_info_ack(self) -> None:
        header = self._build_header(OPCODE_INFORMATION_ACK)
        self._send_l3(encode_l3_frame(header))

    def _send_disconnect_request(self) -> None:
        header = self._build_header(OPCODE_DISCONNECT_REQ)
        self._send_l3(encode_l3_frame(header))

    def _send_disconnect_ack(self) -> None:
        header = self._build_header(OPCODE_DISCONNECT_ACK)
        self._send_l3(encode_l3_frame(header))

    # ── Window / flow-control helpers ────────────────────────────────────────

    def _frames_in_flight(self) -> int:
        """Number of outbound INFO frames sent but not yet acknowledged.

        Mod-256 distance from V(A) to V(S).  Valid as long as we never have
        more than ~128 unacknowledged frames in flight at once, which the
        window itself enforces.
        """
        return (self.vs - self.va) & 0xFF

    def _advance_va(self, new_va: int) -> None:
        """Update V(A) from a peer's rx_seq.  Wakes any sender that was
        waiting on window room."""
        new_va &= 0xFF
        if new_va != self.va:
            self.va = new_va
            self._window_event.set()

    async def _wait_for_window(self) -> None:
        """Block until ``V(S) - V(A) < accepted_window``.

        Returns immediately if the circuit has left CONNECTED state — the
        caller is expected to re-check state before sending anything else.
        """
        while (
            self._frames_in_flight() >= self.accepted_window
            and self.state == CircuitState.CONNECTED
        ):
            # clear() before wait() is safe: asyncio is single-threaded so
            # no ACK can race in between, and _advance_va()'s set() will
            # wake us up regardless of whether it happens before or after
            # the await below.
            self._window_event.clear()
            await self._window_event.wait()

    # ── Inbound: handle decoded L3 frames ────────────────────────────────────

    def handle_information(self, frame: Information) -> None:
        if len(self._reassembly) + len(frame.info) > _MAX_REASSEMBLY_BYTES:
            # A MORE_FOLLOWS run that never terminates — drop the partial to
            # keep memory bounded.  We still ACK below so the peer's send
            # window keeps advancing rather than wedging on this circuit.
            logger.warning(
                "netrom: circuit %d/%d reassembly exceeded %d bytes without a "
                "terminating fragment — dropping partial message from %s",
                self.local_idx, self.local_id, _MAX_REASSEMBLY_BYTES,
                self.origin_node_call,
            )
            self._reassembly.clear()
        else:
            # Reassembly: stash and wait for non-MORE_FOLLOWS to flush.
            self._reassembly.extend(frame.info)
            if not frame.header.more_follows:
                payload = bytes(self._reassembly)
                self._reassembly.clear()
                if payload:
                    self.reader.feed_data(payload)
        # Advance V(R) and ACK.  We don't enforce strict ordering in V1 —
        # AX.25 ARQ underneath delivers in-order, and out-of-order at NETROM
        # would imply a multi-hop path with reordering, which is rare.
        self.vr = (self.vr + 1) & 0xFF
        # Piggyback ACK: peer's rx_seq tells us how far they've ACK'd OUR
        # send queue.  Advancing V(A) here unblocks any sender waiting on
        # window room.
        self._advance_va(frame.header.rx_seq)
        self._send_info_ack()

    def handle_information_ack(self, frame: InformationAck) -> None:
        logger.debug(
            "netrom: circuit %d/%d INFO ACK from %s (V(A) %d → %d)",
            self.local_idx, self.local_id, self.origin_node_call,
            self.va, frame.header.rx_seq,
        )
        self._advance_va(frame.header.rx_seq)

    def handle_connect_ack(self, frame: ConnectAck) -> None:
        """Inbound CONNECT ACK on an outbound-originated circuit.

        Per NETROM spec convention, the responder's CONNECT ACK echoes our
        local idx/id in bytes 15-16 of the header (that's how the manager
        demuxed it here) and carries the responder's local idx/id in bytes
        17-18 (the tx_seq / rx_seq slots).  We learn those and use them as
        ``remote_idx`` / ``remote_id`` when addressing subsequent INFO
        frames to the responder.
        """
        if self.state != CircuitState.AWAITING_CONNECT:
            logger.warning(
                "netrom: unexpected CONNECT ACK on circuit %d/%d (state=%s)",
                self.local_idx, self.local_id, self.state.name,
            )
            return
        if frame.refused:
            logger.info(
                "netrom: circuit %d/%d → %s — CONNECT REQ refused",
                self.local_idx, self.local_id, self.origin_node_call,
            )
            self._set_closed()
            # _set_closed sets _connected_event too — originator's wait
            # will return, and originate_circuit() inspects state to know
            # this was a refusal rather than a successful connect.
            return
        # Learn peer's idx/id and clamp window if they downgraded.
        self.remote_idx = frame.header.tx_seq & 0xFF
        self.remote_id  = frame.header.rx_seq & 0xFF
        if frame.accepted_window:
            self.accepted_window = min(
                self.accepted_window, frame.accepted_window
            )
        self.state = CircuitState.CONNECTED
        self._connected_event.set()
        logger.info(
            "netrom: circuit %d/%d → %s — CONNECT ACK; remote=%d/%d, window=%d",
            self.local_idx, self.local_id, self.origin_node_call,
            self.remote_idx, self.remote_id, self.accepted_window,
        )

    def handle_disconnect_request(self, _frame: Disconnect) -> None:
        self._send_disconnect_ack()
        logger.info(
            "netrom: circuit %d/%d — DISC REQ from %s, sent DISC ACK, closing",
            self.local_idx, self.local_id, self.origin_node_call,
        )
        self._set_closed()

    def handle_disconnect_ack(self, _frame: Disconnect) -> None:
        if self.state == CircuitState.DISCONNECTING:
            logger.info(
                "netrom: circuit %d/%d — DISC ACK received, closed",
                self.local_idx, self.local_id,
            )
            self._set_closed()
        else:
            logger.debug(
                "netrom: circuit %d/%d — unexpected DISC ACK in state %s",
                self.local_idx, self.local_id, self.state.name,
            )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _initiate_disconnect(self) -> None:
        """Called by the writer.close() when the BBS session ends."""
        if self.state in (CircuitState.DISCONNECTING, CircuitState.CLOSED):
            return
        self._send_disconnect_request()
        self.state = CircuitState.DISCONNECTING
        logger.info(
            "netrom: circuit %d/%d — BBS ended, sent DISC REQ to %s",
            self.local_idx, self.local_id, self.origin_node_call,
        )

    def _set_closed(self) -> None:
        if self.state == CircuitState.CLOSED:
            return
        self.state = CircuitState.CLOSED
        # Wake any sender that was blocked on _wait_for_window so it can
        # observe the closed state and bail out instead of stalling forever.
        self._window_event.set()
        # Same for an originator that was awaiting CONNECT ACK — let them
        # unblock and inspect state to detect refusal / abort.
        self._connected_event.set()
        try:
            self.reader.feed_eof()
        except Exception:
            pass
        self._closed_event.set()

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    def __str__(self) -> str:
        return (
            f"NetromCircuit(user={self.user_call} via={self.via_node} "
            f"local={self.local_idx}/{self.local_id} "
            f"remote={self.remote_idx}/{self.remote_id} state={self.state.name})"
        )


# ── NetromCircuitManager ─────────────────────────────────────────────────────

class NetromCircuitManager:
    """Manages all NETROM circuits for ONE AX.25 crosslink to one adjacent node.

    Constructed per AX.25 connected session that has been classified as
    a NETROM crosslink.  Owns the local circuit-index allocator and the
    demultiplexer for incoming L3 frames.
    """

    def __init__(
        self,
        local_call:       str,
        via_node:         str,
        ax25_writer:      _AX25WriterProto,
        on_user_connect:  Callable[[Connection], Awaitable[None]],
        default_ttl:      int = _DEFAULT_TTL,
        info_mtu:         int = 108,
    ) -> None:
        self._local_call      = local_call
        self._via_node        = via_node
        self._ax25_writer     = ax25_writer
        self._on_user_connect = on_user_connect
        self._default_ttl     = default_ttl
        # Cached for each circuit we create — keep them in sync with the
        # TNC's PACLEN so outbound L3 frames never get split at L2.
        self._info_mtu        = max(1, info_mtu)

        # Demux: incoming frames address us by OUR (local_idx, local_id).
        self._by_local_key:  dict[tuple[int, int], NetromCircuit] = {}
        # Secondary index for duplicate-CONNECT-REQ detection.
        self._by_remote_key: dict[tuple[int, int], NetromCircuit] = {}

        # Local-side allocators.
        self._used_local_idx: set[int] = set()
        self._next_local_id:  int = 0

        # Per-circuit BBS session tasks (so we can cancel them on teardown).
        self._user_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}

    # ── Allocation ───────────────────────────────────────────────────────────

    def _allocate_local_idx(self) -> int:
        for i in range(_MAX_LOCAL_CIRCUITS):
            if i not in self._used_local_idx:
                self._used_local_idx.add(i)
                return i
        # Callers on the inbound path pre-check capacity and refuse the
        # CONNECT REQ gracefully, so this is a backstop for the (unused)
        # originate path only.
        raise RuntimeError("netrom circuit table full (256 local indices in use)")

    def _allocate_local_id(self) -> int:
        # Wrap-around counter — a (local_idx, local_id) pair is unique within
        # the lifetime of this crosslink because local_idx is only reused
        # after a circuit closes.
        self._next_local_id = (self._next_local_id + 1) & 0xFF
        return self._next_local_id

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def dispatch(self, frame: L3Frame) -> None:
        """Route an incoming decoded L3 frame to the appropriate circuit."""
        if isinstance(frame, ConnectRequest):
            await self._handle_connect_request(frame)
            return

        # All non-CONNECT-REQ frames address us by OUR (local) idx/id.
        local_key = (frame.header.circuit_idx, frame.header.circuit_id)
        circuit = self._by_local_key.get(local_key)
        if circuit is None:
            logger.warning(
                "netrom: %s frame for unknown circuit local=%d/%d (via %s) — dropped",
                type(frame).__name__,
                frame.header.circuit_idx, frame.header.circuit_id, self._via_node,
            )
            return

        if isinstance(frame, Information):
            circuit.handle_information(frame)
        elif isinstance(frame, InformationAck):
            circuit.handle_information_ack(frame)
        elif isinstance(frame, Disconnect):
            opcode = frame.header.opcode
            if opcode == OPCODE_DISCONNECT_REQ:
                circuit.handle_disconnect_request(frame)
                self._remove_circuit(circuit)
            else:  # DISCONNECT_ACK
                circuit.handle_disconnect_ack(frame)
                self._remove_circuit(circuit)
        elif isinstance(frame, ConnectAck):
            # Inbound CONNECT ACK: we originated a circuit and the peer is
            # accepting (or refusing) it.  Per spec, bytes 15-16 echo our
            # local idx/id — that's the lookup key.
            circuit.handle_connect_ack(frame)

    def _send_connect_refusal(self, req: ConnectRequest) -> None:
        """Refuse an inbound CONNECT REQ without allocating a circuit.

        Sends a CONNECT ACK with the refusal (CHOKE) bit set so the
        originator learns the node cannot accept the circuit right now
        (e.g. the circuit table is full) instead of timing out.  Echoes
        the originator's idx/id so they can match it to their pending
        request; the seq slots are 0/0 because no local circuit exists.
        """
        if self._ax25_writer.is_closing():
            return
        header = L3Header(
            origin_call  = self._local_call,
            dest_call    = req.origin_node_call,
            ttl          = self._default_ttl,
            circuit_idx  = req.header.circuit_idx,
            circuit_id   = req.header.circuit_id,
            tx_seq       = 0,
            rx_seq       = 0,
            opcode_flags = OPCODE_CONNECT_ACK | FLAG_CHOKE,
        )
        self._ax25_writer.write(
            encode_l3_frame(header, encode_connect_ack_tail(0))
        )

    async def _handle_connect_request(self, frame: ConnectRequest) -> None:
        remote_key = (frame.header.circuit_idx, frame.header.circuit_id)

        # Duplicate-REQ: same (remote_idx, remote_id) means our prior CONNECT
        # ACK was lost.  Resend it.
        existing = self._by_remote_key.get(remote_key)
        if existing is not None:
            logger.info(
                "netrom: duplicate CONNECT REQ remote=%d/%d — resending CONNECT ACK",
                *remote_key,
            )
            existing._send_connect_ack(existing.accepted_window)
            return

        # Refuse gracefully when the local circuit table is full instead of
        # raising out of dispatch (which would bounce the whole transport).
        if len(self._used_local_idx) >= _MAX_LOCAL_CIRCUITS:
            logger.warning(
                "netrom: circuit table full (%d in use) — refusing CONNECT REQ "
                "from user %s (origin %s) via %s",
                _MAX_LOCAL_CIRCUITS, frame.user_call,
                frame.origin_node_call, self._via_node,
            )
            self._send_connect_refusal(frame)
            return

        accepted_window = max(1, min(_MAX_WINDOW, frame.proposed_window))
        circuit = self._open_circuit(frame, accepted_window)

        # Send CONNECT ACK *before* spinning up the BBS session task so the
        # ACK arrives in front of any BBS welcome banner the session emits.
        circuit._send_connect_ack(accepted_window)

        # Build a Connection and hand it to the BBS engine.
        assert circuit.writer is not None
        conn = Connection(
            remote_addr  = circuit.user_call,
            reader       = circuit.reader,
            writer       = circuit.writer,        # type: ignore[arg-type]
            transport_id = "netrom",
            # The L3 destination the user asked to reach (our node call/SSID),
            # so the service dispatcher can route NET/ROM users by called SSID.
            local_addr   = frame.header.dest_call,
        )
        task = asyncio.create_task(
            self._run_user_session(circuit, conn),
            name=f"netrom:session:{circuit.user_call}",
        )
        self._user_tasks[(circuit.local_idx, circuit.local_id)] = task

    async def originate_circuit(
        self,
        dest_node_call:  str,
        user_call:       str,
        *,
        proposed_window: int   = 4,
        timeout:         float = 30.0,
    ) -> NetromCircuit:
        """Originate a NETROM circuit through this AX.25 crosslink to
        *dest_node_call* on behalf of *user_call*.

        The destination is reached either directly (if our crosslink peer
        is the destination) or via NETROM L3 routing at the peer node
        (multi-hop).  Either way, the L3 CONNECT REQ goes onto our
        crosslink writer; the L3 destination address tells the peer (and
        any transit nodes) where to route the circuit.

        Returns the connected NetromCircuit.  Raises ``asyncio.TimeoutError``
        if no CONNECT ACK arrives within *timeout* seconds, or
        ``ConnectionRefusedError`` if the destination explicitly refuses.

        Caller is responsible for cleaning up the circuit (call
        ``circuit.writer.close()``) when the user session ends.
        """
        local_idx = self._allocate_local_idx()
        local_id  = self._allocate_local_id()
        circuit = NetromCircuit(
            local_idx        = local_idx,
            local_id         = local_id,
            remote_idx       = 0,
            remote_id        = 0,
            local_call       = self._local_call,
            user_call        = user_call,
            origin_node_call = dest_node_call,
            via_node         = self._via_node,
            accepted_window  = max(1, min(_MAX_WINDOW, proposed_window)),
            state            = CircuitState.AWAITING_CONNECT,
            info_mtu         = self._info_mtu,
        )
        circuit._ax25_writer = self._ax25_writer
        circuit.writer = _NetromCircuitWriter(circuit)
        self._by_local_key[(local_idx, local_id)] = circuit
        logger.info(
            "netrom: originating circuit %d/%d → %s (user %s, window %d) "
            "via crosslink %s",
            local_idx, local_id, dest_node_call, user_call,
            circuit.accepted_window, self._via_node,
        )
        circuit._send_connect_request(circuit.accepted_window)

        try:
            await asyncio.wait_for(
                circuit._connected_event.wait(), timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "netrom: outbound circuit %d/%d → %s timed out after %.1fs",
                local_idx, local_id, dest_node_call, timeout,
            )
            self._remove_circuit(circuit)
            raise

        if circuit.state != CircuitState.CONNECTED:
            # _connected_event also fires on close (refusal or abort).
            self._remove_circuit(circuit)
            raise ConnectionRefusedError(
                f"NETROM CONNECT to {dest_node_call} via {self._via_node} "
                f"was refused or aborted"
            )
        return circuit

    async def _run_user_session(
        self, circuit: NetromCircuit, conn: Connection
    ) -> None:
        try:
            await self._on_user_connect(conn)
        except Exception:
            logger.exception(
                "netrom: BBS session error for %s on circuit %d/%d",
                circuit.user_call, circuit.local_idx, circuit.local_id,
            )
        finally:
            # BBS session has ended — make sure the circuit is being torn down.
            if circuit.state == CircuitState.CONNECTED:
                circuit._initiate_disconnect()
            self._user_tasks.pop((circuit.local_idx, circuit.local_id), None)

    def _open_circuit(
        self, req: ConnectRequest, accepted_window: int
    ) -> NetromCircuit:
        local_idx = self._allocate_local_idx()
        local_id  = self._allocate_local_id()
        circuit = NetromCircuit(
            local_idx        = local_idx,
            local_id         = local_id,
            remote_idx       = req.header.circuit_idx,
            remote_id        = req.header.circuit_id,
            local_call       = self._local_call,
            user_call        = req.user_call,
            origin_node_call = req.origin_node_call,
            via_node         = self._via_node,
            accepted_window  = accepted_window,
            info_mtu         = self._info_mtu,
        )
        circuit._ax25_writer = self._ax25_writer
        circuit.writer = _NetromCircuitWriter(circuit)
        self._by_local_key[(local_idx, local_id)]   = circuit
        self._by_remote_key[(req.header.circuit_idx, req.header.circuit_id)] = circuit
        logger.info(
            "netrom: opened circuit local=%d/%d for user %s "
            "(origin node %s) via %s",
            local_idx, local_id, req.user_call, req.origin_node_call,
            self._via_node,
        )
        return circuit

    def _remove_circuit(self, circuit: NetromCircuit) -> None:
        self._by_local_key.pop((circuit.local_idx, circuit.local_id), None)
        self._by_remote_key.pop((circuit.remote_idx, circuit.remote_id), None)
        self._used_local_idx.discard(circuit.local_idx)
        task = self._user_tasks.pop((circuit.local_idx, circuit.local_id), None)
        if task and not task.done():
            task.cancel()

    # ── Crosslink teardown ───────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Called when the underlying AX.25 crosslink drops.  Closes all
        circuits and feeds EOF to each BBS session reader."""
        for circuit in list(self._by_local_key.values()):
            circuit._set_closed()
        for task in list(self._user_tasks.values()):
            if not task.done():
                task.cancel()
        self._by_local_key.clear()
        self._by_remote_key.clear()
        self._used_local_idx.clear()
        self._user_tasks.clear()

    # ── Introspection ────────────────────────────────────────────────────────

    @property
    def active_circuits(self) -> list[NetromCircuit]:
        return list(self._by_local_key.values())

    @property
    def circuit_count(self) -> int:
        return len(self._by_local_key)
