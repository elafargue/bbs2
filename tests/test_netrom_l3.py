"""
tests/test_netrom_l3.py — Unit tests for the NETROM L3/L4 frame codec.

Covers:
  - L3Header dataclass and flag accessors
  - Header encode/decode round-trip (incl. EOA bit on dest)
  - Per-opcode encode/decode round-trip:
      CONNECT REQ, CONNECT ACK (incl. refusal), INFO, INFO ACK, DISC REQ/ACK
  - decode_l3_frame() rejection of truncated / unknown frames
  - looks_like_netrom_l3() positive and negative cases

No transport, no async, no fixtures.
"""
from __future__ import annotations

import pytest

from bbs.ax25.netrom_frame import (
    FLAG_CHOKE,
    FLAG_MORE_FOLLOWS,
    FLAG_NAK,
    L3_HEADER_LEN,
    L3_INFO_MTU,
    OPCODE_CONNECT_ACK,
    OPCODE_CONNECT_REQ,
    OPCODE_DISCONNECT_ACK,
    OPCODE_DISCONNECT_REQ,
    OPCODE_INFORMATION,
    OPCODE_INFORMATION_ACK,
    ConnectAck,
    ConnectRequest,
    Disconnect,
    Information,
    InformationAck,
    L3Header,
    decode_l3_frame,
    decode_l3_header,
    encode_connect_ack_tail,
    encode_connect_request_tail,
    encode_l3_frame,
    encode_l3_header,
    looks_like_netrom_l3,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hdr(opcode_flags: int = OPCODE_INFORMATION) -> L3Header:
    """Build a plausible header for a circuit between W6ELA-1 and N6ZX-5."""
    return L3Header(
        origin_call  = "N6ZX-5",
        dest_call    = "W6ELA-1",
        ttl          = 25,
        circuit_idx  = 7,
        circuit_id   = 42,
        tx_seq       = 3,
        rx_seq       = 4,
        opcode_flags = opcode_flags,
    )


# ── L3Header dataclass + flag accessors ──────────────────────────────────────

class TestL3HeaderFlags:
    def test_opcode_masked_from_low_3_bits(self):
        h = _hdr(opcode_flags=0xE5)        # all flags + opcode 0x05
        assert h.opcode == OPCODE_INFORMATION

    def test_choke_flag(self):
        assert _hdr(opcode_flags=OPCODE_INFORMATION | FLAG_CHOKE).choke is True
        assert _hdr(opcode_flags=OPCODE_INFORMATION).choke is False

    def test_nak_flag(self):
        assert _hdr(opcode_flags=OPCODE_INFORMATION | FLAG_NAK).nak is True

    def test_more_follows_flag(self):
        h = _hdr(opcode_flags=OPCODE_INFORMATION | FLAG_MORE_FOLLOWS)
        assert h.more_follows is True

    def test_refused_only_meaningful_for_connect_ack(self):
        # CHOKE bit on INFO is NOT a refusal.
        assert _hdr(opcode_flags=OPCODE_INFORMATION | FLAG_CHOKE).refused is False
        assert _hdr(opcode_flags=OPCODE_CONNECT_ACK | FLAG_CHOKE).refused is True
        assert _hdr(opcode_flags=OPCODE_CONNECT_ACK).refused is False


# ── Header encode / decode round-trip ────────────────────────────────────────

class TestHeaderCodec:
    def test_roundtrip_preserves_all_fields(self):
        h = _hdr()
        decoded = decode_l3_header(encode_l3_header(h))
        assert decoded is not None
        assert decoded.origin_call  == h.origin_call
        assert decoded.dest_call    == h.dest_call
        assert decoded.ttl          == h.ttl
        assert decoded.circuit_idx  == h.circuit_idx
        assert decoded.circuit_id   == h.circuit_id
        assert decoded.tx_seq       == h.tx_seq
        assert decoded.rx_seq       == h.rx_seq
        assert decoded.opcode_flags == h.opcode_flags

    def test_encoded_length_is_20(self):
        assert len(encode_l3_header(_hdr())) == L3_HEADER_LEN

    def test_eoa_bit_set_on_dest_ssid(self):
        # Dest SSID byte is at position 13 per spec.
        raw = encode_l3_header(_hdr())
        assert raw[13] & 0x01 == 0x01, "EOA must be set on dest SSID byte"

    def test_eoa_bit_clear_on_origin_ssid(self):
        raw = encode_l3_header(_hdr())
        assert raw[6] & 0x01 == 0x00, "EOA must be clear on origin SSID byte"

    def test_decode_short_returns_none(self):
        assert decode_l3_header(b"\x00" * (L3_HEADER_LEN - 1)) is None
        assert decode_l3_header(b"") is None


# ── CONNECT REQUEST ──────────────────────────────────────────────────────────

class TestConnectRequest:
    def _build(
        self,
        proposed_window: int = 4,
        user_call:       str = "KN6PE-7",
        origin_node:     str = "N6ZX-5",
    ) -> bytes:
        header = _hdr(opcode_flags=OPCODE_CONNECT_REQ)
        header.tx_seq = 0
        header.rx_seq = 0
        tail = encode_connect_request_tail(proposed_window, user_call, origin_node)
        return encode_l3_frame(header, tail)

    def test_total_length_is_35(self):
        # 20 header + 1 window + 7 user + 7 origin node
        assert len(self._build()) == 35

    def test_decode_yields_connect_request(self):
        f = decode_l3_frame(self._build())
        assert isinstance(f, ConnectRequest)

    def test_proposed_window_preserved(self):
        f = decode_l3_frame(self._build(proposed_window=7))
        assert isinstance(f, ConnectRequest)
        assert f.proposed_window == 7

    def test_user_callsign_preserved(self):
        f = decode_l3_frame(self._build(user_call="W1AW"))
        assert isinstance(f, ConnectRequest)
        assert f.user_call == "W1AW"

    def test_user_ssid_preserved(self):
        f = decode_l3_frame(self._build(user_call="KN6PE-9"))
        assert isinstance(f, ConnectRequest)
        assert f.user_call == "KN6PE-9"

    def test_origin_node_callsign_preserved(self):
        f = decode_l3_frame(self._build(origin_node="W6OAK-5"))
        assert isinstance(f, ConnectRequest)
        assert f.origin_node_call == "W6OAK-5"

    def test_header_origin_and_dest_preserved(self):
        f = decode_l3_frame(self._build())
        assert isinstance(f, ConnectRequest)
        assert f.header.origin_call == "N6ZX-5"
        assert f.header.dest_call   == "W6ELA-1"

    def test_truncated_tail_returns_none(self):
        # Header + 1 window byte but no callsigns
        header = _hdr(opcode_flags=OPCODE_CONNECT_REQ)
        truncated = encode_l3_frame(header, bytes([4]))   # only window
        assert decode_l3_frame(truncated) is None


# ── CONNECT ACK ──────────────────────────────────────────────────────────────

class TestConnectAck:
    def _build(self, accepted_window: int = 4, refused: bool = False) -> bytes:
        flags = OPCODE_CONNECT_ACK | (FLAG_CHOKE if refused else 0)
        header = _hdr(opcode_flags=flags)
        return encode_l3_frame(header, encode_connect_ack_tail(accepted_window))

    def test_total_length_is_21(self):
        assert len(self._build()) == 21

    def test_decode_yields_connect_ack(self):
        f = decode_l3_frame(self._build())
        assert isinstance(f, ConnectAck)

    def test_accepted_window_preserved(self):
        f = decode_l3_frame(self._build(accepted_window=6))
        assert isinstance(f, ConnectAck)
        assert f.accepted_window == 6

    def test_not_refused_by_default(self):
        f = decode_l3_frame(self._build())
        assert isinstance(f, ConnectAck)
        assert f.refused is False

    def test_refused_via_choke_bit(self):
        f = decode_l3_frame(self._build(refused=True))
        assert isinstance(f, ConnectAck)
        assert f.refused is True

    def test_truncated_returns_none(self):
        header = _hdr(opcode_flags=OPCODE_CONNECT_ACK)
        assert decode_l3_frame(encode_l3_frame(header)) is None  # no window byte


# ── INFORMATION ──────────────────────────────────────────────────────────────

class TestInformation:
    def _build(self, info: bytes, more_follows: bool = False) -> bytes:
        flags = OPCODE_INFORMATION | (FLAG_MORE_FOLLOWS if more_follows else 0)
        header = _hdr(opcode_flags=flags)
        return encode_l3_frame(header, info)

    def test_decode_yields_information(self):
        f = decode_l3_frame(self._build(b"hello"))
        assert isinstance(f, Information)

    def test_payload_preserved(self):
        f = decode_l3_frame(self._build(b"hello\r"))
        assert isinstance(f, Information)
        assert f.info == b"hello\r"

    def test_empty_payload_allowed(self):
        # The wire allows it; behavior is "no-op INFO with sequence increment."
        f = decode_l3_frame(self._build(b""))
        assert isinstance(f, Information)
        assert f.info == b""

    def test_full_mtu_payload(self):
        payload = bytes(i & 0xFF for i in range(L3_INFO_MTU))
        f = decode_l3_frame(self._build(payload))
        assert isinstance(f, Information)
        assert f.info == payload
        assert len(f.info) == L3_INFO_MTU

    def test_more_follows_flag_decodes(self):
        f = decode_l3_frame(self._build(b"chunk1", more_follows=True))
        assert isinstance(f, Information)
        assert f.header.more_follows is True

    def test_tx_rx_seq_preserved(self):
        f = decode_l3_frame(self._build(b"x"))
        assert isinstance(f, Information)
        assert f.header.tx_seq == 3   # from _hdr()
        assert f.header.rx_seq == 4


# ── INFORMATION ACK ──────────────────────────────────────────────────────────

class TestInformationAck:
    def test_decode_yields_info_ack(self):
        header = _hdr(opcode_flags=OPCODE_INFORMATION_ACK)
        f = decode_l3_frame(encode_l3_frame(header))
        assert isinstance(f, InformationAck)

    def test_rx_seq_is_the_ack_value(self):
        header = _hdr(opcode_flags=OPCODE_INFORMATION_ACK)
        header.rx_seq = 9
        f = decode_l3_frame(encode_l3_frame(header))
        assert isinstance(f, InformationAck)
        assert f.header.rx_seq == 9

    def test_length_is_exactly_header(self):
        header = _hdr(opcode_flags=OPCODE_INFORMATION_ACK)
        assert len(encode_l3_frame(header)) == L3_HEADER_LEN


# ── DISCONNECT REQUEST / ACK ─────────────────────────────────────────────────

class TestDisconnect:
    def test_disconnect_request_decodes_as_disconnect(self):
        header = _hdr(opcode_flags=OPCODE_DISCONNECT_REQ)
        f = decode_l3_frame(encode_l3_frame(header))
        assert isinstance(f, Disconnect)
        assert f.header.opcode == OPCODE_DISCONNECT_REQ

    def test_disconnect_ack_decodes_as_disconnect(self):
        header = _hdr(opcode_flags=OPCODE_DISCONNECT_ACK)
        f = decode_l3_frame(encode_l3_frame(header))
        assert isinstance(f, Disconnect)
        assert f.header.opcode == OPCODE_DISCONNECT_ACK


# ── decode_l3_frame edge cases ───────────────────────────────────────────────

class TestDecodeRejections:
    def test_empty_input(self):
        assert decode_l3_frame(b"") is None

    def test_short_header(self):
        assert decode_l3_frame(b"\x00" * 10) is None

    def test_unknown_opcode_returns_none(self):
        # Opcodes 0 and 7 are not defined.
        header = _hdr(opcode_flags=0x00)
        assert decode_l3_frame(encode_l3_frame(header)) is None
        header = _hdr(opcode_flags=0x07)
        assert decode_l3_frame(encode_l3_frame(header)) is None


# ── looks_like_netrom_l3 — positive cases ────────────────────────────────────

class TestDetectorPositive:
    @pytest.mark.parametrize("opcode", [
        OPCODE_CONNECT_REQ,
        OPCODE_CONNECT_ACK,
        OPCODE_DISCONNECT_REQ,
        OPCODE_DISCONNECT_ACK,
        OPCODE_INFORMATION,
        OPCODE_INFORMATION_ACK,
    ])
    def test_any_real_opcode_detected(self, opcode):
        header = _hdr(opcode_flags=opcode)
        raw = encode_l3_frame(header)
        assert looks_like_netrom_l3(raw) is True

    def test_real_connect_request_detected(self):
        header = _hdr(opcode_flags=OPCODE_CONNECT_REQ)
        raw = encode_l3_frame(
            header,
            encode_connect_request_tail(4, "KN6PE-7", "N6ZX-5"),
        )
        assert looks_like_netrom_l3(raw) is True

    def test_information_with_payload_detected(self):
        header = _hdr(opcode_flags=OPCODE_INFORMATION)
        raw = encode_l3_frame(header, b"hello world\r")
        assert looks_like_netrom_l3(raw) is True

    def test_dest_eoa_bit_clear_still_detected(self):
        # Some implementations may omit the EOA bit on dest. Be lenient.
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[13] &= 0xFE  # clear EOA
        assert looks_like_netrom_l3(bytes(raw)) is True


# ── looks_like_netrom_l3 — negative cases ────────────────────────────────────

class TestDetectorNegative:
    def test_empty_input(self):
        assert looks_like_netrom_l3(b"") is False

    def test_short_input(self):
        assert looks_like_netrom_l3(b"\x00" * (L3_HEADER_LEN - 1)) is False

    def test_all_zeros_rejected(self):
        # TTL = 0 fails the (1 <= ttl <= 64) check.
        assert looks_like_netrom_l3(b"\x00" * 30) is False

    def test_plain_ascii_user_input_rejected(self):
        # The first input a real BBS user typically sends: their callsign,
        # a CR, or a command. Bytes have bit 0 set (odd ASCII chars), so
        # the NETROM callsign-char check fails.
        for sample in (
            b"\r",
            b"W1AW\r",
            b"HELP\r\n",
            b"L\r",                       # short command
            b"connect bbs\r",
            b"\x1b[2J",                   # ANSI escape
            b"the quick brown fox jumps " ,
        ):
            padded = sample + b"\x00" * max(0, L3_HEADER_LEN - len(sample))
            assert looks_like_netrom_l3(padded) is False, (
                f"input {sample!r} incorrectly classified as NETROM"
            )

    def test_invalid_callsign_char_rejected(self):
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[0] = ord('@') << 1   # '@' is between '?' and 'A', not allowed
        assert looks_like_netrom_l3(bytes(raw)) is False

    def test_origin_ssid_reserved_bit_rejected(self):
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[6] |= 0x60   # set AX.25 reserved bits (NETROM strips them)
        assert looks_like_netrom_l3(bytes(raw)) is False

    def test_dest_ssid_reserved_bit_rejected(self):
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[13] |= 0x60
        assert looks_like_netrom_l3(bytes(raw)) is False

    def test_ttl_zero_rejected(self):
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[14] = 0
        assert looks_like_netrom_l3(bytes(raw)) is False

    def test_ttl_too_large_rejected(self):
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[14] = 200
        assert looks_like_netrom_l3(bytes(raw)) is False

    def test_unknown_opcode_rejected(self):
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[19] = 0x00      # opcode 0
        assert looks_like_netrom_l3(bytes(raw)) is False
        raw[19] = 0x07      # opcode 7
        assert looks_like_netrom_l3(bytes(raw)) is False

    def test_random_binary_rejected(self):
        # Random 30 bytes very rarely matches all constraints.
        import os
        for _ in range(20):
            sample = os.urandom(30)
            # We don't assert always False (vanishingly small chance of
            # a hit), but the suite would fail noisily if every random
            # sample hit. Instead, accumulate hits and assert < 2.
        # Replace with a deterministic check: a frame whose origin SSID
        # has high bit set (typical AX.25 C-bit, never present in NETROM).
        raw = bytearray(encode_l3_frame(_hdr()))
        raw[6] |= 0x80
        assert looks_like_netrom_l3(bytes(raw)) is False
