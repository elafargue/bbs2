"""Transport factory — instantiates the enabled transports from bbs.yaml."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bbs.config import BBSConfig
    from bbs.transport.base import Transport


def build_transports(cfg: "BBSConfig") -> list["Transport"]:
    """Return a list of initialised (but not yet connected) transports."""
    from bbs.transport.kernel_ax25 import KernelAX25Transport
    from bbs.transport.kiss import KISSSerialTransport, KISSTCPTransport
    from bbs.transport.netrom import NetROMTransport
    from bbs.transport.tcp import TCPTransport

    transports: list[Transport] = []
    t = cfg.transports

    if t.get("kiss_serial", {}).get("enabled"):
        transports.append(KISSSerialTransport(t["kiss_serial"], cfg.callsign))

    if t.get("kiss_tcp", {}).get("enabled"):
        transports.append(KISSTCPTransport(t["kiss_tcp"], cfg.callsign))

    if t.get("kernel_ax25", {}).get("enabled"):
        transports.append(KernelAX25Transport(t["kernel_ax25"], cfg.callsign))

    if t.get("netrom", {}).get("enabled"):
        transports.append(NetROMTransport(t["netrom"], cfg.callsign))

    if t.get("tcp", {}).get("enabled"):
        transports.append(TCPTransport(t["tcp"]))

    return transports
