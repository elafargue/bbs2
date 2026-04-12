"""
bbs/ax25/kiss_frame.py — Minimal AX.25 frame decoder used ONLY by the
direct-KISS transport (KISSSerialTransport / KISSTCPTransport).

When the kernel AX.25 stack is used (AF_AX25 sockets via kissattach) this
module is never imported; the kernel handles all framing.

We only need enough decoding to:
  1. Strip the KISS framing (FEND / FESC bytes).
  2. Extract the source callsign from the AX.25 address field.
  3. Extract the information (payload) field.

We do NOT implement the full AX.25 connected-mode state machine here —
the kernel handles that or Dire Wolf handles connected mode when operating
as a full TNC.  For direct-KISS without kissattach this transport operates
in UI (connectionless) frame mode only.
"""
from __future__ import annotations

from dataclasses import dataclass

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
    """Decoded KISS frame."""
    port: int          # KISS TNC port (0-15)
    dest_call: str     # Destination callsign ("W1AW-0")
    src_call: str      # Source callsign ("N0CALL-3")
    is_ui: bool        # True if UI frame (connectionless)
    pid: int           # Protocol ID (0xF0 = no layer 3)
    payload: bytes     # Information field bytes


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

    # Minimum AX.25 frame: dest(7) + src(7) + control(1) = 15 bytes
    if len(ax25) < 15:
        return None

    dest_call, dest_ssid = _decode_callsign(ax25[0:7])
    src_call, src_ssid = _decode_callsign(ax25[7:14])

    control = ax25[14]
    # UI frame: control = 0x03
    is_ui = (control == 0x03)

    # If UI frame there is a PID byte after control
    if is_ui:
        if len(ax25) < 16:
            return None
        pid = ax25[15]
        payload = ax25[16:]
    else:
        pid = 0
        payload = ax25[15:]

    return KISSFrame(
        port=port,
        dest_call=format_addr(dest_call, dest_ssid),
        src_call=format_addr(src_call, src_ssid),
        is_ui=is_ui,
        pid=pid,
        payload=payload,
    )


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
