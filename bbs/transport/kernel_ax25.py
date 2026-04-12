"""
bbs/transport/kernel_ax25.py — Linux kernel AX.25 transport (AF_AX25).

Prerequisites on the host:
    modprobe ax25
    kissattach /dev/ttyACM0 <axport>   # or via Dire Wolf + kissattach
    # /etc/ax25/axports entry for <axport>

The kernel handles all AX.25 framing, connected-mode state machine, and
flow control.  We simply bind an AF_AX25 SEQPACKET socket to our callsign
and accept() incoming connections — exactly like a TCP server.

The remote callsign is available via getpeername(), which returns a string
like "W1AW-3" on Linux.  No frame parsing needed.

Note: asyncio has no native AF_AX25 support, so we run accept() in an
executor thread and then hand the socket off to asyncio via
asyncio.open_connection on an already-connected socket (using
asyncio.StreamReaderProtocol machinery).

IMPORTANT — why we use ctypes for bind/accept/sendto:
CPython's socketmodule.c exports the AF_AX25 constant but has NO case for
it in either getsockaddrarg() or getsockaddrlen().  Every call to
sock.bind(), sock.accept(), or sock.sendto() on an AF_AX25 socket falls
through to the default branch which raises OSError("bad family").
We bypass Python's socket methods by calling libc's bind(), accept(), and
sendto() directly, passing a manually-packed struct sockaddr_ax25.

struct sockaddr_ax25 layout (linux/ax25.h, native alignment):
    sa_family_t  sax25_family;  // 2 bytes (unsigned short) = AF_AX25 = 3
    ax25_address sax25_call;    // 7 bytes (6 shifted chars + SSID byte)
    int          sax25_ndigis; // 4 bytes (needs 3-byte pad → total 16 B)
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import os
import socket
import struct
from typing import Any

from bbs.ax25.address import format_addr, parse
from bbs.transport.base import Connection, ConnectionCallback, Transport

logger = logging.getLogger(__name__)

# AF_AX25 is not always exposed in Python's socket module on non-Linux or
# in virtual environments.  Guard so import doesn't crash on macOS / CI.
try:
    _AF_AX25 = socket.AF_AX25  # type: ignore[attr-defined]
    _AF_NETROM = socket.AF_NETROM  # type: ignore[attr-defined]
except AttributeError:
    _AF_AX25 = None
    _AF_NETROM = None

# Load libc for direct syscall bypass (AF_AX25 address operations).
_libc: ctypes.CDLL | None = None
try:
    _libname = ctypes.util.find_library("c")
    if _libname:
        _libc = ctypes.CDLL(_libname, use_errno=True)
except (OSError, TypeError):
    pass

# Native-aligned sockaddr_ax25: 2-byte family + 7-byte call + (3-pad) + 4-byte ndigis = 16 bytes
_SOCKADDR_AX25_FMT = "@H7si"
_SOCKADDR_AX25_SIZE = struct.calcsize(_SOCKADDR_AX25_FMT)
# Linux requires addrlen == sizeof(full_sockaddr_ax25) = 16 + 8×7 = 72 when ndigis != 0
_AX25_MAX_DIGIS = 8


def _encode_ax25_call(callsign_str: str) -> bytes:
    """
    Encode a callsign string to a 7-byte AX.25 address.

    Each of the 6 callsign chars is left-shifted by 1; the string is
    right-padded with spaces (0x40 each after shifting).  The 7th byte
    is the SSID << 1.
    """
    if "-" in callsign_str:
        call_part, ssid_str = callsign_str.split("-", 1)
        ssid = int(ssid_str)
    else:
        call_part = callsign_str
        ssid = 0
    call_part = call_part.upper().ljust(6)[:6]
    return bytes(ord(c) << 1 for c in call_part) + bytes([ssid << 1])


def _pack_sockaddr_ax25(callsign_str: str) -> bytes:
    """Pack a plain struct sockaddr_ax25 (no digipeater path) for bind()."""
    return struct.pack(_SOCKADDR_AX25_FMT, _AF_AX25, _encode_ax25_call(callsign_str), 0)


def _pack_full_sockaddr_ax25(dest_callsign: str, path: list[str]) -> bytes:
    """
    Pack a struct full_sockaddr_ax25 for sendto() with optional digipeater path.

    Linux ax25_sendmsg() requires addrlen == sizeof(struct full_sockaddr_ax25)
    = 16 + AX25_MAX_DIGIS×7 = 72 bytes whenever sax25_ndigis != 0; passing a
    shorter buffer returns EINVAL.  We always produce 72 bytes, zeroing unused
    digi slots, so the same buffer works for both no-path and path cases.
    """
    ndigis = min(len(path), _AX25_MAX_DIGIS)
    base = struct.pack(_SOCKADDR_AX25_FMT, _AF_AX25, _encode_ax25_call(dest_callsign), ndigis)
    digi_bytes = b"".join(_encode_ax25_call(d) for d in path[:ndigis])
    # Zero-pad to exactly AX25_MAX_DIGIS slots
    digi_bytes = digi_bytes.ljust(_AX25_MAX_DIGIS * 7, b"\x00")
    return base + digi_bytes


def _decode_sockaddr_ax25(raw: bytes) -> str:
    """
    Decode a packed struct sockaddr_ax25 back to a callsign string.
    The 7-byte ax25_call starts at byte offset 2 (after the 2-byte family field).
    """
    if len(raw) < 9:
        return "UNKNOWN"
    call_bytes = raw[2:9]
    call_chars = "".join(chr(b >> 1) for b in call_bytes[:6]).rstrip()
    ssid = (call_bytes[6] >> 1) & 0x0F
    if ssid:
        return f"{call_chars}-{ssid}"
    return call_chars


def _bind_ax25(sock_fd: int, sockaddr: bytes) -> None:
    """Call libc bind() directly with a packed sockaddr_ax25."""
    if _libc is None:
        raise OSError("libc not available for AF_AX25 bind")
    buf = ctypes.create_string_buffer(sockaddr)
    result = _libc.bind(sock_fd, buf, len(sockaddr))
    if result != 0:
        errno_val = ctypes.get_errno()
        raise OSError(errno_val, os.strerror(errno_val))


def _accept_ax25(sock_fd: int) -> tuple[int, str]:
    """
    Blocking libc accept() for an AF_AX25 socket.
    Returns (new_client_fd, remote_callsign_str).
    Intended to run in a thread executor.
    """
    if _libc is None:
        raise OSError("libc not available for AF_AX25 accept")
    buf = ctypes.create_string_buffer(_SOCKADDR_AX25_SIZE)
    addrlen = ctypes.c_uint32(_SOCKADDR_AX25_SIZE)
    new_fd = _libc.accept(sock_fd, buf, ctypes.byref(addrlen))
    if new_fd < 0:
        errno_val = ctypes.get_errno()
        raise OSError(errno_val, os.strerror(errno_val))
    captured = min(addrlen.value, _SOCKADDR_AX25_SIZE)
    remote_str = _decode_sockaddr_ax25(bytes(buf.raw[:captured]))
    return new_fd, remote_str


def _sendto_ax25(sock_fd: int, data: bytes, dest_sockaddr: bytes) -> None:
    """Call libc sendto() directly with a packed sockaddr_ax25 destination."""
    if _libc is None:
        raise OSError("libc not available for AF_AX25 sendto")
    data_buf = ctypes.create_string_buffer(data)
    addr_buf = ctypes.create_string_buffer(dest_sockaddr)
    result = _libc.sendto(sock_fd, data_buf, len(data), 0, addr_buf, len(dest_sockaddr))
    if result < 0:
        errno_val = ctypes.get_errno()
        raise OSError(errno_val, os.strerror(errno_val))


class _SeqpacketTransport(asyncio.Transport):
    """
    Minimal asyncio.Transport for AF_AX25 SOCK_SEQPACKET sockets.

    asyncio.loop.create_connection() validates sock.type == SOCK_STREAM and
    raises ValueError for SOCK_SEQPACKET.  This transport wires up the same
    asyncio stream machinery (StreamReader / StreamWriter) manually, using
    loop.add_reader() for incoming data and a simple write buffer drained
    via loop.add_writer() on EAGAIN.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        sock: socket.socket,
        protocol: asyncio.StreamReaderProtocol,
    ) -> None:
        super().__init__()
        self._loop = loop
        self._sock = sock
        self._protocol = protocol
        self._closing = False
        self._write_buf = bytearray()
        self._write_scheduled = False
        loop.add_reader(sock.fileno(), self._read_ready)

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def _read_ready(self) -> None:
        try:
            data = self._sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            self._fatal_error(exc)
            return
        if data:
            self._protocol.data_received(data)
        else:
            # Peer closed the connection.
            self._loop.remove_reader(self._sock.fileno())
            keep_open = self._protocol.eof_received()
            if not keep_open:
                self.close()

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        if not data or self._closing:
            return
        self._write_buf += data
        if not self._write_scheduled:
            self._try_write()

    def _try_write(self) -> None:
        while self._write_buf:
            try:
                n = self._sock.send(self._write_buf)
                del self._write_buf[:n]
            except (BlockingIOError, InterruptedError):
                self._write_scheduled = True
                self._loop.add_writer(self._sock.fileno(), self._write_ready)
                return
            except OSError as exc:
                self._fatal_error(exc)
                return
        self._write_scheduled = False

    def _write_ready(self) -> None:
        self._loop.remove_writer(self._sock.fileno())
        self._write_scheduled = False
        self._try_write()

    def can_write_eof(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self._loop.remove_reader(self._sock.fileno())
            if self._write_scheduled:
                self._loop.remove_writer(self._sock.fileno())
            self._sock.close()
            self._loop.call_soon(self._protocol.connection_lost, None)

    def abort(self) -> None:
        self._closing = True
        self._loop.remove_reader(self._sock.fileno())
        if self._write_scheduled:
            self._loop.remove_writer(self._sock.fileno())
        self._sock.close()
        self._loop.call_soon(self._protocol.connection_lost, None)

    def _fatal_error(self, exc: Exception) -> None:
        self._closing = True
        self._loop.remove_reader(self._sock.fileno())
        if self._write_scheduled:
            self._loop.remove_writer(self._sock.fileno())
        self._sock.close()
        self._loop.call_soon(self._protocol.connection_lost, exc)

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "socket":
            return self._sock
        if name == "sockname":
            try:
                return self._sock.getsockname()
            except OSError:
                return default
        if name == "peername":
            try:
                return self._sock.getpeername()
            except OSError:
                return default
        return default


async def _socket_to_streams(
    sock: socket.socket,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    Wrap a connected AF_AX25 SEQPACKET socket in asyncio StreamReader /
    StreamWriter without using loop.create_connection(), which only accepts
    SOCK_STREAM sockets.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport = _SeqpacketTransport(loop, sock, protocol)
    protocol.connection_made(transport)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


class KernelAX25Transport(Transport):
    """
    Listens on an AF_AX25 SEQPACKET socket for incoming connected-mode
    calls from remote stations.

    A separate SOCK_DGRAM socket is used for periodic UI beacon frames
    (beacon_text / beacon_interval in config).  The two sockets are
    independent: one per-connection SEQPACKET listener, one write-only
    DGRAM sender.
    """

    transport_id = "kernel_ax25"

    def __init__(self, cfg: dict[str, Any], bbs_callsign: str) -> None:
        if _AF_AX25 is None:
            raise RuntimeError(
                "AF_AX25 is not available on this platform. "
                "kernel_ax25 transport requires Linux with ax25 kernel module."
            )
        call, ssid = parse(bbs_callsign)
        self._local_addr_str = format_addr(call, ssid)
        self._local_sockaddr = _pack_sockaddr_ax25(self._local_addr_str)
        self._axport: str = cfg.get("axport", "ax0")
        self._running = False
        self._server_sock: socket.socket | None = None
        self._on_connect: ConnectionCallback | None = None
        self._beacon_text: str = cfg.get("beacon_text", "").strip()
        self._beacon_interval: int = max(1, int(cfg.get("beacon_interval", 20))) * 60
        raw_path = cfg.get("beacon_path", "")
        self._beacon_path: list[str] = [
            p.strip() for p in raw_path.split(",") if p.strip()
        ]

    async def start(self, on_connect: ConnectionCallback) -> None:
        self._on_connect = on_connect
        self._running = True

        sock = socket.socket(_AF_AX25, socket.SOCK_SEQPACKET)
        # Keep the server socket blocking — accept() runs in a thread executor
        # and must block until a connection arrives.
        try:
            _bind_ax25(sock.fileno(), self._local_sockaddr)
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                f"Failed to bind AF_AX25 socket to {self._local_addr_str!r} "
                f"(axport {self._axport!r}): {exc}  "
                f"— is the ax25 kernel module loaded? "
                f"Try: sudo modprobe ax25 && sudo modprobe mkiss"
            ) from exc

        sock.listen(10)
        self._server_sock = sock
        server_fd = sock.fileno()
        logger.info(
            "kernel_ax25 transport listening on %s (axport %s)",
            self._local_addr_str,
            self._axport,
        )

        loop = asyncio.get_running_loop()

        beacon_task: asyncio.Task[None] | None = None
        if self._beacon_text:
            beacon_task = asyncio.create_task(
                self._beacon_loop(), name="kernel_ax25:beacon"
            )
            logger.info(
                "kernel_ax25 beacon enabled: every %d min — %s",
                self._beacon_interval // 60,
                self._beacon_text,
            )

        while self._running:
            try:
                new_fd, remote_str = await loop.run_in_executor(
                    None, _accept_ax25, server_fd
                )
            except OSError:
                if self._running:
                    logger.exception("kernel_ax25 accept() failed")
                break

            logger.info("AX.25 connection from %s", remote_str)
            # Wrap the raw fd in a Python socket object; explicit family/type
            # avoids getsockname() auto-detection which fails for AF_AX25.
            client_sock = socket.socket(_AF_AX25, socket.SOCK_SEQPACKET, fileno=new_fd)
            asyncio.create_task(
                self._handle_client(client_sock, remote_str)
            )

        if beacon_task:
            beacon_task.cancel()

    def _send_beacon(self) -> None:
        """Send a single UI beacon frame via a SOCK_DGRAM AF_AX25 socket."""
        try:
            sock = socket.socket(_AF_AX25, socket.SOCK_DGRAM)  # type: ignore[arg-type]
            dest_sockaddr = _pack_full_sockaddr_ax25("BEACON", self._beacon_path)
            _bind_ax25(sock.fileno(), self._local_sockaddr)
            payload = self._beacon_text.encode("ascii", errors="replace")
            _sendto_ax25(sock.fileno(), payload, dest_sockaddr)
            sock.close()
        except OSError:
            logger.warning("kernel_ax25 beacon send failed", exc_info=True)

    async def _beacon_loop(self) -> None:
        """Send beacon immediately on start, then every beacon_interval seconds."""
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                await loop.run_in_executor(None, self._send_beacon)
                logger.debug("kernel_ax25 beacon sent")
                await asyncio.sleep(self._beacon_interval)
        except asyncio.CancelledError:
            pass

    async def _handle_client(
        self, client_sock: socket.socket, remote_addr: str
    ) -> None:
        assert self._on_connect is not None
        client_sock.setblocking(False)
        try:
            reader, writer = await _socket_to_streams(client_sock)
            conn = Connection(
                remote_addr=remote_addr,
                reader=reader,
                writer=writer,
                transport_id=self.transport_id,
            )
            await self._on_connect(conn)
        except Exception:
            logger.exception(
                "Error handling kernel_ax25 connection from %s", remote_addr
            )
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    async def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        logger.info("kernel_ax25 transport stopped")
