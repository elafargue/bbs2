"""
bbs/ax25/address.py — AX.25 address string helpers.

The Linux kernel AF_AX25 socket layer presents callsigns as plain Python
strings in the form "CALLSIGN" or "CALLSIGN-SSID" (e.g. "W1AW", "W1AW-3").
No frame-level decoding is needed on the kernel path.

For the direct-KISS path (without kissattach) we do need to decode the binary
AX.25 address field; that is handled in bbs/ax25/kiss_frame.py and uses the
helpers here for the final string conversion.
"""
from __future__ import annotations

import re

# AX.25 allows A-Z, 0-9 in a callsign, max 6 chars, SSID 0-15.
_CALLSIGN_RE = re.compile(r"^([A-Z0-9]{1,6})(?:-(\d{1,2}))?$")


def parse(addr: str) -> tuple[str, int]:
    """
    Parse an AX.25 address string into (callsign, ssid).

    Accepts:  "W1AW"  →  ("W1AW", 0)
              "W1AW-3" → ("W1AW", 3)

    Raises ValueError for invalid input.
    """
    m = _CALLSIGN_RE.match(addr.strip().upper())
    if not m:
        raise ValueError(f"Invalid AX.25 address: {addr!r}")
    call = m.group(1)
    ssid = int(m.group(2)) if m.group(2) else 0
    if ssid > 15:
        raise ValueError(f"SSID {ssid} out of range 0-15 in address: {addr!r}")
    return call, ssid


def format_addr(callsign: str, ssid: int = 0) -> str:
    """
    Build a canonical AX.25 address string.

    format_addr("W1AW", 0) → "W1AW"
    format_addr("W1AW", 3) → "W1AW-3"
    """
    call = callsign.upper().strip()
    if ssid < 0 or ssid > 15:
        raise ValueError(f"SSID {ssid} out of range 0-15")
    return f"{call}-{ssid}" if ssid else call


def callsign_only(addr: str) -> str:
    """Return just the callsign part (no SSID)."""
    call, _ = parse(addr)
    return call
