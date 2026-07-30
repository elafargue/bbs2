"""
tests/test_netrom_frame.py — Unit tests for the NETROM frame codec.
"""
import pytest

from bbs.ax25.netrom_frame import (
    NodeEntry,
    NodesFrame,
    PID_NETROM,
    _ALIAS_LEN,
    _NODE_ENTRY_LEN,
    _ROUTING_DISCRIMINATOR,
    decode_nodes_broadcast,
    decode_wire_call,
    encode_nodes_broadcast,
    encode_wire_call,
)


# ── Wire-format callsign codec ────────────────────────────────────────────────

class TestWireCall:
    def test_decode_no_ssid(self):
        # 'N', '6', 'Z', 'X', ' ', ' ' each shifted left 1; SSID byte = 0
        raw = bytes([
            ord('N') << 1, ord('6') << 1, ord('Z') << 1,
            ord('X') << 1, 0x40, 0x40,  # two space-padding bytes
            0x00,                         # SSID = 0
        ])
        assert decode_wire_call(raw) == "N6ZX"

    def test_decode_with_ssid(self):
        raw = bytes([
            ord('N') << 1, ord('6') << 1, ord('Z') << 1,
            ord('X') << 1, 0x40, 0x40,
            (5 << 1),   # SSID = 5
        ])
        assert decode_wire_call(raw) == "N6ZX-5"

    def test_decode_six_char_call(self):
        raw = bytes([
            ord('K') << 1, ord('I') << 1, ord('6') << 1,
            ord('Z') << 1, ord('H') << 1, ord('D') << 1,
            0x00,
        ])
        assert decode_wire_call(raw) == "KI6ZHD"

    def test_encode_no_ssid(self):
        raw = encode_wire_call("N6ZX")
        assert len(raw) == 7
        assert decode_wire_call(raw) == "N6ZX"

    def test_encode_with_ssid(self):
        raw = encode_wire_call("N6ZX-5")
        assert len(raw) == 7
        assert decode_wire_call(raw) == "N6ZX-5"

    def test_roundtrip_max_ssid(self):
        assert decode_wire_call(encode_wire_call("W1AW-15")) == "W1AW-15"

    def test_roundtrip_six_char(self):
        assert decode_wire_call(encode_wire_call("KI6ZHD-7")) == "KI6ZHD-7"

    def test_encode_uppercase(self):
        assert decode_wire_call(encode_wire_call("n6zx-5")) == "N6ZX-5"


# ── NODES broadcast codec ─────────────────────────────────────────────────────

def _make_nodes_payload(alias: str, entries: list[NodeEntry]) -> bytes:
    """Build a minimal NODES payload directly (independent of the encoder)."""
    out = bytearray([_ROUTING_DISCRIMINATOR])
    out += alias.upper()[:6].ljust(6).encode("ascii")
    for e in entries:
        out += encode_wire_call(e.dest_call)
        out += e.alias.upper()[:6].ljust(6).encode("ascii")
        out += encode_wire_call(e.neighbor_call)
        out.append(e.quality & 0xFF)
    return bytes(out)


class TestNodesFrame:
    def test_decode_single_entry(self):
        entry = NodeEntry("N6ZX-5", "WBAY", "K6FB-5", 192)
        payload = _make_nodes_payload("PALO", [entry])
        frame = decode_nodes_broadcast("W6ELA-1", payload)

        assert frame is not None
        assert frame.source_call == "W6ELA-1"
        assert frame.source_alias == "PALO"
        assert len(frame.entries) == 1

        e = frame.entries[0]
        assert e.dest_call == "N6ZX-5"
        assert e.alias == "WBAY"
        assert e.neighbor_call == "K6FB-5"
        assert e.quality == 192

    def test_decode_multiple_entries(self):
        entries = [
            NodeEntry("N6ZX-5",   "WBAY",   "K6FB-5",  200),
            NodeEntry("K2YE-5",   "COOL",   "N6ZX-5",  180),
            NodeEntry("KI6ZHD-5", "SKUNK",  "N6ZX-5",  160),
        ]
        payload = _make_nodes_payload("PALO", entries)
        frame = decode_nodes_broadcast("W6ELA-1", payload)

        assert frame is not None
        assert len(frame.entries) == 3
        assert frame.entries[0].alias == "WBAY"
        assert frame.entries[1].alias == "COOL"
        assert frame.entries[2].alias == "SKUNK"

    def test_decode_alias_stripped(self):
        # Aliases shorter than 6 chars are space-padded in wire format
        entry = NodeEntry("K2YE-5", "HI", "N6ZX-5", 128)
        payload = _make_nodes_payload("PALO", [entry])
        frame = decode_nodes_broadcast("W6ELA-1", payload)
        assert frame is not None
        assert frame.entries[0].alias == "HI"   # trailing spaces stripped

    def test_decode_quality_extremes(self):
        for q in (0, 1, 127, 254, 255):
            entry = NodeEntry("W1AW-1", "TEST", "W1AW-2", q)
            payload = _make_nodes_payload("PALO", [entry])
            frame = decode_nodes_broadcast("W6ELA-1", payload)
            assert frame is not None
            assert frame.entries[0].quality == q

    def test_decode_too_short_returns_none(self):
        assert decode_nodes_broadcast("W6ELA-1", b"") is None
        assert decode_nodes_broadcast("W6ELA-1", b"\xFF\x00") is None

    def test_decode_wrong_discriminator_returns_none(self):
        payload = b"\xF0" + b"PALO  " + b"\x00" * _NODE_ENTRY_LEN
        assert decode_nodes_broadcast("W6ELA-1", payload) is None

    def test_decode_partial_entry_ignored(self):
        # Payload has header + 1 full entry + 10 extra bytes (< 21) — ignored
        entry = NodeEntry("N6ZX-5", "WBAY", "K6FB-5", 200)
        payload = _make_nodes_payload("PALO", [entry]) + b"\x00" * 10
        frame = decode_nodes_broadcast("W6ELA-1", payload)
        assert frame is not None
        assert len(frame.entries) == 1   # partial entry silently dropped

    def test_encode_decode_roundtrip(self):
        entries = [
            NodeEntry("K6FB-5",  "SKUNK",  "N6ZX-5",  200),
            NodeEntry("W6OAK-5", "OAK",    "K6FB-5",  150),
        ]
        payload = encode_nodes_broadcast("PALO", entries)
        frame = decode_nodes_broadcast("W6ELA-1", payload)

        assert frame is not None
        assert frame.source_alias == "PALO"
        assert len(frame.entries) == len(entries)
        for orig, decoded in zip(entries, frame.entries):
            assert decoded.dest_call    == orig.dest_call
            assert decoded.alias        == orig.alias
            assert decoded.neighbor_call == orig.neighbor_call
            assert decoded.quality      == orig.quality

    def test_encode_empty_entries(self):
        payload = encode_nodes_broadcast("PALO", [])
        assert len(payload) == 1 + _ALIAS_LEN   # discriminator + alias only
        frame = decode_nodes_broadcast("W6ELA-1", payload)
        assert frame is not None
        assert frame.entries == []

    def test_real_network_size(self):
        # Simulate a 6-entry broadcast (133 bytes = 1 + 6 + 6*21)
        entries = [
            NodeEntry("N6ZX-5",   "WBAY",   "N6ZX-5",   255),
            NodeEntry("K6FB-5",   "SKUNK",  "N6ZX-5",   200),
            NodeEntry("W6OAK-5",  "OAK",    "N6ZX-5",   180),
            NodeEntry("K2YE-5",   "COOL",   "N6ZX-5",   170),
            NodeEntry("KI6ZHD-5", "BETHEL", "N6ZX-5",   160),
            NodeEntry("K6BER-5",  "BRKNRG", "N6ZX-5",   150),
        ]
        payload = encode_nodes_broadcast("WBAY", entries)
        assert len(payload) == 133
        frame = decode_nodes_broadcast("N6ZX-5", payload)
        assert frame is not None
        assert len(frame.entries) == 6
