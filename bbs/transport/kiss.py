"""
bbs/transport/kiss.py — KISS transport over serial or TCP.

╔═══════════════════════════════════════════════════════════════════════════╗
║  ⚠  UI-FRAMES ONLY  ⚠                                                     ║
║                                                                           ║
║  This transport is CONNECTIONLESS. It only handles AX.25 UI frames        ║
║  (APRS, beacons, ID broadcasts). It does NOT implement the AX.25 v2.0     ║
║  connected-mode state machine (SABM/UA/I-frames/RR/RNR/REJ).              ║
║                                                                           ║
║  A standard BBS client that sends a SABM to connect will TIME OUT — the   ║
║  transport sees the frame, creates a fake virtual "session," and never    ║
║  sends a UA reply because there is no AX.25 state machine here.           ║
║                                                                           ║
║  For real connected-mode BBS access, use one of:                          ║
║    • `agwpe`        — works on Linux, macOS, Windows; Direwolf has it     ║
║                       on TCP port 8000 by default                         ║
║    • `kernel_ax25`  — Linux only; requires kissattach + axports           ║
║                                                                           ║
║  This UI-only transport is useful for:                                    ║
║    • APRS-style position / status / message beacons                       ║
║    • Periodic BBS-availability beacons (BEACON, ID, QST destinations)     ║
║    • Heard-station logging (decoding monitored UI traffic)                ║
║                                                                           ║
║  Both serial (kiss_serial) and TCP (kiss_tcp) flavours share the same     ║
║  UI-only limitation.                                                      ║
╚═══════════════════════════════════════════════════════════════════════════╝

Implementation notes:
  - Each arriving KISS frame is decoded just enough to extract the source
    callsign (bbs/ax25/kiss_frame.py).
  - Each source callsign gets a virtual per-callsign "connection" backed by
    asyncio queues — useful only if the remote station is sending UI text
    we want to log/echo, not for negotiated BBS sessions.
  - Replies are sent as outgoing AX.25 v2.0 command-mode UI frames addressed
    to the remote station.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from abc import abstractmethod
from typing import Any

import serial_asyncio  # type: ignore[import-untyped]

from bbs.ax25.address import format_addr, parse
from bbs.ax25.kiss_frame import (
    FEND,
    KISSFrame,
    PID_NO_LAYER3,
    build_kiss_frame,
    decode_frame,
    kiss_escape,
    split_kiss_frames,
)
from bbs.transport.base import Connection, ConnectionCallback, Transport

logger = logging.getLogger(__name__)

# Idle-session cleanup: if a callsign sends nothing for this many seconds,
# close (tear down) its virtual connection.
_SESSION_IDLE_TIMEOUT = 300


def _build_ax25_ui_frame(
    src: str,
    dest: str,
    payload: bytes,
    via: list[str] | None = None,
) -> bytes:
    """Build a raw AX.25 v2.0 UI command frame (no kernel involvement).

    Encodes the C-bit pair as (dest=1, src=0) — i.e. command-mode UI, the
    standard for modern AX.25 v2.0/v2.2. If `via` is supplied, each callsign
    is appended to the address field as a digipeater entry with the H
    (has-been-repeated) bit clear, since we are the originator. The
    end-of-address bit moves from the source onto the last via.
    """

    def encode_addr(
        callsign: str, ssid: int, last: bool, high_bit: bool = False
    ) -> bytes:
        padded = callsign.upper().ljust(6)[:6]
        encoded = bytes((ord(c) << 1) for c in padded)
        # Bits 6-5 reserved (always 1) → 0x60. Bit 0 = end-of-address.
        # Bit 7 holds the C-bit for src/dest or the H-bit for digipeaters.
        ssid_byte = 0x60 | ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
        if high_bit:
            ssid_byte |= 0x80
        return encoded + bytes([ssid_byte])

    dest_call, dest_ssid = parse(dest)
    src_call, src_ssid = parse(src)
    vias = list(via or [])

    addr_field = encode_addr(
        dest_call, dest_ssid, False, high_bit=True
    ) + encode_addr(src_call, src_ssid, not vias, high_bit=False)
    for i, v in enumerate(vias):
        v_call, v_ssid = parse(v)
        addr_field += encode_addr(
            v_call, v_ssid, i == len(vias) - 1, high_bit=False
        )

    control = bytes([0x03])  # UI frame
    pid = bytes([PID_NO_LAYER3])
    return addr_field + control + pid + payload


class _KISSVirtualWriter:
    """
    Mimics asyncio.StreamWriter for a KISS UI virtual session.
    Outgoing bytes are wrapped in AX.25 UI frames and sent as KISS data.
    """

    def __init__(
        self,
        raw_writer: asyncio.StreamWriter,
        src_addr: str,   # BBS callsign
        dest_addr: str,  # remote station callsign
        kiss_port: int,
    ) -> None:
        self._raw = raw_writer
        self._src = src_addr
        self._dest = dest_addr
        self._port = kiss_port
        self._closing = False

    def write(self, data: bytes) -> None:
        ax25 = _build_ax25_ui_frame(self._src, self._dest, data)
        frame = build_kiss_frame(self._port, ax25)
        self._raw.write(frame)

    async def drain(self) -> None:
        await self._raw.drain()

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        pass  # Virtual — nothing to wait for

    def get_extra_info(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        return default


class _KISSBaseTransport(Transport):
    """Shared logic for KISS serial and TCP transports."""

    def __init__(self, bbs_callsign: str, kiss_port: int, cfg: dict[str, Any]) -> None:
        call, ssid = parse(bbs_callsign)
        self._local_addr = format_addr(call, ssid)
        self._kiss_port = kiss_port
        self._running = False
        self._sessions: dict[str, asyncio.Queue[bytes]] = {}
        self._session_tasks: dict[str, asyncio.Task[None]] = {}
        self._raw_writer: asyncio.StreamWriter | None = None
        self._on_connect: ConnectionCallback | None = None
        self._beacon_text: str = cfg.get("beacon_text", "").strip()
        self._beacon_dest: str = (
            cfg.get("beacon_dest", "BEACON").strip().upper() or "BEACON"
        )
        self._beacon_interval: int = max(1, int(cfg.get("beacon_interval", 20))) * 60
        raw_path = cfg.get("beacon_path", "")
        self._beacon_path: list[str] = [
            p.strip().upper() for p in raw_path.split(",") if p.strip()
        ]

    @abstractmethod
    async def _open_raw_streams(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open the underlying serial/TCP connection and return raw streams."""

    async def start(self, on_connect: ConnectionCallback) -> None:
        self._on_connect = on_connect
        self._running = True

        reader, writer = await self._open_raw_streams()
        self._raw_writer = writer
        logger.info("%s transport connected", self.transport_id)
        logger.warning(
            "%s is UI-FRAMES ONLY — connected-mode clients (SABM) will time out. "
            "For real BBS sessions use the 'agwpe' transport (Direwolf TCP 8000) "
            "or 'kernel_ax25' (Linux + kissattach). See bbs/transport/kiss.py "
            "docstring for details.",
            self.transport_id,
        )

        beacon_task: asyncio.Task[None] | None = None
        if self._beacon_text:
            beacon_task = asyncio.create_task(
                self._beacon_loop(), name=f"{self.transport_id}:beacon"
            )
            logger.info(
                "%s beacon enabled: every %d min to %s%s — %s",
                self.transport_id,
                self._beacon_interval // 60,
                self._beacon_dest,
                " via " + ",".join(self._beacon_path) if self._beacon_path else "",
                self._beacon_text,
            )

        buf = bytearray()
        try:
            while self._running:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                frames, buf = split_kiss_frames(buf)
                for raw_frame in frames:
                    decoded = decode_frame(raw_frame)
                    if decoded:
                        await self._dispatch(decoded)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("%s transport read error", self.transport_id)
        finally:
            if beacon_task:
                beacon_task.cancel()
            await self.stop()

    async def _dispatch(self, frame: KISSFrame) -> None:
        """Route an incoming UI frame to the appropriate virtual session."""
        assert self._raw_writer is not None

        # Frames not addressed to the BBS callsign — heard but not for us.
        if frame.dest_call.upper() != self._local_addr.upper():
            if self._heard_observer is not None:
                info = frame.payload.decode("latin-1", errors="replace").strip()
                await self._heard_observer(
                    frame.src_call, frame.dest_call, frame.via,
                    int(time.time()), self.transport_id, info,
                )
            return

        src = frame.src_call

        if src not in self._sessions:
            q: asyncio.Queue[bytes] = asyncio.Queue()
            self._sessions[src] = q

            virtual_writer = _KISSVirtualWriter(
                self._raw_writer, self._local_addr, src, self._kiss_port
            )
            # We need a real asyncio.StreamWriter for Connection — wrap it.
            # Since _KISSVirtualWriter is duck-typed we pass it directly via
            # Connection and rely on Connection.send() calling write()+drain().
            # To keep Connection typed correctly, we cast.
            conn = Connection(
                remote_addr=src,
                reader=_QueueStreamReader(q),  # type: ignore[arg-type]
                writer=virtual_writer,  # type: ignore[arg-type]
                transport_id=self.transport_id,
            )

            assert self._on_connect is not None
            task = asyncio.create_task(self._run_session(src, conn))
            self._session_tasks[src] = task

        await self._sessions[src].put(frame.payload)

    async def _run_session(self, src: str, conn: Connection) -> None:
        assert self._on_connect is not None
        logger.info("KISS virtual session started for %s", src)
        try:
            await self._on_connect(conn)
        except Exception:
            logger.exception("Error in KISS session for %s", src)
        finally:
            self._sessions.pop(src, None)
            self._session_tasks.pop(src, None)
            logger.info("KISS virtual session ended for %s", src)

    def _send_beacon(self) -> None:
        """Build and write a KISS UI beacon frame (fire-and-forget write)."""
        if not self._raw_writer or self._raw_writer.is_closing():
            return
        ax25 = _build_ax25_ui_frame(
            self._local_addr,
            self._beacon_dest,
            self._beacon_text.encode("ascii", errors="replace"),
            via=self._beacon_path or None,
        )
        frame = build_kiss_frame(self._kiss_port, ax25)
        self._raw_writer.write(frame)

    async def _beacon_loop(self) -> None:
        """Send a beacon immediately on start, then every beacon_interval seconds."""
        try:
            while self._running:
                if self._sessions:
                    # Don't beacon while a user is connected — save air time.
                    await asyncio.sleep(self._beacon_interval)
                    continue
                self._send_beacon()
                logger.debug("%s beacon sent", self.transport_id)
                await asyncio.sleep(self._beacon_interval)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        for task in list(self._session_tasks.values()):
            task.cancel()
        if self._raw_writer and not self._raw_writer.is_closing():
            self._raw_writer.close()
        logger.info("%s transport stopped", self.transport_id)


class _QueueStreamReader(asyncio.StreamReader):
    """
    A StreamReader whose data comes from an asyncio.Queue instead of a
    real transport.  Used for KISS virtual sessions.
    """

    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        super().__init__()
        self._queue = queue

    async def read(self, n: int = -1) -> bytes:
        # Yield from the queue; block until data arrives.
        data = await self._queue.get()
        return data

    async def readline(self) -> bytes:
        """Read until \\r or \\n (handles both line endings)."""
        buf = bytearray()
        while True:
            chunk = await self.read(1)
            if not chunk:
                return bytes(buf)
            buf.extend(chunk)
            if chunk in (b"\r", b"\n"):
                return bytes(buf)

    async def readexactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await self.read(n - len(buf))
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(buf), n)
            buf.extend(chunk)
        return bytes(buf)


class KISSTCPTransport(_KISSBaseTransport):
    """KISS over TCP connection to Dire Wolf (port 8001 by default)."""

    transport_id = "kiss_tcp"

    def __init__(self, cfg: dict[str, Any], bbs_callsign: str) -> None:
        super().__init__(bbs_callsign, int(cfg.get("ax25_port", 0)), cfg)
        self._host: str = cfg.get("host", "127.0.0.1")
        self._port: int = int(cfg.get("port", 8001))

    async def _open_raw_streams(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        logger.info("kiss_tcp connecting to %s:%d", self._host, self._port)
        return await asyncio.open_connection(self._host, self._port)


class KISSSerialTransport(_KISSBaseTransport):
    """KISS over a serial port (hardware TNC or Dire Wolf pseudo-TTY)."""

    transport_id = "kiss_serial"

    def __init__(self, cfg: dict[str, Any], bbs_callsign: str) -> None:
        super().__init__(bbs_callsign, int(cfg.get("port", 0)), cfg)
        self._device: str = cfg.get("device", "/dev/ttyACM0")
        self._baud: int = int(cfg.get("baud", 9600))

    async def _open_raw_streams(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        logger.info(
            "kiss_serial opening %s @ %d baud", self._device, self._baud
        )
        reader, writer = await serial_asyncio.open_serial_connection(
            url=self._device, baudrate=self._baud
        )
        return reader, writer
