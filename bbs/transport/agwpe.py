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
  'T' (0x54) — Send unproto (UI) frame — used for periodic beacons
  'm' (0x6D) — Enable monitoring of all received frames
  'U' (0x55) — Monitored UI frame (AGWPE monitor format, e.g.:
               "1:Fm W6ELA-1 To BEACON Via KROCK*,KJOHN* <UI pid=F0 ...>")

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
import struct
import time
from typing import Any, Optional

# Set this logger to DEBUG in your logging config (or via bbs.yaml) to get
# per-frame traces.  At INFO level only connection events are emitted.
#
# Quick enable at runtime:
#   logging.getLogger('bbs.transport.agwpe').setLevel(logging.DEBUG)

import re

from bbs.ax25.address import format_addr, parse
from bbs.transport.base import Connection, ConnectionCallback, Transport

logger = logging.getLogger(__name__)

# ── Frame format ──────────────────────────────────────────────────────────────
_HEADER_FMT  = "<BBBBBBBB10s10sii"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)   # 36 bytes

assert _HEADER_SIZE == 36, "AGWPE header must be 36 bytes"

_PID_NO_L3 = 0xF0  # no layer-3 protocol

# AGWPE 'U' monitor string format:
#   "1:Fm W6ELA-1 To BEACON Via KROCK*,KJOHN*,KBERR <UI pid=F0 ...>"
# Via section is absent for direct (non-digipeated) frames.
_MONITOR_VIA_RE = re.compile(r"\bVia\s+([^<\r\n]+?)\s*<", re.IGNORECASE)


def _parse_via(monitor_text: str) -> list[str]:
    """Extract digipeater path from an AGWPE 'U' monitor string."""
    m = _MONITOR_VIA_RE.search(monitor_text)
    if not m:
        return []
    return [v.strip() for v in m.group(1).split(",") if v.strip()]


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
      N × 10 bytes : via callsigns, each null-padded to 10 bytes (same as header fields)
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
    ) -> None:
        self._w = agwpe_writer
        self._local = local_call
        self._remote = remote_call
        self._port = agw_port
        self._closing = False
        self._drain_lock = drain_lock

    def write(self, data: bytes) -> None:
        if data and not self._closing:
            frame = _build_frame(self._port, "D", self._local, self._remote, _PID_NO_L3, data)
            self._w.write(frame)
            logger.debug(
                "agwpe TX [D] %s→%s  %d bytes: %r",
                self._local, self._remote, len(data), data[:80],
            )

    async def drain(self) -> None:
        if not self._closing:
            async with self._drain_lock:
                await self._w.drain()

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if not self._closing:
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
    ) -> None:
        self.remote_call = remote_call
        self.reader = asyncio.StreamReader()
        self.writer = _AGWPEVirtualWriter(agwpe_writer, local_call, remote_call, agw_port, drain_lock)

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
        self._running = False
        self._sessions: dict[_SessionKey, _AGWPESession] = {}
        self._on_connect: Optional[ConnectionCallback] = None
        self._registered: Optional[asyncio.Event] = None  # set when 'X' ack received
        # Serialises all drain() calls on the shared AGWPE TCP writer across
        # concurrent session tasks and the beacon loop (Python 3.9 fix).
        self._drain_lock: Optional[asyncio.Lock] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, on_connect: ConnectionCallback) -> None:
        self._on_connect = on_connect
        self._running = True
        retry_delay = 5
        # Create the lock here so it binds to the running event loop.
        self._drain_lock = asyncio.Lock()

        while self._running:
            writer: Optional[asyncio.StreamWriter] = None
            beacon_task: Optional[asyncio.Task[None]] = None
            try:
                reader, writer = await asyncio.open_connection(self._host, self._port)
                logger.info(
                    "agwpe connected to %s:%d — registering %s on port %d",
                    self._host, self._port, self._local_call, self._agw_port,
                )
                retry_delay = 5  # reset back-off after a successful connect

                # Optional login (only needed when AGWPE has a password set)
                if self._password:
                    writer.write(
                        _build_frame(0, "P", "", "", data=self._password.encode("ascii"))
                    )
                    await writer.drain()

                # Register our callsign so AGWPE routes incoming calls to us
                self._registered = asyncio.Event()
                writer.write(
                    _build_frame(self._agw_port, "X", self._local_call, "")
                )
                await writer.drain()

                if self._beacon_text:
                    assert self._drain_lock is not None
                    beacon_task = asyncio.create_task(
                        self._beacon_loop(writer, self._registered, self._drain_lock), name="agwpe:beacon"
                    )
                    logger.info(
                        "agwpe beacon enabled: every %d min to %s — %s",
                        self._beacon_interval // 60,
                        self._beacon_dest,
                        self._beacon_text,
                    )

                await self._read_loop(reader, writer)

            except asyncio.CancelledError:
                return
            except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                logger.warning(
                    "agwpe connection to %s:%d failed: %s — retry in %ds",
                    self._host, self._port, exc, retry_delay,
                )
            except Exception:
                logger.exception("agwpe unexpected error — reconnecting in %ds", retry_delay)
            finally:
                if beacon_task:
                    beacon_task.cancel()
                if writer and not writer.is_closing():
                    writer.close()
                # Tear down all active sessions so the BBS sessions see EOF
                for sess in list(self._sessions.values()):
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
                    payload = await reader.readexactly(data_len)
                except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
                    logger.info("agwpe read loop: payload read error: %s", exc)
                    break

            await self._dispatch(kind, port, call_from, call_to, pid, payload, writer)

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
                if self._heard_observer is not None:
                    writer.write(_build_frame(self._agw_port, "m", "", ""))
                    assert self._drain_lock is not None
                    async with self._drain_lock:
                        await writer.drain()
                    logger.info("agwpe: monitoring enabled for heard-station tracking")
            else:
                logger.warning(
                    "agwpe: callsign registration FAILED for %s on port %d",
                    self._local_call, port,
                )

        elif kind == "C":
            # Incoming connected call — create a new session
            if key in self._sessions:
                logger.warning(
                    "agwpe: duplicate 'C' for %s (key=%s) — ignoring; existing sessions: %s",
                    call_from, key, list(self._sessions.keys()),
                )
                return
            logger.info(
                "agwpe: incoming connection from %s; total sessions will be %d",
                call_from, len(self._sessions) + 1,
            )
            assert self._drain_lock is not None
            sess = _AGWPESession(call_from, self._local_call, port, writer, self._drain_lock)
            self._sessions[key] = sess
            conn = Connection(
                remote_addr=call_from,
                reader=sess.reader,
                writer=sess.writer,       # type: ignore[arg-type]
                transport_id=self.transport_id,
            )
            assert self._on_connect is not None
            asyncio.create_task(
                self._run_session(key, conn), name=f"agwpe:session:{call_from}"
            )

        elif kind == "D":
            # Data for an active connected session
            sess = self._sessions.get(key)
            if sess and payload:
                sess.feed_data(payload)
            elif not sess:
                logger.warning(
                    "agwpe: 'D' frame for unknown session %s (key=%s) — dropped; known sessions: %s",
                    call_from, key, list(self._sessions.keys()),
                )

        elif kind == "d":
            # Remote station disconnected
            sess = self._sessions.pop(key, None)
            if sess:
                logger.info(
                    "agwpe: %s disconnected; remaining sessions: %d %s",
                    call_from, len(self._sessions), list(self._sessions.keys()),
                )
                sess.feed_eof()
            else:
                logger.warning(
                    "agwpe: 'd' for unknown session %s (key=%s); known sessions: %s",
                    call_from, key, list(self._sessions.keys()),
                )

        elif kind == "U":
            # Monitored UI frame — AGWPE already parsed src/dest into the header;
            # extract the via path from the TNC2 monitor string in the payload.
            if self._heard_observer is None:
                return
            if call_from.upper() == self._local_call.upper():
                return  # our own transmitted frame echoed back
            via: list[str] = []
            if payload:
                try:
                    via = _parse_via(payload.decode("ascii", errors="replace"))
                except Exception:
                    pass
            await self._heard_observer(
                call_from,
                call_to,
                via,
                int(time.time()),
                self.transport_id,
            )

        # All other frame types (version info, port info, etc.) are
        # silently ignored — the BBS has no use for them.

    # ── Session runner ────────────────────────────────────────────────────────

    async def _run_session(self, key: _SessionKey, conn: Connection) -> None:
        assert self._on_connect is not None
        t_start = time.monotonic()
        logger.debug("agwpe: _run_session started for %s", conn.remote_addr)
        try:
            await self._on_connect(conn)
        except Exception:
            logger.exception("agwpe: error in session %s", conn.remote_addr)
        finally:
            elapsed = time.monotonic() - t_start
            was_in_sessions = key in self._sessions
            self._sessions.pop(key, None)
            logger.info(
                "agwpe: session %s ended after %.1fs; session was%s in table; remaining: %d %s",
                conn.remote_addr,
                elapsed,
                "" if was_in_sessions else " NOT",
                len(self._sessions),
                list(self._sessions.keys()),
            )

    # ── Beacon ────────────────────────────────────────────────────────────────

    async def _beacon_loop(
        self, writer: asyncio.StreamWriter, registered: asyncio.Event,
        drain_lock: asyncio.Lock,
    ) -> None:
        """Send an unproto beacon every beacon_interval seconds."""
        # Wait for the 'X' registration ack before sending; AGWPE silently drops
        # 'T'/'V' frames from an unregistered callsign.
        try:
            await asyncio.wait_for(registered.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("agwpe: registration not confirmed after 30 s; sending beacon anyway")
        except asyncio.CancelledError:
            return
        try:
            while self._running:
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
