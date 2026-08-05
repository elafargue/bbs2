"""
bbs/transport/agwpe.py — AGWPE (AGW Packet Engine) transport.

AGWPE is a packet-radio engine by SV2AGW that exposes an AX.25 API over a
TCP socket (default port 8000).  This transport connects to AGWPE, registers
the BBS callsign, and accepts incoming AX.25 connected-mode sessions.

Protocol reference: http://www.sv2agw.com/downloads/develop.zip

── Frame format (36-byte header + optional data) ────────────────────────────
  Byte   0     : Port       — radio port number (0-based)
  Bytes  1-3   : Reserved
  Byte   4     : DataKind   — frame type (ASCII character code)
  Byte   5     : Reserved
  Byte   6     : PID        — AX.25 protocol identifier (0xF0 = no layer 3)
  Byte   7     : Reserved
  Bytes  8-17  : CallFrom   — source callsign, null-padded to 10 bytes
  Bytes 18-27  : CallTo     — destination callsign, null-padded to 10 bytes
  Bytes 28-31  : DataLen    — payload byte count (int32 little-endian)
  Bytes 32-35  : UserReserved (int32, ignored)

── Relevant DataKind codes ───────────────────────────────────────────────────
  'P' (0x50) — Login to AGWPE (send: data = password; omit if no password set)
  'X' (0x58) — Register callsign (send: CallFrom = our call; reply: Data[0]=1=OK)
  'C' (0x43) — Incoming connected call notification (receive only)
               CallFrom = remote station, CallTo = our callsign
  'D' (0x44) — Connected I-frame data
               Receive: CallFrom = remote  →  send: CallFrom = our call, CallTo = remote
  'd' (0x64) — Disconnect
               Receive: CallFrom = remote  →  send: CallFrom = our call, CallTo = remote
  'M' (0x4D) — Send unproto (UI) frame — used for periodic beacons and
               NETROM NODES broadcasts. Note: SV2AGW spec also defines a
               server→client 'T' confirmation; do NOT confuse the two —
               sending 'T' as a client command makes Direwolf reject the
               frame as INVALID.
  'm' (0x6D) — Enable monitoring of all received frames
  'U' (0x55) — Monitored UI / UNPROTO frame (AGWPE monitor format, e.g.:
               "1:Fm W6ELA-1 To BEACON Via KROCK*,KJOHN* <UI pid=F0 ...>")
  'S' (0x53) — Monitored supervisory + non-UI U-frames (SABM, SABME, UA, DM,
               RR, RNR, REJ, …). Same text format as 'U'; the angle-bracket
               metadata names the actual frame type, e.g. "<SABME PF=1 >".

── Design notes ─────────────────────────────────────────────────────────────
One TCP connection is maintained to AGWPE.  Multiple simultaneous AX.25
sessions (one per remote station) are multiplexed over it.

Each session gets:
  - a plain asyncio.StreamReader, fed via feed_data() as 'D' frames arrive
  - a duck-typed writer (_AGWPEVirtualWriter) that encodes outgoing data as
    'D' frames and disconnect requests as 'd' frames

Using feed_data() on a real StreamReader (rather than the queue-backed
_QueueStreamReader used by KISS) means Terminal.readchar()'s read(1) works
correctly: the reader's internal byte-buffer delivers one byte at a time.

If the AGWPE TCP connection is lost, the transport reconnects automatically
with exponential back-off up to 60 seconds.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from typing import Any, Callable, Optional

# Set this logger to DEBUG in your logging config (or via bbs.yaml) to get
# per-frame traces.  At INFO level only connection events are emitted.
#
# Quick enable at runtime:
#   logging.getLogger('bbs.transport.agwpe').setLevel(logging.DEBUG)

import re

from bbs.ax25.address import format_addr, parse
from bbs.ax25.netrom_frame import (
    PID_NETROM,
    decode_l3_frame,
    looks_like_netrom_l3,
)
from bbs.netrom.circuit import NetromCircuitManager
from bbs.transport.base import Connection, ConnectionCallback, Transport

logger = logging.getLogger(__name__)

# ── Frame format ──────────────────────────────────────────────────────────────
_HEADER_FMT  = "<BBBBBBBB10s10sii"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)   # 36 bytes

assert _HEADER_SIZE == 36, "AGWPE header must be 36 bytes"

_PID_NO_L3 = 0xF0  # no layer-3 protocol

# Timeout for a single readexactly() on the AGWPE TCP socket.
# Applied only to the *payload* read (mid-frame stall detection): once we have
# the 36-byte header we expect the payload to arrive within this window.
# The header read is NOT timed out — during idle periods (no AX.25 activity)
# AGWPE sends nothing at all, which is normal; timing that out would cause
# spurious reconnects.  Dead-connection detection for truly silent links is
# handled by TCP keepalives set on the socket after connect.
_TCP_READ_TIMEOUT = 120  # seconds

# AGWPE 'U' monitor string format (as sent by Direwolf):
#   " 1:Fm CALL To DEST [Via PATH ]<UI pid=F0 Len=NN PF=0 >[HH:MM:SS]\rPAYLOAD\r\r\x00"
# The Via section is absent for direct (non-digipeated) frames.
# The actual AX.25 info payload follows >[HH:MM:SS]\r — everything between
# <...> is AGWPE frame-control metadata, not the payload.
_MONITOR_VIA_RE   = re.compile(r"\bVia\s+([^<\r\n]+?)\s*<", re.IGNORECASE)
_MONITOR_SABM_RE  = re.compile(r"<[^>]*\bSABME?\b", re.IGNORECASE)
_AGWPE_PAYLOAD_RE = re.compile(r">\[[\d:]+\]\r?(.*)", re.DOTALL)

# Used to locate the binary AX.25 info field in a raw AGWPE 'U' monitor frame.
# The TNC2 header ends with ">[HH:MM:SS]\r" (ASCII); the binary payload follows.
_BINARY_INFO_RE   = re.compile(rb">\[\d{2}:\d{2}:\d{2}\]\r?")


def _extract_binary_info(raw: bytes) -> bytes | None:
    """
    Extract the raw binary AX.25 info field from an AGWPE 'U' monitor frame.

    AGWPE 'U' payloads are TNC2-format strings: the ASCII header ends with
    ">[HH:MM:SS]\\r" and the binary info field follows immediately.
    Trailing \\r\\r\\x00 bytes are stripped from the result.
    """
    m = _BINARY_INFO_RE.search(raw)
    if not m:
        return None
    return raw[m.end():].rstrip(b"\r\x00")


def _parse_via(monitor_text: str) -> list[str]:
    """Extract digipeater path from an AGWPE 'U' monitor string."""
    m = _MONITOR_VIA_RE.search(monitor_text)
    if not m:
        return []
    return [v.strip() for v in m.group(1).split(",") if v.strip()]


def _parse_info(monitor_text: str) -> str:
    """
    Extract the information field from an AGWPE 'U' monitor string.

    AGWPE format: 'PORT:Fm CALL To DEST [Via PATH ]<CTRL>[HH:MM:SS]\rPAYLOAD\r\r\x00'
    The payload follows the '>[HH:MM:SS]\r' marker, regardless of whether
    a Via path is present.

    TNC2 fallback: 'CALL>DEST,PATH:INFO'
    """
    m = _AGWPE_PAYLOAD_RE.search(monitor_text)
    if m:
        return m.group(1).strip("\r\n\x00 ")
    # TNC2 fallback: "CALL>DEST,PATH:INFO"
    colon = monitor_text.find(":")
    if colon > 0:
        return monitor_text[colon + 1:].strip()
    return ""


def _append_monitor_log(path: str, text: str) -> None:
    """Append one raw AGWPE 'U' monitor string to *path* (one line per frame)."""
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(path, "a", encoding="ascii", errors="replace") as fh:
            fh.write(f"{ts}\t{text.rstrip()}\n")
    except OSError as exc:
        logger.warning("monitor_log write failed (%s): %s", path, exc)


def _encode_call(callsign: str) -> bytes:
    """Return callsign as a 10-byte null-padded ASCII field."""
    return callsign.upper().encode("ascii")[:9] + b"\x00"


def _decode_call(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()


def _build_frame(
    port: int,
    kind: str,
    call_from: str,
    call_to: str,
    pid: int = 0,
    data: bytes = b"",
) -> bytes:
    """Pack one AGWPE frame (header + data)."""
    return struct.pack(
        _HEADER_FMT,
        port, 0, 0, 0,                           # port + 3 reserved
        ord(kind), 0, pid, 0,                    # DataKind + reserved + PID + reserved
        _encode_call(call_from),
        _encode_call(call_to),
        len(data),
        0,                                        # UserReserved
    ) + data


def _build_unproto_via_frame(
    port: int,
    call_from: str,
    call_to: str,
    via_path: list[str],
    payload: bytes,
) -> bytes:
    """
    Pack an AGWPE 'V' (SendUnprotoVia) frame.

    Data layout for 'V':
      1 byte    : count of via addresses
      N x 10 bytes : via callsigns, each null-padded to 10 bytes (same as header fields)
      followed immediately by the payload bytes.
    """
    fmt = "B" + len(via_path) * "10s"
    via_encoded = [v.upper().encode("ascii")[:9].ljust(10, b"\x00") for v in via_path]
    path_bytes = struct.pack(fmt, len(via_path), *via_encoded)
    data = path_bytes + payload
    return _build_frame(port, "V", call_from, call_to, _PID_NO_L3, data)


# ── Virtual per-session writer ────────────────────────────────────────────────

class _AGWPEVirtualWriter:
    """
    Duck-typed asyncio.StreamWriter for one AGWPE connected session.

    write() wraps outgoing bytes as AGWPE 'D' frames.
    close() sends a 'd' (disconnect) frame to AGWPE.
    All writes go through the shared AGWPE TCP writer.

    drain_lock must be shared across all writers and the beacon loop so that
    concurrent drain() calls on the underlying StreamWriter are serialised.
    Python 3.9 asyncio uses a single _drain_waiter and raises AssertionError
    if two coroutines call drain() concurrently on the same writer.
    """

    def __init__(
        self,
        agwpe_writer: asyncio.StreamWriter,
        local_call: str,
        remote_call: str,
        agw_port: int,
        drain_lock: asyncio.Lock,
        write_timeout: int = 30,
    ) -> None:
        self._w = agwpe_writer
        self._local = local_call
        self._remote = remote_call
        self._port = agw_port
        self._closing = False
        # Set when the session this writer belongs to has been promoted to a
        # NETROM crosslink — future close() calls become no-ops so the
        # underlying AX.25 link stays up for L3 traffic.  write() and
        # drain() continue to work normally.
        self._suppress_close = False
        # Set when we want to silently swallow all subsequent write()
        # calls.  Used during NETROM promotion to keep the cancelled BBS
        # task's late writes off the wire — those bytes wouldn't have
        # proper NETROM L3 framing and would interleave garbage with the
        # real circuit's traffic from the new NETROM session.
        self._muted = False
        # PID byte for outbound AGWPE 'D' frames.  Default 0xF0 (no L3) for
        # direct BBS use; NETROM crosslinks set 0xCF on promotion so peers'
        # AX.25 layer routes the payload to their NETROM handler instead of
        # their session/text handler.
        self._pid: int = _PID_NO_L3
        self._drain_lock = drain_lock
        self._write_timeout = write_timeout

    def set_pid(self, pid: int) -> None:
        """Override the AX.25 PID for all subsequent ``write()`` calls.

        Used during NETROM-crosslink promotion to flip the writer from
        PID=0xF0 (BBS data) to PID=0xCF so outbound L3 frames carry the
        correct PID on the AX.25 wrapper.  Without this, peers receive
        valid NETROM L3 bytes but with an AX.25 PID that says "no layer
        3" — they hand the payload to their session handler instead of
        the NETROM stack and ignore it.
        """
        self._pid = pid & 0xFF

    def mute(self) -> None:
        """Drop all subsequent write() calls — bytes go nowhere.

        Used during NETROM promotion to stop the cancelled BBS task from
        emitting unframed bytes onto an AX.25 link the new NETROM circuit
        is now driving.  Peers' NETROM stacks vary in lenience and some
        will forward those unframed bytes through to the user, interleaved
        with our proper L3-encoded INFO frames — visible to the user as
        garbled or duplicated output.  Muting is cleaner than relying on
        the receiver to drop malformed L3.
        """
        self._muted = True

    def suppress_close(self) -> None:
        """Mark this writer so that ``close()`` becomes a no-op.

        Used when the AGWPE session has been promoted from direct-BBS to a
        NETROM crosslink and the BBS session task is being cancelled — its
        finally block would otherwise cascade into ``conn.close()`` →
        ``writer.close()`` → an AGWPE ``'d'`` frame that tears down the
        AX.25 link the NETROM crosslink still needs.
        """
        self._suppress_close = True

    def write(self, data: bytes) -> None:
        if self._muted or self._closing or not data:
            return
        frame = _build_frame(self._port, "D", self._local, self._remote, self._pid, data)
        self._w.write(frame)
        logger.debug(
            "agwpe TX [D] %s→%s pid=0x%02x %d bytes: %r",
            self._local, self._remote, self._pid, len(data), data[:80],
        )

    async def drain(self) -> None:
        # Check _closing INSIDE the lock so we never drain a writer that was
        # closed between the check and the lock acquisition.
        async with self._drain_lock:
            if not self._closing:
                try:
                    await asyncio.wait_for(
                        self._w.drain(), timeout=self._write_timeout
                    )
                except asyncio.TimeoutError:
                    self._closing = True
                    raise ConnectionResetError(
                        f"agwpe write timeout after {self._write_timeout}s"
                    )

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if self._closing:
            return
        if self._suppress_close:
            # Session was promoted to a NETROM crosslink — keep the AX.25
            # link up for L3 traffic.  Do NOT set _closing here either;
            # write() and drain() must continue to work for outbound NETROM
            # frames on the same underlying TCP writer.
            logger.debug(
                "agwpe TX [d] suppressed (NETROM-promoted) %s→%s",
                self._local, self._remote,
            )
            return
        self._closing = True
        logger.debug("agwpe TX [d] disconnect %s→%s", self._local, self._remote)
        try:
            frame = _build_frame(self._port, "d", self._local, self._remote)
            self._w.write(frame)
        except Exception:
            pass  # TCP connection already gone

    async def wait_closed(self) -> None:
        pass  # Virtual — AGWPE sends 'd' confirmation asynchronously

    def get_extra_info(self, key: str, default: Any = None) -> Any:
        return default


# ── _AGWPESession ─────────────────────────────────────────────────────────────

class _AGWPESession:
    """Holds the reader and writer for one connected station."""

    def __init__(
        self,
        remote_call: str,
        local_call: str,
        agw_port: int,
        agwpe_writer: asyncio.StreamWriter,
        drain_lock: asyncio.Lock,
        write_timeout: int = 30,
    ) -> None:
        self.remote_call = remote_call
        self.reader = asyncio.StreamReader()
        self.writer = _AGWPEVirtualWriter(
            agwpe_writer, local_call, remote_call, agw_port, drain_lock, write_timeout
        )
        # Populated when the session is a NETROM crosslink (either
        # classified as such at 'C' time by the router-lookup, or by the
        # cold-start late-PID-detection fallback).  Owns all per-user
        # NETROM circuits on this AX.25 link.
        self.netrom_manager: Optional[NetromCircuitManager] = None
        # Cached Connection used by the direct-BBS path; built in the 'C'
        # handler so the session task gets the same writer/reader objects.
        self.connection: Optional[Connection] = None

    def feed_data(self, data: bytes) -> None:
        if data:
            qlen = self.reader._buffer.__len__() if hasattr(self.reader, '_buffer') else -1
            logger.debug(
                "agwpe RX [D] %s  %d bytes (reader buffer was %d bytes): %r",
                self.remote_call, len(data), qlen, data[:80],
            )
            self.reader.feed_data(data)

    def feed_eof(self) -> None:
        logger.debug("agwpe feed_eof for %s", self.remote_call)
        try:
            self.reader.feed_eof()
        except Exception:
            pass


# ── Transport ─────────────────────────────────────────────────────────────────

# Session key: (agw_port, remote_callsign_upper)
_SessionKey = tuple[int, str]


class AGWPETransport(Transport):
    """
    Listens for incoming AX.25 connected calls via an AGW Packet Engine TCP
    interface.  One persistent TCP connection to AGWPE serves all sessions;
    reconnects automatically on failure.
    """

    transport_id = "agwpe"

    def __init__(self, cfg: dict[str, Any], bbs_callsign: str) -> None:
        call, ssid = parse(bbs_callsign)
        self._local_call = format_addr(call, ssid)
        # NET/ROM node identity (N3).  Outbound crosslinks (connect_out) and
        # NODES broadcasts originate from this callsign; it defaults to the BBS
        # callsign (today's behavior) and the engine overrides it with the node
        # SSID via set_netrom_node_call() when netrom.node_ssid is configured.
        self._netrom_node_call: str = self._local_call
        self._host: str = cfg.get("host", "127.0.0.1")
        self._port: int = int(cfg.get("port", 8000))
        self._agw_port: int = int(cfg.get("agw_port", 0))
        self._password: str = cfg.get("password", "")
        self._beacon_text: str = cfg.get("beacon_text", "").strip()
        self._beacon_dest: str = cfg.get("beacon_dest", "BEACON").strip().upper() or "BEACON"
        self._beacon_interval: int = max(1, int(cfg.get("beacon_interval", 20))) * 60
        raw_path = cfg.get("beacon_path", "")
        self._beacon_path: list[str] = [
            p.strip().upper() for p in raw_path.split(",") if p.strip()
        ]
        self._netrom_nodes_interval: int = 30 * 60  # overridden by set_netrom_nodes_interval()
        self._running = False
        self._sessions: dict[_SessionKey, _AGWPESession] = {}
        # Maps session key → running Task so they can be force-cancelled on TCP drop.
        self._session_tasks: dict[_SessionKey, "asyncio.Task[None]"] = {}
        self._on_connect: Optional[ConnectionCallback] = None
        self._registered: Optional[asyncio.Event] = None  # set when 'X' ack received
        # Direwolf's 'm' command TOGGLES monitoring, so it must be sent exactly
        # once per connection.  When multiple callsigns are registered (BBS +
        # service SSIDs) each 'X' ack would otherwise re-toggle it — an even
        # count silences the heard/display plugins.  Guarded by this flag.
        self._monitoring_on: bool = False
        # Serialises all drain() calls on the shared AGWPE TCP writer across
        # concurrent session tasks and the beacon loop (Python 3.9 fix).
        self._drain_lock: Optional[asyncio.Lock] = None
        # The live AGWPE TCP StreamWriter for the current connection, exposed
        # so out-of-read-loop callers (connect_out) can send frames.  Set after
        # open_connection() in start(); cleared to None on TCP drop.  None means
        # "not connected" — connect_out refuses rather than NPE.
        self._sock_writer: Optional[asyncio.StreamWriter] = None
        # Outbound NETROM crosslinks awaiting their Direwolf 'C' confirmation.
        # Keyed by (agw_port, neighbor_upper); the future resolves to the new
        # crosslink's NetromCircuitManager on 'C', or is failed on 'd'/RETRYOUT,
        # timeout, or TCP drop.  See connect_out() and the 'C'/'d' dispatch.
        self._pending_connects: dict[
            _SessionKey, "asyncio.Future[NetromCircuitManager]"
        ] = {}
        self._write_timeout: int = int(cfg.get("write_timeout", 30))
        # Optional path for logging raw 'U' monitor strings (useful for test-data capture).
        self._monitor_log: str = cfg.get("monitor_log", "").strip()
        # Maps callsign → hop count derived from monitored SABM/SABME frames;
        # consumed when the corresponding 'C' (connect) event arrives.
        self._pending_hop_counts: dict[str, int] = {}
        # NETROM crosslink classifier (Milestone 3 + router-lookup refactor).
        # Each incoming 'C' is classified synchronously by looking up the
        # caller in the router's adjacent-neighbor set: known neighbor →
        # NETROM crosslink (no BBS task started), unknown → direct BBS.
        # This is deterministic and avoids the race that the old timer-based
        # classifier had with slow NETROM stacks (KPC-3 / TheNet emit
        # CONNECT REQ up to ~10 seconds after AX.25 link-up, by which time
        # the BBS task had already queued banner frames into Direwolf's
        # outbound RF buffer that we could not pull back).  Late-PID
        # detection is kept as a cold-start fallback when the router has
        # no entries yet (see _dispatch 'D').
        self._netrom_crosslink_enabled: bool = False
        self._is_netrom_neighbor: Optional[Callable[[str], bool]] = None
        # Outbound NETROM L3 info-MTU.  Must be ≤ (AX.25 PACLEN − 20-byte
        # L3 header) of the local TNC: oversize L3 frames get split at L2
        # by Direwolf (or any AX.25 stack honoring PACLEN) and the
        # header-less second fragment is undecodable by the peer's NETROM
        # stack — visible as missing chunks in BBS output.  Default 108
        # matches NORCAL convention of PACLEN=128.
        self._netrom_info_mtu: int = 108
        # Idle-crosslink reaper timeout (seconds); 0 = keep links up forever.
        self._netrom_link_idle_timeout: float = 0.0

        # Additional callsign-SSIDs to register with AGWPE (beyond the BBS
        # callsign) so callers can reach ax25d-style external services on
        # those SSIDs.  Populated via set_extra_callsigns() before start().
        self._extra_callsigns: list[str] = []

    def set_netrom_nodes_interval(self, seconds: int) -> None:
        self._netrom_nodes_interval = max(60, seconds)

    def set_netrom_crosslink_enabled(self, enabled: bool = True) -> None:
        """Enable or disable inbound NETROM L3 crosslink acceptance.

        Called by the engine when the user has a ``netrom:`` block in
        bbs.yaml.  Per-connection classification (NETROM vs. direct BBS)
        is delegated to the callback registered via
        :meth:`set_netrom_neighbor_check`.
        """
        self._netrom_crosslink_enabled = enabled

    def set_netrom_neighbor_check(
        self, cb: Callable[[str], bool]
    ) -> None:
        """Register the synchronous predicate used on each incoming 'C'
        to decide whether the caller is a NETROM crosslink.

        Caller-side contract: *cb* should return True iff the callsign
        passed in is in our adjacent-neighbor set (= we've received at
        least one NODES broadcast from them since startup, including via
        ``seed_from_db``).  The router's ``adjacent_neighbors`` property
        is the canonical source; engine wires it through.
        """
        self._is_netrom_neighbor = cb

    def set_netrom_info_mtu(self, mtu: int) -> None:
        """Set the max NETROM L3 info-field payload (bytes) per fragment.

        Must be ≤ (TNC PACLEN − 20).  See class-level docstring on
        ``_netrom_info_mtu`` for the rationale.
        """
        self._netrom_info_mtu = max(1, int(mtu))

    def set_netrom_link_idle_timeout(self, seconds: float) -> None:
        """Disconnect a NETROM crosslink after this many seconds with no
        circuits (0 = keep it up indefinitely). See NetromCircuitManager."""
        self._netrom_link_idle_timeout = max(0.0, float(seconds))

    def set_netrom_node_call(self, call: str) -> None:
        """Source outbound NETROM crosslinks + NODES broadcasts from *call* (N3).

        Defaults to the BBS callsign; the engine sets the node SSID here when
        ``netrom.node_ssid`` is configured.  The call is also registered with
        AGWPE in :meth:`start` (when it differs from the BBS callsign) so
        inbound connects to the node identity reach us."""
        c = (call or "").upper().strip()
        self._netrom_node_call = c or self._local_call

    def set_extra_callsigns(self, calls: list[str]) -> None:
        """Register extra callsign-SSIDs to accept (ax25d-style services).

        Each is registered with AGWPE via its own 'X' frame in start() so
        Direwolf routes inbound connects for those SSIDs to us; the incoming
        'C' then carries the service SSID as ``call_to`` for the dispatcher.
        The BBS callsign itself is always registered and need not be listed.
        """
        seen = {self._local_call.upper(), self._netrom_node_call.upper()}
        out: list[str] = []
        for c in calls:
            cu = str(c).upper().strip()
            if cu and cu not in seen:
                seen.add(cu)
                out.append(cu)
        self._extra_callsigns = out

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, on_connect: ConnectionCallback) -> None:
        self._on_connect = on_connect
        self._running = True
        retry_delay = 5

        while self._running:
            writer: Optional[asyncio.StreamWriter] = None
            beacon_task: Optional[asyncio.Task[None]] = None
            nodes_task: Optional[asyncio.Task[None]] = None
            # Fresh lock per TCP connection so zombie tasks from a prior connection
            # do not contend with new-connection sessions on a different writer.
            self._drain_lock = asyncio.Lock()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=30,
                )
                logger.info(
                    "agwpe connected to %s:%d — registering %s on port %d",
                    self._host, self._port, self._local_call, self._agw_port,
                )
                retry_delay = 5  # reset back-off after a successful connect
                # Publish the writer so out-of-read-loop callers (connect_out)
                # can send frames on this connection.  Paired with the
                # fresh _drain_lock created above.
                self._sock_writer = writer

                # Enable TCP keepalives so the OS will probe and tear down the
                # connection if AGWPE or the host becomes unreachable silently
                # (no FIN/RST), without requiring application-level read timeouts
                # on the idle header read.
                _sock = writer.get_extra_info("socket")
                if _sock is not None:
                    _sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

                # Optional login (only needed when AGWPE has a password set)
                if self._password:
                    writer.write(
                        _build_frame(0, "P", "", "", data=self._password.encode("ascii"))
                    )
                    await asyncio.wait_for(writer.drain(), timeout=30)

                # Register our callsign so AGWPE routes incoming calls to us
                self._registered = asyncio.Event()
                self._monitoring_on = False   # re-enable once, on this connection
                writer.write(
                    _build_frame(self._agw_port, "X", self._local_call, "")
                )
                # Register the NET/ROM node SSID too (N3) when it differs from
                # the BBS callsign, so inbound connects to the node identity
                # route to us.  (set_extra_callsigns dedups it out of the list
                # below, so it is registered exactly once here.)
                if self._netrom_node_call.upper() != self._local_call.upper():
                    writer.write(
                        _build_frame(self._agw_port, "X", self._netrom_node_call, "")
                    )
                    logger.info(
                        "agwpe: registering NET/ROM node callsign %s on port %d",
                        self._netrom_node_call, self._agw_port,
                    )
                # Register any extra service SSIDs (ax25d-style hosting) so
                # Direwolf also routes connects for those to us.
                for _svc_call in self._extra_callsigns:
                    writer.write(
                        _build_frame(self._agw_port, "X", _svc_call, "")
                    )
                    logger.info("agwpe: registering service callsign %s on port %d",
                                _svc_call, self._agw_port)
                await asyncio.wait_for(writer.drain(), timeout=30)

                assert self._drain_lock is not None
                if self._beacon_text:
                    beacon_task = asyncio.create_task(
                        self._beacon_loop(writer, self._registered, self._drain_lock), name="agwpe:beacon"
                    )
                    logger.info(
                        "agwpe beacon enabled: every %d min to %s — %s",
                        self._beacon_interval // 60,
                        self._beacon_dest,
                        self._beacon_text,
                    )

                if self._netrom_nodes_builder is not None:
                    nodes_task = asyncio.create_task(
                        self._netrom_nodes_loop(writer, self._registered, self._drain_lock),
                        name="agwpe:netrom_nodes",
                    )
                    logger.info(
                        "agwpe NETROM NODES broadcast enabled: every %d min",
                        self._netrom_nodes_interval // 60,
                    )

                await self._read_loop(reader, writer)

            except asyncio.CancelledError:
                return
            except (asyncio.TimeoutError, ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                logger.warning(
                    "agwpe connection to %s:%d failed: %s — retry in %ds",
                    self._host, self._port, exc, retry_delay,
                )
            except Exception:
                logger.exception("agwpe unexpected error — reconnecting in %ds", retry_delay)
            finally:
                # No more out-of-loop sends on this writer, and any connect
                # awaiting a 'C' confirmation will never get one now — clear the
                # writer and fail every pending connect future so their callers
                # unblock instead of hanging until timeout.
                self._sock_writer = None
                self._fail_pending_connects(
                    ConnectionError("agwpe TCP connection lost before connect confirmed")
                )
                for _task in (beacon_task, nodes_task):
                    if _task:
                        _task.cancel()
                        try:
                            await _task
                        except (asyncio.CancelledError, Exception):
                            pass
                if writer and not writer.is_closing():
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                # Force-cancel live session tasks; they also receive feed_eof
                # below, but cancellation ensures hung tasks don't ghost.
                for stask in list(self._session_tasks.values()):
                    stask.cancel()
                self._session_tasks.clear()
                # Tear down all active sessions so the BBS sessions see EOF.
                # NETROM crosslinks also need to drop all their user circuits.
                for sess in list(self._sessions.values()):
                    if sess.netrom_manager is not None:
                        sess.netrom_manager.shutdown()
                        self._note_crosslink(sess.remote_call, False)
                    sess.feed_eof()
                self._sessions.clear()

            if self._running:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def stop(self) -> None:
        self._running = False
        logger.info("agwpe transport stopped")

    # ── Read loop (demultiplexer) ─────────────────────────────────────────────

    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read AGWPE frames and route them to the appropriate session."""
        frames_in = 0
        logger.debug("agwpe _read_loop started, active sessions: %d", len(self._sessions))
        while self._running:
            try:
                # No timeout here: during idle periods (no AX.25 activity) AGWPE
                # sends nothing, which is normal.  Dead connections are detected
                # by TCP keepalives (set on the socket after connect) and by the
                # payload-read timeout below.
                raw = await reader.readexactly(_HEADER_SIZE)
            except (asyncio.IncompleteReadError, ConnectionResetError, EOFError) as exc:
                logger.info("agwpe read loop: TCP read error after %d frames: %s", frames_in, exc)
                break
            except asyncio.CancelledError:
                logger.debug("agwpe read loop cancelled after %d frames", frames_in)
                raise  # propagate so start() exits rather than reconnecting

            (
                port, _, _, _,
                kind_byte, _, pid, _,
                call_from_raw, call_to_raw,
                data_len, _,
            ) = struct.unpack(_HEADER_FMT, raw)

            call_from = _decode_call(call_from_raw)
            call_to   = _decode_call(call_to_raw)
            kind      = chr(kind_byte)

            logger.debug(
                "agwpe frame #%d kind=%r port=%d from=%r to=%r data_len=%d sessions=%d",
                frames_in, kind, port, call_from, call_to, data_len, len(self._sessions),
            )
            frames_in += 1

            payload = b""
            if data_len > 0:
                try:
                    payload = await asyncio.wait_for(
                        reader.readexactly(data_len), timeout=_TCP_READ_TIMEOUT
                    )
                except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
                    logger.info("agwpe read loop: payload read error: %s", exc)
                    break
                except asyncio.TimeoutError:
                    logger.info(
                        "agwpe read loop: payload read timeout (%ds) — reconnecting",
                        _TCP_READ_TIMEOUT,
                    )
                    break

            # Isolate per-frame failures: a malformed frame or a bug in one
            # session's handling must not tear down the whole read loop (which
            # would drop every connected user and bounce the TCP link).  Log
            # and move on; only cancellation propagates.
            try:
                await self._dispatch(kind, port, call_from, call_to, pid, payload, writer)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "agwpe: error dispatching %r frame from %s→%s (port %d) — "
                    "dropping frame, transport stays up",
                    kind, call_from, call_to, port,
                )

        logger.info(
            "agwpe read loop ended after %d frames — TCP connection closed, %d sessions active",
            frames_in, len(self._sessions),
        )

    # ── Frame dispatcher ──────────────────────────────────────────────────────

    async def _dispatch(
        self,
        kind: str,
        port: int,
        call_from: str,
        call_to: str,
        pid: int,
        payload: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        key: _SessionKey = (port, call_from.upper())

        if kind == "X":
            # Registration acknowledgement
            ok = bool(payload and payload[0] == 1)
            if ok:
                logger.info(
                    "agwpe: callsign %s registered on port %d", self._local_call, port
                )
                if self._registered is not None:
                    self._registered.set()
                # Enable frame monitoring so 'U' (UI) frames arrive with
                # source, dest, and via path already parsed in TNC2 format.
                # Direwolf's 'm' TOGGLES monitoring, so send it ONLY on the
                # first 'X' ack — extra service-SSID registrations each ack
                # too, and re-sending 'm' would toggle monitoring back off.
                if not self._monitoring_on:
                    self._monitoring_on = True
                    writer.write(_build_frame(self._agw_port, "m", "", ""))
                    assert self._drain_lock is not None
                    async with self._drain_lock:
                        await writer.drain()
                    logger.info("agwpe: monitoring enabled for hop-count tracking and heard-station logging")
            else:
                logger.warning(
                    "agwpe: callsign registration FAILED for %s on port %d",
                    self._local_call, port,
                )

        elif kind == "C":
            # Outbound-connect confirmation?  Direwolf's 'C' confirmation carries
            # call_from=remote, call_to=us for BOTH inbound and outbound links
            # (server_link_established), so address order can't discriminate.
            # The pending-connect table is the discriminator; the "CONNECTED
            # With" payload string (server.c:1137) merely corroborates.  This
            # check runs BEFORE the inbound duplicate-'C' teardown below, so an
            # outbound confirmation never trips it.
            pending = self._pending_connects.get(key)
            if pending is not None:
                self._pending_connects.pop(key, None)
                if payload:
                    try:
                        _text = payload.decode("ascii", errors="replace")
                        if "CONNECTED With" not in _text:
                            logger.warning(
                                "agwpe: outbound 'C' for %s but payload lacks "
                                "'CONNECTED With' (%r) — proceeding on pending match",
                                call_from, _text[:60],
                            )
                    except Exception:
                        pass
                assert self._drain_lock is not None
                assert self._on_connect is not None
                # Build the crosslink session sourced from OUR node call to the
                # neighbor (call_from).  A NET/ROM node's crosslinks originate
                # from the node identity (N3: the node SSID when configured,
                # else the BBS callsign) — this feeds both the AGWPE 'D' frame
                # CallFrom and the L3 origin of circuits originated on the link.
                # No BBS session task is started — this link originates circuits;
                # any inbound circuit arriving on it is handled by the manager's
                # on_user_connect.
                sess = _AGWPESession(
                    call_from, self._netrom_node_call, port, writer,
                    self._drain_lock, self._write_timeout,
                )
                sess.netrom_manager = NetromCircuitManager(
                    local_call        = self._netrom_node_call,
                    via_node          = sess.remote_call,
                    ax25_writer       = self._make_netrom_writer(sess),
                    on_user_connect   = self._on_connect,
                    info_mtu          = self._netrom_info_mtu,
                    link_idle_timeout = self._netrom_link_idle_timeout,
                )
                self._sessions[key] = sess
                self._note_crosslink(call_from, True)
                logger.info(
                    "agwpe: outbound NETROM crosslink to %s established", call_from,
                )
                if not pending.done():
                    pending.set_result(sess.netrom_manager)
                return

            # Incoming connected call — create a new session.
            # Per AX.25 spec, when a TNC receives a SABM while already connected it
            # sends UA back and resets its sequence counters — i.e. it treats the
            # SABM as a reconnect.  Direwolf does this correctly, so a duplicate 'C'
            # here means the remote TNC has already reset its AX.25 state.  We must
            # mirror that: tear down the stale BBS session and start a fresh one.
            # Silently ignoring it (the old behaviour) left the two sides with
            # mismatched sequence numbers, causing the connection to go idle.
            if key in self._sessions:
                logger.warning(
                    "agwpe: duplicate 'C' for %s (key=%s) — AX.25 reconnect detected; "
                    "tearing down old session and starting fresh; existing sessions: %s",
                    call_from, key, list(self._sessions.keys()),
                )
                old_sess = self._sessions.pop(key, None)
                if old_sess:
                    # If the old session was a NETROM crosslink, shut down
                    # every NETROM circuit riding on it (otherwise their BBS
                    # session tasks would be orphaned and the user circuits
                    # would leak).
                    if old_sess.netrom_manager is not None:
                        old_sess.netrom_manager.shutdown()
                        self._note_crosslink(call_from, False)
                    old_sess.feed_eof()
                old_task = self._session_tasks.pop(key, None)
                if old_task and not old_task.done():
                    old_task.cancel()
            logger.info(
                "agwpe: incoming connection from %s; total sessions will be %d",
                call_from, len(self._sessions) + 1,
            )
            assert self._drain_lock is not None
            # Source outbound frames from the callsign the caller actually
            # dialed (``call_to``), NOT the fixed BBS callsign.  For a service
            # SSID (ax25d-style hosting) the connected-mode stream at the TNC is
            # keyed on that SSID; sending replies from the BBS callsign instead
            # leaves Direwolf unable to match them to the stream and it silently
            # drops all output.  For a normal BBS connect, call_to == the BBS
            # callsign, so this is a no-op there.
            sess = _AGWPESession(
                call_from, call_to, port, writer,
                self._drain_lock, self._write_timeout,
            )
            self._sessions[key] = sess
            conn = Connection(
                remote_addr=call_from,
                reader=sess.reader,
                writer=sess.writer,       # type: ignore[arg-type]
                transport_id=self.transport_id,
                hop_count=self._pending_hop_counts.pop(call_from.upper(), 0),
                local_addr=call_to,       # which of our SSIDs the caller dialed
            )
            sess.connection = conn
            assert self._on_connect is not None
            # Classify on 'C' using the router's adjacent-neighbor set —
            # synchronous, deterministic, no timer race.  A known NETROM
            # neighbor → instantiate a circuit manager and DO NOT start
            # the BBS task; the first 'D' frame will be a NETROM L3
            # CONNECT REQ that the manager handles, and the per-user BBS
            # session is spun up there.  An unknown caller → regular BBS
            # session immediately, banner goes out at AX.25 UA time.
            is_netrom = (
                self._netrom_crosslink_enabled
                and self._is_netrom_neighbor is not None
                and self._is_netrom_neighbor(call_from)
            )
            if is_netrom:
                netrom_writer = self._make_netrom_writer(sess)
                sess.netrom_manager = NetromCircuitManager(
                    local_call      = self._local_call,
                    via_node        = sess.remote_call,
                    ax25_writer     = netrom_writer,
                    on_user_connect = self._on_connect,
                    info_mtu        = self._netrom_info_mtu,
                    link_idle_timeout = self._netrom_link_idle_timeout,
                )
                self._note_crosslink(call_from, True)
                logger.info(
                    "agwpe: NETROM crosslink with %s — known neighbor, "
                    "no BBS task started, waiting for L3 CONNECT REQ",
                    call_from,
                )
                # No session task to register — the per-user BBS sessions
                # are created on demand by the circuit manager.
            else:
                task = asyncio.create_task(
                    self._run_session(key, sess, conn),
                    name=f"agwpe:session:{call_from}",
                )
                self._session_tasks[key] = task

        elif kind == "D":
            # Data for an active connected session.  Three branches:
            #   (a) NETROM crosslink (decided at 'C' time by router lookup,
            #       or by late-PID promotion below) → decode L3 + dispatch
            #   (b) cold-start fallback: PID=0xCF on a session that was
            #       classified as direct because the caller wasn't in our
            #       neighbor set on 'C' → promote and dispatch.
            #   (c) direct BBS user → feed to reader
            sess = self._sessions.get(key)
            if not sess:
                logger.warning(
                    "agwpe: 'D' frame for unknown session %s (key=%s) — dropped; known sessions: %s",
                    call_from, key, list(self._sessions.keys()),
                )
            elif not payload:
                pass  # empty 'D' is a no-op
            elif sess.netrom_manager is not None:
                frame = decode_l3_frame(payload)
                if frame is None:
                    logger.warning(
                        "agwpe: undecodable NETROM L3 frame on crosslink %s (%d bytes)",
                        call_from, len(payload),
                    )
                else:
                    await sess.netrom_manager.dispatch(frame)
            elif pid == PID_NETROM:
                # Cold-start fallback (router didn't know this neighbor yet).
                await self._promote_to_netrom_crosslink(key, sess)
                assert sess.netrom_manager is not None
                frame = decode_l3_frame(payload)
                if frame is None:
                    logger.warning(
                        "agwpe: late NETROM detection on %s but L3 decode "
                        "failed (%d bytes)", call_from, len(payload),
                    )
                else:
                    await sess.netrom_manager.dispatch(frame)
            else:
                sess.feed_data(payload)

        elif kind == "d":
            # Outbound connect failure?  A 'd' while a connect to this neighbor
            # is pending means the link never came up (RETRYOUT timeout, or a DM
            # refusal).  Reject the pending future; there is no session to tear
            # down because the 'C' confirmation that would have created one never
            # arrived.
            pending = self._pending_connects.pop(key, None)
            if pending is not None:
                reason = ""
                if payload:
                    try:
                        reason = payload.decode("ascii", errors="replace").strip()
                    except Exception:
                        reason = ""
                logger.info(
                    "agwpe: outbound connect to %s failed/terminated: %s",
                    call_from, reason or "(link terminated)",
                )
                if not pending.done():
                    pending.set_exception(ConnectionError(
                        f"connect to {call_from} failed: "
                        f"{reason or 'link terminated'}"
                    ))
                return

            # Remote station disconnected
            sess = self._sessions.pop(key, None)
            if sess:
                logger.info(
                    "agwpe: %s disconnected; remaining sessions: %d %s",
                    call_from, len(self._sessions), list(self._sessions.keys()),
                )
                if sess.netrom_manager is not None:
                    # Tear down all NETROM circuits riding on this crosslink.
                    sess.netrom_manager.shutdown()
                    self._note_crosslink(call_from, False)
                sess.feed_eof()
            else:
                logger.warning(
                    "agwpe: 'd' for unknown session %s (key=%s); known sessions: %s",
                    call_from, key, list(self._sessions.keys()),
                )

        elif kind in ("U", "S"):
            # Monitored frame. Direwolf splits these by frame class:
            #   'U' — UI / unproto frames
            #   'S' — supervisory + all non-UI U-frames (SABM, SABME, UA, DM, …)
            # SABM/SABME for an incoming connection arrives under 'S', so the
            # hop-count cache MUST observe both kinds to populate before 'C'.
            # 1. NETROM UI frames — extract binary payload and dispatch to the
            #    NETROM observer before any text decoding.  Use both PID and
            #    destination as discriminators: some AGWPE implementations
            #    (including Direwolf) report PID=0x00 in the header for all
            #    monitored frames, so PID alone is not reliable.
            if (kind == "U" and self._netrom_observer is not None
                    and (pid == PID_NETROM or call_to.upper() == "NODES")):
                binary_info = _extract_binary_info(payload) if payload else None
                if binary_info is not None:
                    try:
                        await self._netrom_observer(call_from, call_to, binary_info)
                    except Exception:
                        logger.exception("netrom observer error for frame from %s", call_from)
                return  # NETROM frames are not heard-station traffic
            # 2. Cache the via-path length when a SABM/SABME is directed at us.
            if payload and call_to.upper() == self._local_call.upper():
                try:
                    _sabm_text = payload.decode("ascii", errors="replace")
                    if _MONITOR_SABM_RE.search(_sabm_text):
                        _via = _parse_via(_sabm_text)
                        if len(self._pending_hop_counts) < 200:  # bound cache size
                            self._pending_hop_counts[call_from.upper()] = len(_via)
                except Exception:
                    pass
            # 3. Heard-station tracking is UI-frame only — that's the scope of
            #    "stations heard on the air". Skip for 'S' kind.
            if kind != "U":
                return
            if self._heard_observer is None:
                return
            if call_from.upper() == self._local_call.upper():
                return  # our own transmitted frame echoed back
            via: list[str] = []
            info = ""
            if payload:
                try:
                    text = payload.decode("ascii", errors="replace")
                    if self._monitor_log:
                        _append_monitor_log(self._monitor_log, text)
                    via  = _parse_via(text)
                    info = _parse_info(text)
                except Exception:
                    pass
            await self._heard_observer(
                call_from,
                call_to,
                via,
                int(time.time()),
                self.transport_id,
                info,
            )

        # All other frame types (version info, port info, etc.) are
        # silently ignored — the BBS has no use for them.

    # ── Session runner ────────────────────────────────────────────────────────

    async def _run_session(
        self, key: _SessionKey, sess: _AGWPESession, conn: Connection
    ) -> None:
        assert self._on_connect is not None
        t_start = time.monotonic()
        logger.debug("agwpe: _run_session started for %s", conn.remote_addr)
        try:
            await self._on_connect(conn)
        except Exception:
            logger.exception("agwpe: error in session %s", conn.remote_addr)
        finally:
            elapsed = time.monotonic() - t_start
            # Guard: only remove OUR entry; a fast reconnect from the same station
            # may have already registered a new session/task under this key.
            # Also guard against the late-NETROM promotion case — if we got
            # cancelled because the session was promoted to a NETROM crosslink,
            # the session needs to stay registered for the manager to receive
            # subsequent 'D' / 'd' frames on it.
            was_in_sessions = (
                self._sessions.get(key) is sess
                and sess.netrom_manager is None
            )
            if was_in_sessions:
                self._sessions.pop(key, None)
            cur_task = asyncio.current_task()
            if self._session_tasks.get(key) is cur_task:
                self._session_tasks.pop(key, None)
            logger.info(
                "agwpe: session %s ended after %.1fs; session was%s in table; remaining: %d %s",
                conn.remote_addr,
                elapsed,
                "" if was_in_sessions else " NOT",
                len(self._sessions),
                list(self._sessions.keys()),
            )

    # ── NETROM crosslink classifier (Milestone 3) ─────────────────────────────

    def _note_crosslink(self, call: str, up: bool) -> None:
        """Tell the adjacency observer (router.note_crosslink) that a NETROM
        crosslink to *call* went up/down.  A live crosslink is proof of one-hop
        adjacency.  No-op when unwired."""
        if self._netrom_crosslink_observer is not None:
            try:
                self._netrom_crosslink_observer(call.upper(), up)
            except Exception:
                logger.debug(
                    "agwpe: netrom crosslink observer failed for %s up=%s",
                    call, up, exc_info=True,
                )

    def _make_netrom_writer(
        self, sess: _AGWPESession
    ) -> _AGWPEVirtualWriter:
        """Build a fresh _AGWPEVirtualWriter for a NETROM circuit manager.

        Wraps the same underlying AGWPE TCP socket as ``sess.writer`` but
        has its own state (PID, close-suppression, mute flag).  The
        NETROM circuit uses this writer; the old ``sess.writer`` stays
        in place for the cancelled BBS task but is muted so its writes
        no longer reach the wire.
        """
        old = sess.writer
        assert self._drain_lock is not None
        new = _AGWPEVirtualWriter(
            agwpe_writer  = old._w,
            local_call    = old._local,
            remote_call   = old._remote,
            agw_port      = old._port,
            drain_lock    = self._drain_lock,
            write_timeout = self._write_timeout,
        )
        new.set_pid(PID_NETROM)
        return new

    async def _promote_to_netrom_crosslink(
        self, key: _SessionKey, sess: _AGWPESession
    ) -> None:
        """Cold-start fallback: a 'D' frame with PID=0xCF arrived on a
        session we treated as a direct BBS user because the caller wasn't
        in our adjacent-neighbor set at 'C' time.

        This fires only when the router was empty / hadn't yet learned of
        this neighbor (e.g. fresh deployment with no DB seed, or the
        neighbor's first-ever NODES broadcast hasn't reached us yet).
        Once the router fills (typically within one broadcast cycle of
        a normal startup), every subsequent NETROM 'C' from this neighbor
        is classified correctly on first contact.

        The cost: any banner the BBS already emitted has gone on the air
        with PID=0xF0 — a strict peer will drop them, a lenient peer
        forwards them as text.  Best-effort cleanup; the proper fix is to
        get the neighbor into the router before they connect, which is
        what ``seed_from_db`` does at startup.
        """
        logger.warning(
            "agwpe: late NETROM crosslink detection on session with %s — "
            "caller wasn't in router's neighbor set on 'C'; tearing down "
            "direct BBS path. (Check that NODES from %s reach this station; "
            "seed_from_db should normally cover this case at startup.)",
            sess.remote_call, sess.remote_call,
        )
        # Three things we do to the OLD writer (the one the cancelled BBS
        # session still holds a reference to via conn.writer):
        #   1. mute() — its write() calls become no-ops, so the cancelled
        #      BBS task's "73 de … -- disconnecting --" tail does NOT go on
        #      the wire as raw bytes with PID=0xCF and risk being forwarded
        #      to the user by a lenient peer NETROM stack.
        #   2. suppress_close() — its close() becomes a no-op, so the
        #      cancelled task's conn.close() cascade doesn't send an AGWPE
        #      'd' frame and tear down the AX.25 link the NETROM crosslink
        #      still needs.
        # The NEW writer (built below) is what the NETROM circuit manager
        # actually uses; it has its own state and PID=0xCF.
        if hasattr(sess.writer, "mute"):
            sess.writer.mute()
        if hasattr(sess.writer, "suppress_close"):
            sess.writer.suppress_close()
        # Cancel the BBS session task that's been writing the banner.
        old_task = self._session_tasks.pop(key, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()
        # Push EOF onto the reader so the BBS task exits its read loop.
        try:
            sess.reader.feed_eof()
        except Exception:
            pass
        # Replace the reader with a fresh one — NETROM circuits get their
        # own per-user readers via NetromCircuit; this one is defensive.
        sess.reader = asyncio.StreamReader()
        # Promote: install a circuit manager with its OWN writer.
        assert self._on_connect is not None
        netrom_writer = self._make_netrom_writer(sess)
        sess.netrom_manager = NetromCircuitManager(
            local_call      = self._local_call,
            via_node        = sess.remote_call,
            ax25_writer     = netrom_writer,
            on_user_connect = self._on_connect,
            info_mtu        = self._netrom_info_mtu,
            link_idle_timeout = self._netrom_link_idle_timeout,
        )
        self._note_crosslink(sess.remote_call, True)
        sess.pending_classification = False

    # ── Outbound NETROM crosslink origination (N1) ─────────────────────────────

    async def connect_netrom(
        self, neighbor: str
    ) -> Optional[NetromCircuitManager]:
        """Transport-interface override: originate (or reuse) a NETROM
        crosslink to *neighbor*.  Thin wrapper over :meth:`connect_out`
        with the default timeout so the engine/node layer can call it
        polymorphically."""
        return await self.connect_out(neighbor)

    async def connect_out(
        self, neighbor: str, *, timeout: float = 30.0
    ) -> NetromCircuitManager:
        """Originate an AX.25 crosslink to NETROM *neighbor* and return the
        :class:`NetromCircuitManager` that owns it.

        We *originate* the AX.25 connect (AGWPE 'C' frame from our registered
        local callsign to *neighbor*).  When Direwolf confirms the link with
        its own 'C' frame, the dispatch path builds the crosslink session +
        manager and resolves the future this coroutine awaits.

        Behaviour:
          - **Reuse**: if a crosslink to *neighbor* already exists (inbound or
            outbound) with a manager, return that manager immediately — no new
            connect.
          - **Coalesce**: concurrent calls to the same neighbor share one
            in-flight connect / one future.
          - **Timeout / failure**: raises ``asyncio.TimeoutError`` if no 'C'
            confirmation arrives within *timeout*; ``ConnectionError`` on a
            'd'/RETRYOUT rejection or if the AGWPE TCP link drops meanwhile.
          - Raises ``ConnectionError`` if we are not currently connected to
            AGWPE.

        The returned manager is *connected at the AX.25 layer only*; the caller
        (N2) then does ``await mgr.originate_circuit(dest_node, user)`` to open
        a NET/ROM circuit over it.  Idle teardown is handled by the reaper.
        """
        neighbor_u = neighbor.upper()
        key: _SessionKey = (self._agw_port, neighbor_u)

        # Reuse an existing crosslink (inbound or outbound) to this neighbor.
        sess = self._sessions.get(key)
        if sess is not None and sess.netrom_manager is not None:
            logger.info(
                "agwpe: reusing existing NETROM crosslink to %s for connect_out",
                neighbor_u,
            )
            return sess.netrom_manager

        # Coalesce onto an in-flight connect to the same neighbor.
        existing = self._pending_connects.get(key)
        if existing is not None:
            logger.debug(
                "agwpe: connect_out to %s coalesced onto in-flight connect",
                neighbor_u,
            )
            return await existing

        if self._sock_writer is None or self._drain_lock is None:
            raise ConnectionError(
                "agwpe: not connected to AGWPE — cannot originate crosslink "
                f"to {neighbor_u}"
            )

        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[NetromCircuitManager]" = loop.create_future()
        self._pending_connects[key] = fut

        # Send the connect: AGWPE 'C', call_from = our NET/ROM node call (the
        # node SSID when configured, else the BBS callsign — N3), call_to =
        # neighbor, on our AGW port.  NET/ROM rides as separate 'D' frames at
        # PID=0xCF later; the connect itself is fine at PID=0xF0.
        frame = _build_frame(self._agw_port, "C", self._netrom_node_call, neighbor_u)
        try:
            self._sock_writer.write(frame)
            async with self._drain_lock:
                await self._sock_writer.drain()
        except Exception as exc:
            self._pending_connects.pop(key, None)
            if not fut.done():
                fut.cancel()
            raise ConnectionError(
                f"agwpe: failed to send connect to {neighbor_u}: {exc}"
            ) from exc

        logger.info(
            "agwpe: originating NETROM crosslink %s → %s (timeout %.0fs)",
            self._netrom_node_call, neighbor_u, timeout,
        )
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending_connects.pop(key, None)
            logger.warning(
                "agwpe: connect_out to %s timed out after %.0fs (no 'C' "
                "confirmation)", neighbor_u, timeout,
            )
            raise

    def _fail_pending_connects(self, exc: Exception) -> None:
        """Fail every pending connect future with *exc* and clear the table.

        Called on TCP drop (in ``start()``'s finally): a connect awaiting a 'C'
        confirmation on the dropped link will never get one, so unblock its
        caller with an error instead of leaving it to time out.
        """
        for fut in self._pending_connects.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending_connects.clear()

    # ── Beacon ────────────────────────────────────────────────────────────────

    async def _beacon_loop(
        self, writer: asyncio.StreamWriter, registered: asyncio.Event,
        drain_lock: asyncio.Lock,
    ) -> None:
        """Send an unproto beacon every beacon_interval seconds."""
        # Wait for the 'X' registration ack before sending; AGWPE silently drops
        # 'M'/'V' frames from an unregistered callsign.
        try:
            await asyncio.wait_for(registered.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("agwpe: registration not confirmed after 30 s; sending beacon anyway")
        except asyncio.CancelledError:
            return
        try:
            while self._running:
                if self._sessions:
                    # Don't beacon while a user is connected — save air time.
                    await asyncio.sleep(self._beacon_interval)
                    continue
                try:
                    payload = self._beacon_text.encode("ascii", errors="replace")
                    if self._beacon_path:
                        frame = _build_unproto_via_frame(
                            self._agw_port,
                            self._local_call,
                            self._beacon_dest,
                            self._beacon_path,
                            payload,
                        )
                    else:
                        frame = _build_frame(
                            self._agw_port, "M",
                            self._local_call, self._beacon_dest,
                            _PID_NO_L3, payload,
                        )
                    writer.write(frame)
                    async with drain_lock:
                        await writer.drain()
                    logger.info(
                        "agwpe beacon sent to %s%s",
                        self._beacon_dest,
                        " via " + ",".join(self._beacon_path) if self._beacon_path else "",
                    )
                except Exception:
                    logger.warning("agwpe beacon send failed", exc_info=True)
                await asyncio.sleep(self._beacon_interval)
        except asyncio.CancelledError:
            pass

    # ── NETROM NODES broadcast ────────────────────────────────────────────────

    async def _netrom_nodes_loop(
        self, writer: asyncio.StreamWriter, registered: asyncio.Event,
        drain_lock: asyncio.Lock,
    ) -> None:
        """Broadcast our NETROM NODES routing table every netrom_nodes_interval seconds."""
        try:
            await asyncio.wait_for(registered.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("agwpe: registration not confirmed after 30 s; sending NODES anyway")
        except asyncio.CancelledError:
            return
        try:
            while self._running:
                assert self._netrom_nodes_builder is not None
                payload = self._netrom_nodes_builder()
                if payload:
                    try:
                        frame = _build_frame(
                            self._agw_port, "M",
                            self._netrom_node_call, "NODES",
                            PID_NETROM, payload,
                        )
                        writer.write(frame)
                        async with drain_lock:
                            await writer.drain()
                        logger.info(
                            "agwpe NETROM NODES broadcast sent: %d bytes", len(payload)
                        )
                    except Exception:
                        logger.warning("agwpe NODES broadcast send failed", exc_info=True)
                await asyncio.sleep(self._netrom_nodes_interval)
        except asyncio.CancelledError:
            pass
