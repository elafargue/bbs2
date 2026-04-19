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
from typing import Callable, Awaitable, Optional


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

# Type alias: callback fired by transports when a frame is heard but NOT
# addressed to the BBS callsign.
# Arguments: src_call, dest_call, via (digipeater path), unix_ts, transport_id.
HeardFrameCallback = Callable[[str, str, list[str], int, str], Awaitable[None]]


class Transport(ABC):
    """Base class for all BBS transports."""

    #: Short identifier used in logs and config keys.
    transport_id: str = "base"

    #: Optional observer for frames not addressed to this station.
    #: Set via set_heard_observer(); None if disabled.
    _heard_observer: Optional[HeardFrameCallback] = None

    def set_heard_observer(self, cb: HeardFrameCallback) -> None:
        """Register *cb* as the callback for overheard (non-BBS) frames."""
        self._heard_observer = cb

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
