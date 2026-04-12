"""
bbs/transport/base.py — Abstract transport interface.

A Transport is responsible for:
  - Listening for incoming connections (or connectionless frames).
  - Presenting each connected peer as an asyncio StreamReader / StreamWriter
    pair plus the remote callsign/address string.
  - Sending data back to a peer.

The BBS engine only talks to the Transport through this interface, so new
transports (e.g. VARA, Packet-AGW) can be added without touching core code.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class Connection:
    """
    Represents one live connection from a remote peer.

    remote_addr:  Canonical "CALLSIGN-SSID" string for AX.25 transports,
                  or "host:port" for TCP.  Always a non-empty string.
    reader:       asyncio.StreamReader for data arriving from the peer.
    writer:       asyncio.StreamWriter for data to send to the peer.
    transport_id: Short human-readable label for logging ("kernel_ax25", "kiss_tcp", …)
    """
    remote_addr: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    transport_id: str

    async def send(self, data: bytes) -> None:
        """Write *data* to the peer and drain the buffer."""
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        """Cleanly close the connection."""
        if not self.writer.is_closing():
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


# Type alias: callback the engine registers to receive new connections.
ConnectionCallback = Callable[[Connection], Awaitable[None]]


class Transport(ABC):
    """Base class for all BBS transports."""

    #: Short identifier used in logs and config keys.
    transport_id: str = "base"

    @abstractmethod
    async def start(self, on_connect: ConnectionCallback) -> None:
        """
        Start the transport and call *on_connect(conn)* for every new
        incoming connection.  This coroutine should run until
        :meth:`stop` is called.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the transport."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.transport_id!r}>"
