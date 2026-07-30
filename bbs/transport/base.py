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
    local_addr:   The *called* address — which of our callsign-SSIDs (AX.25) or
                  NET/ROM destination the peer connected to.  Empty when the
                  transport does not distinguish (e.g. TCP).  Used by the
                  service dispatcher to route a connection to an external
                  program vs. the internal BBS.
    """
    remote_addr: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    transport_id: str
    hop_count: int = 0
    local_addr: str = ""

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
# Arguments: src_call, dest_call, via (digipeater path), unix_ts, transport_id,
#            info (decoded AX.25 information field, may be empty).
HeardFrameCallback = Callable[[str, str, list[str], int, str, str], Awaitable[None]]

# Type alias: callback fired by transports when a NETROM UI frame arrives
# (PID=0xCF).  Arguments: src_call, dest_call, binary_payload (raw AX.25
# info field bytes, with PID byte already stripped by the transport).
NetromFrameCallback = Callable[[str, str, bytes], Awaitable[None]]

# Type alias: callable that builds the binary NETROM NODES payload to
# broadcast.  Returns None when there is nothing to send (e.g. routing
# table not yet populated).
NetromNodesBuilder = Callable[[], Optional[bytes]]


class Transport(ABC):
    """Base class for all BBS transports."""

    #: Short identifier used in logs and config keys.
    transport_id: str = "base"

    #: Optional observer for frames not addressed to this station.
    #: Set via set_heard_observer(); None if disabled.
    _heard_observer: Optional[HeardFrameCallback] = None

    #: Optional observer for NETROM UI frames (PID=0xCF).
    #: Set via set_netrom_observer(); None if disabled.
    _netrom_observer: Optional[NetromFrameCallback] = None

    #: Optional callable that builds the NODES broadcast payload.
    #: Set via set_netrom_nodes_builder(); None if NODES TX is disabled.
    _netrom_nodes_builder: Optional[NetromNodesBuilder] = None

    def set_heard_observer(self, cb: HeardFrameCallback) -> None:
        """Register *cb* as the callback for overheard (non-BBS) frames."""
        self._heard_observer = cb

    def set_netrom_observer(self, cb: NetromFrameCallback) -> None:
        """Register *cb* as the callback for received NETROM UI frames."""
        self._netrom_observer = cb

    def set_netrom_nodes_builder(self, cb: NetromNodesBuilder) -> None:
        """Register *cb* as the NODES payload builder for periodic broadcasts."""
        self._netrom_nodes_builder = cb

    def set_netrom_nodes_interval(self, seconds: int) -> None:
        """Set the NODES broadcast interval in seconds (default: 1800)."""

    def set_netrom_crosslink_enabled(self, enabled: bool = True) -> None:
        """Enable acceptance of inbound NETROM L3 crosslinks.

        Default implementation is a no-op; transports that can demultiplex
        a NETROM L3 frame on an AX.25 connected session (currently AGWPE)
        override this.  The classification of an incoming AX.25 connection
        as "NETROM crosslink" vs. "direct BBS user" is delegated to the
        callback registered via :meth:`set_netrom_neighbor_check`.
        """

    def set_netrom_neighbor_check(
        self, cb: "Callable[[str], bool]"
    ) -> None:
        """Register a synchronous callback that returns True iff *call*
        is a known NETROM neighbor.

        Used by the transport on an incoming AX.25 'C' to decide:
          - True  → don't start a BBS session, the first 'D' will be a
                    NETROM L3 CONNECT REQ; create the circuit manager
                    immediately and wait.
          - False → start a regular BBS session, send banner immediately.

        Looking the caller up against the router's adjacent-neighbor set
        is deterministic and removes the timing race that older
        classify-timeout based detection had.
        """

    def set_netrom_info_mtu(self, mtu: int) -> None:
        """Set the outbound NETROM L3 info-field MTU (bytes per fragment).

        Default-implementation no-op; AGWPE overrides.  Should be at
        most (TNC PACLEN − 20-byte L3 header).
        """

    def set_extra_callsigns(self, calls: list[str]) -> None:
        """Register additional callsign-SSIDs this transport should accept.

        Used by the service dispatcher (ax25d-style hosting): each SSID that
        maps to an external program must be accepted in addition to the BBS
        callsign so callers can connect to it.  Default no-op; transports
        that register callsigns with an external engine (AGWPE) override.
        """

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
