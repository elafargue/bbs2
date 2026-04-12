"""
bbs/transport/kiss.py — KISS transport over serial or TCP.

Used when Dire Wolf (or a hardware TNC) exposes a KISS interface WITHOUT
kissattach — i.e. AX.25 is NOT attached to a kernel interface.

In this mode we are operating at the raw AX.25 UI-frame level:
  - Each arriving KISS frame is decoded just enough to extract the source
    callsign (bbs/ax25/kiss_frame.py).
  - Each source callsign gets a virtual per-callsign "connection" backed by
    asyncio queues (since KISS/UI is connectionless, we simulate a connection
    by tracking conversation state per callsign).
  - Replies are sent as outgoing KISS UI frames addressed to the remote station.

This is strictly simpler / lower-feature than the kernel AX.25 path (no ARQ,
no flow control, no multi-hop digipeating state).  For a proper connected-mode
AX.25 BBS, prefer the kernel_ax25 transport with kissattach.

Both serial and TCP flavours are in this file; they share a common base.
"""
from __future__ import annotations

import asyncio
import logging
import struct
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


def _build_ax25_ui_frame(src: str, dest: str, payload: bytes) -> bytes:
    """Build a raw AX.25 UI frame (no kernel involvement)."""

    def encode_addr(callsign: str, ssid: int, last: bool) -> bytes:
        padded = callsign.upper().ljust(6)[:6]
        encoded = bytes((ord(c) << 1) for c in padded)
        ssid_byte = 0x60 | ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
        return encoded + bytes([ssid_byte])

    dest_call, dest_ssid = parse(dest)
    src_call, src_ssid = parse(src)

    addr_field = encode_addr(dest_call, dest_ssid, False) + encode_addr(
        src_call, src_ssid, True
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

    def __init__(self, bbs_callsign: str, kiss_port: int) -> None:
        call, ssid = parse(bbs_callsign)
        self._local_addr = format_addr(call, ssid)
        self._kiss_port = kiss_port
        self._running = False
        self._sessions: dict[str, asyncio.Queue[bytes]] = {}
        self._session_tasks: dict[str, asyncio.Task[None]] = {}
        self._raw_writer: asyncio.StreamWriter | None = None
        self._on_connect: ConnectionCallback | None = None

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
            await self.stop()

    async def _dispatch(self, frame: KISSFrame) -> None:
        """Route an incoming UI frame to the appropriate virtual session."""
        assert self._raw_writer is not None
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
        super().__init__(bbs_callsign, int(cfg.get("ax25_port", 0)))
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
        super().__init__(bbs_callsign, int(cfg.get("port", 0)))
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
