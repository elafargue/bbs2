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
"""
from __future__ import annotations

import asyncio
import logging
import socket
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


def _make_ax25_sockaddr(callsign: str, ssid: int) -> str:
    """
    On Linux, AF_AX25 addresses are presented to Python as plain strings
    "CALLSIGN-SSID" (or "CALLSIGN" for SSID 0).
    """
    return format_addr(callsign, ssid)


async def _socket_to_streams(
    sock: socket.socket,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap a connected socket in asyncio StreamReader / StreamWriter."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.create_connection(lambda: protocol, sock=sock)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


class KernelAX25Transport(Transport):
    """
    Listens on an AF_AX25 SEQPACKET socket for incoming connected-mode
    calls from remote stations.
    """

    transport_id = "kernel_ax25"

    def __init__(self, cfg: dict[str, Any], bbs_callsign: str) -> None:
        if _AF_AX25 is None:
            raise RuntimeError(
                "AF_AX25 is not available on this platform. "
                "kernel_ax25 transport requires Linux with ax25 kernel module."
            )
        call, ssid = parse(bbs_callsign)
        self._local_addr = _make_ax25_sockaddr(call, ssid)
        self._axport: str = cfg.get("axport", "ax0")
        self._running = False
        self._server_sock: socket.socket | None = None
        self._on_connect: ConnectionCallback | None = None

    async def start(self, on_connect: ConnectionCallback) -> None:
        self._on_connect = on_connect
        self._running = True

        sock = socket.socket(_AF_AX25, socket.SOCK_SEQPACKET)
        sock.setblocking(False)
        # Bind to our callsign on the configured axport.
        # The axport name is required as the second element of the address tuple
        # on Linux: (callsign, axport_index) — but Python's AF_AX25 binding
        # accepts a string address directly on modern kernels.
        try:
            sock.bind(self._local_addr)
        except OSError as exc:
            raise RuntimeError(
                f"Failed to bind AF_AX25 socket to {self._local_addr!r} "
                f"(axport {self._axport!r}): {exc}"
            ) from exc

        sock.listen(10)
        self._server_sock = sock
        logger.info(
            "kernel_ax25 transport listening on %s (axport %s)",
            self._local_addr,
            self._axport,
        )

        loop = asyncio.get_running_loop()
        while self._running:
            try:
                client_sock, remote_addr = await loop.run_in_executor(
                    None, sock.accept
                )
            except OSError:
                if self._running:
                    logger.exception("kernel_ax25 accept() failed")
                break

            # remote_addr from AF_AX25 getpeername() is a string "CALL-SSID"
            remote_str = (
                remote_addr if isinstance(remote_addr, str) else str(remote_addr)
            )
            logger.info("AX.25 connection from %s", remote_str)
            asyncio.create_task(
                self._handle_client(client_sock, remote_str)
            )

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
