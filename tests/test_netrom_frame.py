"""
tests/test_netrom_frame.py — Unit tests for the NETROM frame codec.
"""
import pytest

from bbs.ax25.netrom_frame import (
    ConnectRequest,
    L3Header,
    NodeEntry,
    NodesFrame,
    OPCODE_CONNECT_REQ,
    PID_NETROM,
    _ALIAS_LEN,
    _NODE_ENTRY_LEN,
    _ROUTING_DISCRIMINATOR,
    decode_l3_frame,
    decode_nodes_broadcast,
    decode_wire_call,
    encode_connect_request_tail,
    encode_l3_frame,
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


# ── CONNECT REQ callsign validation ───────────────────────────────────────────

class TestConnectRequestValidation:
    """On an established crosslink every 'D' frame is decoded as NET/ROM L3, so
    a neighbor's plain-text notice can coincidentally decode to opcode CONNECT
    REQ and mint a phantom user.  A CONNECT REQ whose user/origin fields aren't
    valid callsigns must be rejected (returns None) rather than accepted."""

    def _connect_req(self, user: str, origin: str) -> bytes:
        hdr = L3Header(
            origin_call="N6ZX-5", dest_call="W6ELA-5", ttl=25,
            circuit_idx=11, circuit_id=22, tx_seq=0, rx_seq=0,
            opcode_flags=OPCODE_CONNECT_REQ,
        )
        return encode_l3_frame(hdr, encode_connect_request_tail(4, user, origin))

    def test_real_connect_req_decodes(self):
        f = decode_l3_frame(self._connect_req("KK6FPP-7", "N6ZX-5"))
        assert isinstance(f, ConnectRequest)
        assert f.user_call == "KK6FPP-7" and f.origin_node_call == "N6ZX-5"

    def test_short_and_padded_callsign_decodes(self):
        f = decode_l3_frame(self._connect_req("W6P-1", "N6ZX"))
        assert isinstance(f, ConnectRequest)
        assert f.user_call == "W6P-1" and f.origin_node_call == "N6ZX"

    def test_uronode_inactivity_text_rejected(self):
        """The exact URONode text frame that produced the phantom '"49177-7'
        user in production — byte 19 is '!' (opcode CONNECT REQ) and the body
        decodes to junk callsigns.  It must be dropped."""
        txt = (b"\x0dInactivity timeout! Disconnecting you... "
               b"\x0dW6ELA-5 de K2YE-5\x0d73! ")
        assert txt[19] == ord("!")            # decodes to opcode CONNECT REQ
        assert decode_l3_frame(txt) is None

    def test_connect_req_with_punctuation_user_rejected(self):
        # A user field containing a non-callsign char (':') must be rejected.
        assert decode_l3_frame(self._connect_req("AB:CDE", "N6ZX-5")) is None

    def test_connect_req_with_bad_origin_rejected(self):
        assert decode_l3_frame(self._connect_req("KK6FPP-7", "A\"BCDE")) is None
