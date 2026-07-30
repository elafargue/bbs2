"""
bbs/ax25/netrom_frame.py — NETROM protocol frame codec.

NETROM is a network-layer protocol that rides over AX.25 frames with PID=0xCF.
Two distinct AX.25 transport modes carry NETROM:

  1. NODES broadcast  — routing table advertisement.  AX.25 UI frame to dest
                        "NODES" (wire: FF FF FF FF FF FF FF), PID=0xCF.
  2. L3/L4 data frame — connection and data frames between nodes.  AX.25
                        connected-mode I-frame on a "crosslink" session,
                        PID=0xCF.  Carries the 20-byte NETROM header plus
                        per-opcode payload (CONNECT REQ/ACK, INFO, INFO ACK,
                        DISC REQ/ACK).

Reference: TheNet X-1J source, KPC-3XE manual NETROM chapter, live capture
on 145.05 MHz.

──────────────────────────────────────────────────────────────────────────
NODES broadcast payload layout
──────────────────────────────────────────────────────────────────────────
  Byte  0       : 0xFF  — routing-broadcast discriminator
  Bytes 1–6     : source alias (6 bytes, space-padded ASCII)
  Bytes 7…end   : N × 21-byte node entries:
    Bytes  0–6  : destination callsign  (7-byte AX.25 wire format)
    Bytes  7–12 : destination alias     (6 bytes, space-padded ASCII)
    Bytes 13–19 : best-neighbor callsign (7-byte AX.25 wire format)
    Byte  20    : quality               (0–255; 255 = direct link)

──────────────────────────────────────────────────────────────────────────
AX.25 wire-format address (7 bytes)
──────────────────────────────────────────────────────────────────────────
  Bytes 0–5 : callsign ASCII characters each shifted left 1 bit.
              'A'=0x41 → 0x82.  Shorter callsigns padded with 0x40
              (space×2).
  Byte  6   : SSID byte.  Bits 4–1 carry the SSID value (0–15).
              Other bits (C, R, end-of-address) are 0 in NETROM
              embedded addresses.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PID_NETROM: int = 0xCF
_ROUTING_DISCRIMINATOR: int = 0xFF

_ALIAS_LEN: int = 6
_ADDR_WIRE_LEN: int = 7
_NODE_ENTRY_LEN: int = _ADDR_WIRE_LEN + _ALIAS_LEN + _ADDR_WIRE_LEN + 1  # 21


# ── AX.25 wire-format helpers ─────────────────────────────────────────────────

def decode_wire_call(data: bytes) -> str:
    """
    Decode a 7-byte AX.25 wire-format address to a callsign string.

    Returns "CALL" or "CALL-N" (SSID omitted when 0).
    """
    call = "".join(
        chr((b >> 1) & 0x7F)
        for b in data[:6]
        if (b >> 1) & 0x7F not in (0, 0x20)   # skip NUL and space padding
    )
    ssid = (data[6] >> 1) & 0x0F
    return f"{call}-{ssid}" if ssid else call


def encode_wire_call(addr: str) -> bytes:
    """
    Encode "CALL" or "CALL-N" to 7-byte AX.25 wire format.

    Pads the callsign to 6 characters with spaces, shifts each character
    left by 1 bit, and packs the SSID into bits 4–1 of byte 6.
    """
    parts = addr.upper().rsplit("-", 1)
    call = parts[0][:6].ljust(6)
    ssid = int(parts[1]) & 0x0F if len(parts) > 1 else 0
    call_bytes = bytes((ord(c) << 1) & 0xFE for c in call)
    ssid_byte = (ssid << 1) & 0x1E
    return call_bytes + bytes([ssid_byte])


def _decode_alias(data: bytes) -> str:
    """Decode a 6-byte space-padded ASCII alias, stripping trailing spaces."""
    return data[:_ALIAS_LEN].decode("ascii", errors="replace").rstrip()


def _encode_alias(alias: str) -> bytes:
    """Encode an alias to 6-byte space-padded ASCII (upper-cased, truncated)."""
    return alias.upper()[:_ALIAS_LEN].ljust(_ALIAS_LEN).encode("ascii", errors="replace")


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class NodeEntry:
    """One entry in a NETROM NODES broadcast."""
    dest_call: str       # destination node callsign, e.g. "N6ZX-5"
    alias: str           # destination node alias, e.g. "WBAY"
    neighbor_call: str   # best next-hop callsign to reach dest
    quality: int         # link quality 0–255; 255 = direct RF link


@dataclass
class NodesFrame:
    """Decoded NETROM NODES routing broadcast."""
    source_call: str                             # AX.25 source callsign
    source_alias: str                            # NETROM alias of the sender
    entries: list[NodeEntry] = field(default_factory=list)


# ── Codec ─────────────────────────────────────────────────────────────────────

def decode_nodes_broadcast(src_call: str, payload: bytes) -> NodesFrame | None:
    """
    Decode a NETROM NODES broadcast payload (the AX.25 info field).

    *src_call* is the AX.25 source callsign (already parsed by the transport).
    Returns None if the payload is not a valid routing broadcast.
    """
    if len(payload) < 1 + _ALIAS_LEN:
        return None
    if payload[0] != _ROUTING_DISCRIMINATOR:
        return None

    source_alias = _decode_alias(payload[1:1 + _ALIAS_LEN])
    entries: list[NodeEntry] = []

    offset = 1 + _ALIAS_LEN
    while offset + _NODE_ENTRY_LEN <= len(payload):
        chunk = payload[offset: offset + _NODE_ENTRY_LEN]
        dest_call    = decode_wire_call(chunk[0:_ADDR_WIRE_LEN])
        dest_alias   = _decode_alias(chunk[_ADDR_WIRE_LEN: _ADDR_WIRE_LEN + _ALIAS_LEN])
        nbr_call     = decode_wire_call(chunk[_ADDR_WIRE_LEN + _ALIAS_LEN:
                                              _ADDR_WIRE_LEN + _ALIAS_LEN + _ADDR_WIRE_LEN])
        quality      = chunk[_NODE_ENTRY_LEN - 1]
        if dest_call:
            entries.append(NodeEntry(
                dest_call=dest_call,
                alias=dest_alias,
                neighbor_call=nbr_call,
                quality=quality,
            ))
        offset += _NODE_ENTRY_LEN

    return NodesFrame(source_call=src_call, source_alias=source_alias, entries=entries)


def encode_nodes_broadcast(source_alias: str, entries: list[NodeEntry]) -> bytes:
    """
    Encode a NETROM NODES broadcast payload (the AX.25 info field only;
    the AX.25 UI frame wrapper with PID=0xCF is added by the transport).
    """
    out = bytearray([_ROUTING_DISCRIMINATOR])
    out += _encode_alias(source_alias)
    for e in entries:
        out += encode_wire_call(e.dest_call)
        out += _encode_alias(e.alias)
        out += encode_wire_call(e.neighbor_call)
        out.append(e.quality & 0xFF)
    return bytes(out)


# ══════════════════════════════════════════════════════════════════════════════
# L3 / L4 frames — connection-oriented NETROM (crosslink sessions)
# ══════════════════════════════════════════════════════════════════════════════
#
# Layout of the 20-byte NETROM header:
#   Bytes  0– 6 : Origin node callsign  (AX.25 wire format, NETROM-stripped:
#                 bits 7,6,5 of SSID byte are 0; EOA bit not set)
#   Bytes  7–13 : Destination node callsign (same format; EOA bit set on
#                 byte 13 per spec)
#   Byte  14    : TTL (time-to-live, decremented at each forwarding hop)
#   Byte  15    : Circuit Index — caller convention; see encoder docstrings
#   Byte  16    : Circuit ID    — serial number qualifying the index
#   Byte  17    : TX Sequence Number
#   Byte  18    : RX Sequence Number
#   Byte  19    : Opcode (low 3 bits) + Flags (bits 5,6,7)
#
# The per-opcode trailers vary:
#   CONNECT REQUEST     : 1B proposed window + 7B user call + 7B origin node
#                         (35 bytes total)
#   CONNECT ACK         : 1B accepted window (or 0 + refusal bit in opcode byte)
#                         (21 bytes total)
#   DISCONNECT REQ/ACK  : header only (20 bytes total)
#   INFORMATION         : header + info payload (≤ 236 info bytes per fragment)
#   INFORMATION ACK     : header only; ack carried in byte-18 RX sequence

# Opcodes — low 3 bits of byte 19
OPCODE_CONNECT_REQ:     int = 0x01
OPCODE_CONNECT_ACK:     int = 0x02
OPCODE_DISCONNECT_REQ:  int = 0x03
OPCODE_DISCONNECT_ACK:  int = 0x04
OPCODE_INFORMATION:     int = 0x05
OPCODE_INFORMATION_ACK: int = 0x06

# Bit flags in byte 19 (bits 5–7 alongside the opcode in bits 0–2)
FLAG_CHOKE:        int = 0x80   # also = "connection refused" bit for CONNECT ACK
FLAG_NAK:          int = 0x40
FLAG_MORE_FOLLOWS: int = 0x20

L3_HEADER_LEN: int = 20
# Default L3 info MTU.  AX.25 PACLEN is the limiting factor in practice:
# NORCAL convention (KPC-3 / TheNet) is PACLEN=128, which leaves us 108
# bytes after the 20-byte L3 header.  Sending more makes Direwolf (or any
# AX.25 stack honoring PACLEN) split our L3 frame at L2, producing a
# header-less second AX.25 I-frame that the receiver's NETROM parser
# can't decode — visible as "chunks missing from the middle of a line"
# on the user's terminal.  Override at runtime via NetromCircuitManager.
L3_INFO_MTU:   int = 108


def _is_netrom_callsign_char(ch: int) -> bool:
    """ASCII char after right-shift: A-Z, 0-9, space, or NUL (lenient)."""
    return (
        ch == 0x00
        or ch == 0x20
        or (0x30 <= ch <= 0x39)
        or (0x41 <= ch <= 0x5A)
    )


@dataclass
class L3Header:
    """The 20-byte NETROM L3+L4 header (raw byte values)."""
    origin_call:  str
    dest_call:    str
    ttl:          int
    circuit_idx:  int
    circuit_id:   int
    tx_seq:       int
    rx_seq:       int
    opcode_flags: int

    @property
    def opcode(self) -> int:
        return self.opcode_flags & 0x07

    @property
    def choke(self) -> bool:
        return bool(self.opcode_flags & FLAG_CHOKE)

    @property
    def nak(self) -> bool:
        return bool(self.opcode_flags & FLAG_NAK)

    @property
    def more_follows(self) -> bool:
        return bool(self.opcode_flags & FLAG_MORE_FOLLOWS)

    @property
    def refused(self) -> bool:
        """True when this is a CONNECT ACK with the refusal bit set.

        The same bit-7 position carries CHOKE semantics for other opcodes,
        so callers must check the opcode before consulting this.
        """
        return (
            self.opcode == OPCODE_CONNECT_ACK
            and bool(self.opcode_flags & FLAG_CHOKE)
        )


# ── Decoded-frame variants (tagged union) ────────────────────────────────────

@dataclass
class ConnectRequest:
    header:           L3Header
    proposed_window:  int
    user_call:        str   # originating end-user callsign
    origin_node_call: str   # node the user entered the NETROM network through


@dataclass
class ConnectAck:
    header:          L3Header
    accepted_window: int    # 0 when refused
    refused:         bool


@dataclass
class Disconnect:
    """Carries both DISCONNECT REQUEST and DISCONNECT ACK — distinguish via
    `header.opcode`."""
    header: L3Header


@dataclass
class Information:
    header: L3Header
    info:   bytes


@dataclass
class InformationAck:
    header: L3Header


L3Frame = (
    ConnectRequest | ConnectAck | Disconnect | Information | InformationAck
)


# ── Header codec ──────────────────────────────────────────────────────────────

def encode_l3_header(h: L3Header) -> bytes:
    """Pack a 20-byte NETROM L3+L4 header.

    The EOA (end-of-address) bit on the destination callsign's SSID byte is
    set automatically per spec; the caller need not pre-flag it.
    """
    out = bytearray()
    out += encode_wire_call(h.origin_call)
    out += encode_wire_call(h.dest_call)
    out[13] |= 0x01  # EOA on destination (last address in the NETROM header)
    out.append(h.ttl          & 0xFF)
    out.append(h.circuit_idx  & 0xFF)
    out.append(h.circuit_id   & 0xFF)
    out.append(h.tx_seq       & 0xFF)
    out.append(h.rx_seq       & 0xFF)
    out.append(h.opcode_flags & 0xFF)
    return bytes(out)


def decode_l3_header(data: bytes) -> L3Header | None:
    """Decode the first 20 bytes of *data* as a NETROM header.

    Returns None when the slice is too short. Performs no opcode validation —
    use decode_l3_frame() for that.
    """
    if len(data) < L3_HEADER_LEN:
        return None
    return L3Header(
        origin_call  = decode_wire_call(data[0:7]),
        dest_call    = decode_wire_call(data[7:14]),
        ttl          = data[14],
        circuit_idx  = data[15],
        circuit_id   = data[16],
        tx_seq       = data[17],
        rx_seq       = data[18],
        opcode_flags = data[19],
    )


# ── Full-frame codec ──────────────────────────────────────────────────────────

def encode_l3_frame(header: L3Header, payload: bytes = b"") -> bytes:
    """Concatenate an encoded header with an opcode-specific payload."""
    return encode_l3_header(header) + payload


def encode_connect_request_tail(
    proposed_window: int,
    user_call:       str,
    origin_node:     str,
) -> bytes:
    """Build the 15-byte tail of a CONNECT REQUEST frame.

    Caller composes the full frame as
        encode_l3_frame(header_with_opcode_CONNECT_REQ, this_tail)
    """
    return (
        bytes([proposed_window & 0xFF])
        + encode_wire_call(user_call)
        + encode_wire_call(origin_node)
    )


def encode_connect_ack_tail(accepted_window: int) -> bytes:
    """Build the 1-byte tail of a CONNECT ACK.

    Refusal is encoded in the header's opcode_flags (set FLAG_CHOKE), not in
    the tail.
    """
    return bytes([accepted_window & 0xFF])


def decode_l3_frame(data: bytes) -> L3Frame | None:
    """Decode a complete NETROM L3+L4 frame.

    Returns one of ConnectRequest / ConnectAck / Disconnect / Information /
    InformationAck depending on the opcode, or None when the frame is
    truncated or carries an unknown opcode.
    """
    header = decode_l3_header(data)
    if header is None:
        return None
    rest = data[L3_HEADER_LEN:]
    op = header.opcode

    if op == OPCODE_CONNECT_REQ:
        if len(rest) < 1 + _ADDR_WIRE_LEN + _ADDR_WIRE_LEN:
            return None
        return ConnectRequest(
            header           = header,
            proposed_window  = rest[0],
            user_call        = decode_wire_call(rest[1:1 + _ADDR_WIRE_LEN]),
            origin_node_call = decode_wire_call(
                rest[1 + _ADDR_WIRE_LEN: 1 + 2 * _ADDR_WIRE_LEN]
            ),
        )

    if op == OPCODE_CONNECT_ACK:
        if len(rest) < 1:
            return None
        return ConnectAck(
            header          = header,
            accepted_window = rest[0],
            refused         = bool(header.opcode_flags & FLAG_CHOKE),
        )

    if op == OPCODE_INFORMATION:
        return Information(header=header, info=bytes(rest))

    if op == OPCODE_INFORMATION_ACK:
        return InformationAck(header=header)

    if op in (OPCODE_DISCONNECT_REQ, OPCODE_DISCONNECT_ACK):
        return Disconnect(header=header)

    return None


# ── Classifier: heuristic NETROM L3 detection ────────────────────────────────

def looks_like_netrom_l3(data: bytes) -> bool:
    """Return True if *data* matches the NETROM L3 header bit-pattern.

    Used by the AGWPE classifier to distinguish a NETROM crosslink CONNECT
    REQUEST from direct BBS user input on the first 'D' frame of a new
    session. Checks the strongest discriminators:

      • Two 6-byte callsign chunks, each char NETROM-encoded (ASCII A-Z,
        0-9, space, or NUL, shifted left by 1 — i.e. bit 0 = 0)
      • Both SSID bytes (positions 6 and 13) carry bits 7,6,5 = 0 (NETROM
        strips AX.25 reserved bits; EOA bit on byte 13 is allowed either
        way to tolerate non-conforming implementations)
      • TTL byte (14) in [1, 64]
      • Opcode (low 3 bits of byte 19) is a known NETROM opcode (1–6)

    False-positive probability from arbitrary user input is effectively
    zero — twelve specific alpha-num-space characters perfectly aligned to
    even bytes plus reserved-bit patterns on bytes 6 and 13 plus a valid
    opcode would all have to coincide.
    """
    if len(data) < L3_HEADER_LEN:
        return False
    # Origin callsign chars (bytes 0–5)
    for b in data[0:6]:
        if not _is_netrom_callsign_char((b >> 1) & 0x7F):
            return False
    # Origin SSID byte (byte 6): bits 7,6,5 must be 0; bit 0 EOA must be 0
    # (origin is never the last NETROM address in the header).
    if data[6] & 0xE1:
        return False
    # Destination callsign chars (bytes 7–12)
    for b in data[7:13]:
        if not _is_netrom_callsign_char((b >> 1) & 0x7F):
            return False
    # Destination SSID byte (byte 13): bits 7,6,5 must be 0; bit 0 may be
    # either (EOA SHOULD be set per spec, but be lenient).
    if data[13] & 0xE0:
        return False
    # TTL (byte 14): plausible bound
    if not (1 <= data[14] <= 64):
        return False
    # Opcode (low 3 bits of byte 19)
    if (data[19] & 0x07) not in (1, 2, 3, 4, 5, 6):
        return False
    return True
