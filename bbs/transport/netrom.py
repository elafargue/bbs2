"""
bbs/transport/netrom.py — NetROM (AF_NETROM) transport stub.

NetROM is a network-layer protocol that runs on top of AX.25 and provides
automatic routing between BBS nodes.  The Linux kernel supports AF_NETROM
sockets and, like AF_AX25, handles all the framing for you — your code just
calls accept() and reads/writes data.

Current status: STUBBED / DISABLED by default.
  NetROM is rarely used on modern networks (replaced by IP/AMPRNet) but the
  socket pattern is almost identical to kernel_ax25; enabling it later is a
  small effort.

To enable in the future:
  1. Load the netrom kernel module:  modprobe netrom
  2. Configure /etc/ax25/nrports
  3. Start netromd
  4. Set transports.netrom.enabled: true in bbs.yaml
  5. Uncomment the bind() call below and test.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from bbs.ax25.address import format_addr, parse
from bbs.transport.base import Connection, ConnectionCallback, Transport

logger = logging.getLogger(__name__)

try:
    _AF_NETROM = socket.AF_NETROM  # type: ignore[attr-defined]
except AttributeError:
    _AF_NETROM = None


class NetROMTransport(Transport):
    """
    AF_NETROM connected-mode transport.

    The session model is identical to KernelAX25Transport:
      - bind() to our callsign / alias
      - accept() → connected socket → asyncio streams
      - getpeername() → remote callsign string
    """

    transport_id = "netrom"

    def __init__(self, cfg: dict[str, Any], bbs_callsign: str) -> None:
        if _AF_NETROM is None:
            raise RuntimeError(
                "AF_NETROM is not available on this platform. "
                "netrom transport requires Linux with netrom kernel module."
            )
        call, ssid = parse(bbs_callsign)
        self._local_addr = format_addr(call, ssid)
        self._alias: str = cfg.get("alias", "BBS").upper()[:6]
        self._running = False
        self._server_sock: socket.socket | None = None

    async def start(self, on_connect: ConnectionCallback) -> None:
        if _AF_NETROM is None:
            raise RuntimeError("AF_NETROM not available")

        sock = socket.socket(_AF_NETROM, socket.SOCK_SEQPACKET)
        sock.setblocking(False)

        # NetROM bind address: (callsign, alias) tuple on Linux
        try:
            sock.bind((self._local_addr, self._alias))
        except OSError as exc:
            raise RuntimeError(
                f"Failed to bind AF_NETROM socket to {self._local_addr!r} "
                f"alias {self._alias!r}: {exc}"
            ) from exc

        sock.listen(10)
        self._server_sock = sock
        self._running = True
        logger.info(
            "netrom transport listening on %s (alias %s)",
            self._local_addr,
            self._alias,
        )

        loop = asyncio.get_running_loop()
        while self._running:
            try:
                client_sock, remote_addr = await loop.run_in_executor(
                    None, sock.accept
                )
            except OSError:
                if self._running:
                    logger.exception("netrom accept() failed")
                break

            remote_str = (
                remote_addr[0]
                if isinstance(remote_addr, tuple)
                else str(remote_addr)
            )
            logger.info("NetROM connection from %s", remote_str)
            asyncio.create_task(self._handle_client(client_sock, remote_str, on_connect))

    async def _handle_client(
        self,
        client_sock: socket.socket,
        remote_addr: str,
        on_connect: ConnectionCallback,
    ) -> None:
        client_sock.setblocking(False)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.create_connection(lambda: protocol, sock=client_sock)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        conn = Connection(
            remote_addr=remote_addr,
            reader=reader,
            writer=writer,
            transport_id=self.transport_id,
        )
        try:
            await on_connect(conn)
        except Exception:
            logger.exception("Error handling NetROM connection from %s", remote_addr)
        finally:
            await conn.close()
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
        logger.info("netrom transport stopped")
