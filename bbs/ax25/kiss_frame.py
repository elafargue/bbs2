"""
bbs/ax25/kiss_frame.py — Minimal AX.25 frame decoder used ONLY by the
direct-KISS transport (KISSSerialTransport / KISSTCPTransport).

When the kernel AX.25 stack is used (AF_AX25 sockets via kissattach) this
module is never imported; the kernel handles all framing.

We only need enough decoding to:
  1. Strip the KISS framing (FEND / FESC bytes).
  2. Extract the source callsign from the AX.25 address field.
  3. Extract the digipeater path (via) with has-been-repeated (*) markers.
  4. Extract the information (payload) field.

We do NOT implement the full AX.25 connected-mode state machine here —
the kernel handles that or Dire Wolf handles connected mode when operating
as a full TNC.  For direct-KISS without kissattach this transport operates
in UI (connectionless) frame mode only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bbs.ax25.address import format_addr

# KISS special bytes
FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

# AX.25 frame PID for no-layer-3 (plain text payload)
PID_NO_LAYER3 = 0xF0

# AX.25 address field: 7 bytes per address (6 char callsign + 1 SSID byte)
# Each character is shifted left by 1 bit; SSID byte: bits 3-6 hold SSID.
_ADDR_BYTES = 7


@dataclass
class KISSFrame:
    """Decoded KISS (or raw AX.25) frame."""
    port: int          # KISS TNC port (0-15); 0 for frames decoded from AGWPE
    dest_call: str     # Destination callsign ("W1AW-0")
    src_call: str      # Source callsign ("N0CALL-3")
    is_ui: bool        # True if UI frame (connectionless)
    pid: int           # Protocol ID (0xF0 = no layer 3)
    payload: bytes     # Information field bytes
    via: list[str] = field(default_factory=list)
    # Digipeater path in transmission order.
    # Each entry is "CALL-SSID" or "CALL-SSID*" where "*" means the
    # has-been-repeated (H) bit is set — i.e. that digi already forwarded it.
    # Example: ["WIDE1-1*", "KD6XYZ-3*", "WIDE2-1"] (last digi not yet repeated)


def kiss_unescape(data: bytes) -> bytes:
    """Remove KISS byte-stuffing (FESC sequences)."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == FESC:
            i += 1
            if i >= len(data):
                break
            nxt = data[i]
            if nxt == TFEND:
                out.append(FEND)
            elif nxt == TFESC:
                out.append(FESC)
            # else: malformed — skip
        else:
            out.append(b)
        i += 1
    return bytes(out)


def kiss_escape(data: bytes) -> bytes:
    """Apply KISS byte-stuffing."""
    out = bytearray()
    for b in data:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    return bytes(out)


def build_kiss_frame(port: int, ax25_frame: bytes) -> bytes:
    """Wrap an AX.25 frame in KISS framing."""
    command = (port & 0x0F) << 4  # Data frame, port in upper nibble
    return bytes([FEND, command]) + kiss_escape(ax25_frame) + bytes([FEND])


def _decode_callsign(addr_field: bytes) -> tuple[str, int]:
    """Decode a 7-byte AX.25 address field into (callsign, ssid)."""
    call_chars = []
    for b in addr_field[:6]:
        ch = (b >> 1) & 0x7F
        if ch != 0x20:  # ignore padding spaces
            call_chars.append(chr(ch))
    callsign = "".join(call_chars).strip()
    ssid = (addr_field[6] >> 1) & 0x0F
    return callsign, ssid


def _decode_ax25(ax25: bytes, port: int = 0) -> KISSFrame | None:
    """
    Decode a raw AX.25 frame (no KISS wrapper).

    Parses a variable-length address field: each 7-byte address ends when
    bit 0 of its SSID byte is set.  For digipeater entries, bit 7 of the
    SSID byte is the H (has-been-repeated) flag, shown as "*" in monitor
    output.
    """
    # Minimum: dest(7) + src(7) + control(1) = 15 bytes
    if len(ax25) < 15:
        return None

    dest_call, dest_ssid = _decode_callsign(ax25[0:7])
    src_call, src_ssid = _decode_callsign(ax25[7:14])

    # Walk the digipeater path.  Bit 0 of a SSID byte marks the last address.
    via: list[str] = []
    offset = 14  # byte index right after the src address field
    if not (ax25[13] & 0x01):  # src is not the last address — digipeaters follow
        while offset + _ADDR_BYTES <= len(ax25):
            digi_field = ax25[offset : offset + _ADDR_BYTES]
            digi_call, digi_ssid = _decode_callsign(digi_field)
            h_bit = bool(digi_field[6] & 0x80)   # has-been-repeated
            is_last = bool(digi_field[6] & 0x01)  # end of address field
            if digi_call:
                via.append(format_addr(digi_call, digi_ssid) + ("*" if h_bit else ""))
            offset += _ADDR_BYTES
            if is_last:
                break

    if offset >= len(ax25):
        return None

    control = ax25[offset]
    # UI frame: control = 0x03 (P/F bit = 0); 0x13 with P/F set is also valid.
    is_ui = (control & 0xEF) == 0x03

    if is_ui:
        if len(ax25) < offset + 2:
            return None
        pid = ax25[offset + 1]
        payload = ax25[offset + 2 :]
    else:
        pid = 0
        payload = ax25[offset + 1 :]

    return KISSFrame(
        port=port,
        dest_call=format_addr(dest_call, dest_ssid),
        src_call=format_addr(src_call, src_ssid),
        is_ui=is_ui,
        pid=pid,
        payload=payload,
        via=via,
    )


def decode_ax25_frame(ax25: bytes) -> KISSFrame | None:
    """
    Decode a raw AX.25 frame (no KISS framing).
    Used by the AGWPE transport for raw 'K' monitoring frames.
    """
    return _decode_ax25(ax25)


def decode_frame(raw: bytes) -> KISSFrame | None:
    """
    Decode a raw KISS frame (already stripped of leading/trailing FEND).
    Returns None if the frame is malformed or too short.
    """
    if len(raw) < 1:
        return None

    port = (raw[0] >> 4) & 0x0F
    frame_type = raw[0] & 0x0F
    if frame_type != 0x00:
        # Only data frames (type 0) carry AX.25; ignore others for now
        return None

    ax25 = kiss_unescape(raw[1:])
    return _decode_ax25(ax25, port)


def split_kiss_frames(buf: bytearray) -> tuple[list[bytes], bytearray]:
    """
    Extract all complete KISS frames from a byte buffer.
    Returns (list_of_raw_frames, remaining_bytes).
    Raw frames include the command byte but NOT the FEND delimiters.
    """
    frames: list[bytes] = []
    while True:
        start = buf.find(FEND)
        if start == -1:
            break
        end = buf.find(FEND, start + 1)
        if end == -1:
            break
        frame_data = bytes(buf[start + 1 : end])
        if frame_data:  # ignore empty frames between consecutive FENDs
            frames.append(frame_data)
        buf = buf[end + 1 :]  # keep remainder after the closing FEND
    return frames, buf
