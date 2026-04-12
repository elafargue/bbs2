"""
bbs/transport/tcp.py — Plain TCP transport.

Used for:
  • Local testing with nc / minicom / telnet.
  • Exposing the BBS to local network Telnet clients.

Each accepted TCP connection becomes a Connection with remote_addr set to
"host:port".  (No callsign is extracted here; the BBS greeter will ask for
one in ASCII/Telnet mode or rely on the user typing their callsign.)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bbs.transport.base import Connection, ConnectionCallback, Transport

logger = logging.getLogger(__name__)


class TCPTransport(Transport):
    transport_id = "tcp"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._host: str = cfg.get("host", "0.0.0.0")
        self._port: int = int(cfg.get("port", 6300))
        self._server: asyncio.Server | None = None

    async def start(self, on_connect: ConnectionCallback) -> None:
        async def _client_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            peername = writer.get_extra_info("peername", ("unknown", 0))
            remote_addr = f"{peername[0]}:{peername[1]}"
            conn = Connection(
                remote_addr=remote_addr,
                reader=reader,
                writer=writer,
                transport_id=self.transport_id,
            )
            logger.info("TCP connection from %s", remote_addr)
            try:
                await on_connect(conn)
            except Exception:
                logger.exception("Error handling TCP connection from %s", remote_addr)
            finally:
                await conn.close()

        self._server = await asyncio.start_server(
            _client_handler, self._host, self._port
        )
        addrs = [s.getsockname() for s in self._server.sockets]
        logger.info("TCP transport listening on %s", addrs)
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("TCP transport stopped")
