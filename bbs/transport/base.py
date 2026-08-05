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
from typing import TYPE_CHECKING, Callable, Awaitable, Optional

if TYPE_CHECKING:
    from bbs.netrom.circuit import NetromCircuitManager


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
    netrom_via:   For an inbound NET/ROM circuit, the adjacent AX.25 neighbor
                  (crosslink) that carried it to us; empty for a direct RF/TCP
                  user or any non-NET/ROM path.  Consumed by the node's INTERLOCK
                  guard (N4a) to refuse routing a circuit back out its arrival
                  link.
    """
    remote_addr: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    transport_id: str
    hop_count: int = 0
    local_addr: str = ""
    netrom_via: str = ""

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

    #: Optional observer for NETROM crosslink up/down — cb(neighbor, up).
    #: Set via set_netrom_crosslink_observer(); the router's note_crosslink.
    _netrom_crosslink_observer: "Optional[Callable[[str, bool], None]]" = None

    def set_heard_observer(self, cb: HeardFrameCallback) -> None:
        """Register *cb* as the callback for overheard (non-BBS) frames."""
        self._heard_observer = cb

    def set_netrom_crosslink_observer(
        self, cb: "Callable[[str, bool], None]"
    ) -> None:
        """Register ``cb(neighbor, up)`` fired when a NETROM crosslink to a
        neighbor is established (up=True) or torn down (up=False).

        A live crosslink is definitive proof of one-hop adjacency; the router
        registers its ``note_crosslink`` here.  Default no-op; AGWPE fires it.
        """
        self._netrom_crosslink_observer = cb

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

    def set_netrom_link_idle_timeout(self, seconds: float) -> None:
        """Set how long a circuit-less NETROM crosslink stays up before we
        disconnect it (seconds; 0 = keep up indefinitely).

        Default-implementation no-op; AGWPE overrides.
        """

    def set_netrom_node_call(self, call: str) -> None:
        """Set the NET/ROM node callsign outbound crosslinks and NODES
        broadcasts originate from (N3).

        Defaults to the BBS callsign; when a distinct node SSID is configured
        (``netrom.node_ssid``) the engine sets it here so this station presents
        a first-class node identity on the air — its NODES self-advertisement
        and every ``connect_out`` crosslink source from the node SSID.  The
        node SSID is also registered with the radio (AGWPE registers it in
        :meth:`start`) so inbound connects to it reach us.

        Default-implementation no-op; AGWPE overrides.
        """

    def set_broadcast_state_path(self, path: str) -> None:
        """Persist the last beacon / NODES broadcast timestamps to *path* so that
        after a restart the transport respects the configured cadence instead of
        transmitting immediately (politer on a shared channel).

        Default-implementation no-op; AGWPE overrides.
        """

    def set_extra_callsigns(self, calls: list[str]) -> None:
        """Register additional callsign-SSIDs this transport should accept.

        Used by the service dispatcher (ax25d-style hosting): each SSID that
        maps to an external program must be accepted in addition to the BBS
        callsign so callers can connect to it.  Default no-op; transports
        that register callsigns with an external engine (AGWPE) override.
        """

    async def connect_netrom(
        self, neighbor: str
    ) -> "Optional[NetromCircuitManager]":
        """Originate (or reuse) a NETROM crosslink to *neighbor* and return
        its circuit manager, on which the caller can then
        ``originate_circuit(dest_node, user)``.

        *neighbor* must be an adjacent AX.25 callsign we can reach directly
        (NET/ROM does its own L3 routing from there).  Returns None on
        transports that cannot originate NET/ROM crosslinks; AGWPE overrides
        this to perform the outbound AX.25 connect.  Implementations that do
        support it may raise ``ConnectionError`` / ``asyncio.TimeoutError``
        if the crosslink cannot be established.
        """
        return None

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
