"""
tests/test_kiss_codec.py — Unit tests for the AX.25/KISS frame codec.

These tests cover:
  - address.py: parse / format_addr / callsign_only
  - kiss_frame.py: escape/unescape, build, decode, split
  - transport/kiss.py: _build_ax25_ui_frame round-trip through decode_frame
  - Beacon frame construction with configurable dest and via path

No network, no async, no fixtures required.
"""
from __future__ import annotations

import pytest

from bbs.ax25.address import callsign_only, format_addr, parse
from bbs.ax25.kiss_frame import (
    FEND,
    FESC,
    TFEND,
    TFESC,
    PID_NO_LAYER3,
    build_kiss_frame,
    decode_frame,
    kiss_escape,
    kiss_unescape,
    split_kiss_frames,
)
from bbs.transport.kiss import _build_ax25_ui_frame


# ---------------------------------------------------------------------------
# address.py
# ---------------------------------------------------------------------------

class TestAddressParse:
    def test_simple_callsign(self):
        call, ssid = parse("W1AW")
        assert call == "W1AW"
        assert ssid == 0

    def test_callsign_with_ssid(self):
        call, ssid = parse("W6ELA-8")
        assert call == "W6ELA"
        assert ssid == 8

    def test_ssid_zero_explicit(self):
        call, ssid = parse("N0CALL-0")
        assert ssid == 0

    def test_ssid_max(self):
        call, ssid = parse("AA6WK-15")
        assert ssid == 15

    def test_lowercase_normalized(self):
        call, ssid = parse("w1aw-3")
        assert call == "W1AW"
        assert ssid == 3

    def test_invalid_ssid_too_large(self):
        with pytest.raises(ValueError):
            parse("W1AW-16")

    def test_invalid_callsign_chars(self):
        with pytest.raises(ValueError):
            parse("W1AW!")

    def test_empty_string(self):
        with pytest.raises(ValueError):
            parse("")


class TestAddressFormat:
    def test_no_ssid(self):
        assert format_addr("W1AW", 0) == "W1AW"

    def test_with_ssid(self):
        assert format_addr("W6ELA", 8) == "W6ELA-8"

    def test_ssid_zero_omitted(self):
        assert "-" not in format_addr("KG6ABC", 0)

    def test_invalid_ssid(self):
        with pytest.raises(ValueError):
            format_addr("W1AW", 16)


class TestCallsignOnly:
    def test_strips_ssid(self):
        assert callsign_only("W6ELA-8") == "W6ELA"

    def test_no_ssid_unchanged(self):
        assert callsign_only("W1AW") == "W1AW"


# ---------------------------------------------------------------------------
# kiss_frame.py — escape / unescape
# ---------------------------------------------------------------------------

class TestKISSEscape:
    def test_plain_bytes_unchanged(self):
        data = b"Hello, World!"
        assert kiss_escape(data) == data

    def test_fend_escaped(self):
        data = bytes([FEND])
        escaped = kiss_escape(data)
        assert escaped == bytes([FESC, TFEND])

    def test_fesc_escaped(self):
        data = bytes([FESC])
        escaped = kiss_escape(data)
        assert escaped == bytes([FESC, TFESC])

    def test_mixed_escape(self):
        data = bytes([0x41, FEND, 0x42, FESC, 0x43])
        escaped = kiss_escape(data)
        assert FEND not in escaped
        assert escaped == bytes([0x41, FESC, TFEND, 0x42, FESC, TFESC, 0x43])

    def test_roundtrip(self):
        data = bytes([0x00, FEND, FESC, 0xFF, FESC, FEND])
        assert kiss_unescape(kiss_escape(data)) == data


class TestKISSUnescape:
    def test_plain_bytes_unchanged(self):
        data = b"no special bytes"
        assert kiss_unescape(data) == data

    def test_fend_unescaped(self):
        assert kiss_unescape(bytes([FESC, TFEND])) == bytes([FEND])

    def test_fesc_unescaped(self):
        assert kiss_unescape(bytes([FESC, TFESC])) == bytes([FESC])

    def test_truncated_fesc_ignored(self):
        # FESC at end with no following byte — should not crash
        result = kiss_unescape(bytes([FESC]))
        assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# kiss_frame.py — build_kiss_frame / decode_frame round-trip
# ---------------------------------------------------------------------------

class TestBuildDecodeRoundtrip:
    """
    Build a raw AX.25 UI frame with _build_ax25_ui_frame (from transport/kiss.py),
    wrap it in KISS framing, then decode it back with decode_frame.
    """

    def _make(self, src: str, dest: str, payload: bytes, port: int = 0) -> bytes:
        ax25 = _build_ax25_ui_frame(src, dest, payload)
        return build_kiss_frame(port, ax25)

    def _decode(self, kiss_bytes: bytes):
        # strip the outer FEND delimiters that build_kiss_frame adds
        inner = kiss_bytes[1:-1]
        return decode_frame(inner)

    def test_src_callsign_preserved(self):
        frame = self._decode(self._make("W6ELA-8", "W1BBS", b"hello"))
        assert frame is not None
        assert frame.src_call == "W6ELA-8"

    def test_dest_callsign_preserved(self):
        frame = self._decode(self._make("W6ELA", "W1BBS-0", b"hello"))
        assert frame is not None
        assert frame.dest_call == "W1BBS"

    def test_payload_preserved(self):
        payload = b"This is the BBS payload\r\n"
        frame = self._decode(self._make("KN6PE", "W1BBS", payload))
        assert frame is not None
        assert frame.payload == payload

    def test_empty_payload(self):
        frame = self._decode(self._make("KN6PE", "W1BBS", b""))
        assert frame is not None
        assert frame.payload == b""

    def test_binary_payload(self):
        payload = bytes(range(256))
        frame = self._decode(self._make("KN6PE", "W1BBS", payload))
        assert frame is not None
        assert frame.payload == payload

    def test_is_ui_frame(self):
        frame = self._decode(self._make("W1AW", "W1BBS", b"test"))
        assert frame is not None
        assert frame.is_ui is True

    def test_pid_no_layer3(self):
        frame = self._decode(self._make("W1AW", "W1BBS", b"test"))
        assert frame is not None
        assert frame.pid == PID_NO_LAYER3

    def test_kiss_port_preserved(self):
        kiss = self._make("W1AW", "W1BBS", b"test", port=2)
        frame = self._decode(kiss)
        assert frame is not None
        assert frame.port == 2

    def test_payload_containing_fend_survives_escaping(self):
        # payload with FEND bytes — must survive KISS escape/unescape
        payload = bytes([0xC0, 0xDB, 0xC0])  # FEND FESC FEND
        frame = self._decode(self._make("W1AW", "W1BBS", payload))
        assert frame is not None
        assert frame.payload == payload

    def test_ssid_zero_roundtrip(self):
        frame = self._decode(self._make("W1AW-0", "W1BBS-0", b"x"))
        assert frame is not None
        assert frame.src_call == "W1AW"   # SSID 0 omitted by format_addr
        assert frame.dest_call == "W1BBS"

    def test_ssid_nonzero_roundtrip(self):
        frame = self._decode(self._make("W6ELA-12", "W1BBS-3", b"x"))
        assert frame is not None
        assert frame.src_call == "W6ELA-12"
        assert frame.dest_call == "W1BBS-3"


# ---------------------------------------------------------------------------
# kiss_frame.py — decode_frame edge / rejection cases
# ---------------------------------------------------------------------------

class TestDecodeFrameRejection:
    def test_empty_returns_none(self):
        assert decode_frame(b"") is None

    def test_too_short_ax25_returns_none(self):
        # command byte for port 0, then 10 bytes (minimum is 15 for addresses + control)
        raw = bytes([0x00]) + b"\x00" * 10
        assert decode_frame(raw) is None

    def test_non_data_frame_type_returns_none(self):
        # frame_type != 0 → ignored
        raw = bytes([0x01]) + b"\x00" * 20  # type=1 (TX delay)
        assert decode_frame(raw) is None

    def test_malformed_in_unescape_does_not_crash(self):
        # lone FESC at end of frame, inside an otherwise valid-length frame
        # build a valid frame and corrupt one byte to FESC
        ax25 = _build_ax25_ui_frame("W1AW", "W1BBS", b"hi")
        corrupted = bytearray(ax25)
        corrupted[-1] = FESC  # truncated escape sequence in payload
        raw = bytes([0x00]) + bytes(corrupted)
        result = decode_frame(raw)
        # May be None or a frame with odd payload — must not raise
        assert result is None or isinstance(result.payload, bytes)


# ---------------------------------------------------------------------------
# kiss_frame.py — split_kiss_frames
# ---------------------------------------------------------------------------

class TestSplitKissFrames:
    def _frame(self, payload: bytes = b"data") -> bytes:
        ax25 = _build_ax25_ui_frame("W1AW", "W1BBS", payload)
        return build_kiss_frame(0, ax25)

    def test_single_frame(self):
        buf = bytearray(self._frame(b"hello"))
        frames, remainder = split_kiss_frames(buf)
        assert len(frames) == 1
        assert remainder == bytearray()

    def test_two_consecutive_frames(self):
        buf = bytearray(self._frame(b"first") + self._frame(b"second"))
        frames, remainder = split_kiss_frames(buf)
        assert len(frames) == 2

    def test_partial_frame_left_in_remainder(self):
        complete = self._frame(b"whole")
        partial = complete[:10]  # cut off mid-frame
        buf = bytearray(complete + partial)
        frames, remainder = split_kiss_frames(buf)
        assert len(frames) == 1
        assert len(remainder) > 0

    def test_empty_buffer(self):
        frames, remainder = split_kiss_frames(bytearray())
        assert frames == []
        assert remainder == bytearray()

    def test_consecutive_fend_ignored(self):
        # Two FENDs in a row form an empty frame — should be skipped
        buf = bytearray([FEND, FEND]) + bytearray(self._frame(b"real"))
        frames, remainder = split_kiss_frames(buf)
        # Only the real frame should be decoded (empty frames between FENDs skipped)
        assert len(frames) >= 1

    def test_frames_decode_correctly_after_split(self):
        payloads = [b"msg one", b"msg two", b"msg three"]
        buf = bytearray()
        for p in payloads:
            buf += self._frame(p)
        frames, _ = split_kiss_frames(buf)
        assert len(frames) == 3
        decoded_payloads = []
        for raw in frames:
            f = decode_frame(raw)
            assert f is not None
            decoded_payloads.append(f.payload)
        assert decoded_payloads == payloads


# ---------------------------------------------------------------------------
# Beacon frame construction (sent by _KISSBaseTransport)
# ---------------------------------------------------------------------------

class TestBeaconFrame:
    """
    Verify that a beacon built the same way _send_beacon() does it decodes
    correctly: destination matches the configured beacon_dest, source is the
    BBS callsign, payload is the configured text.
    """

    BBS_CALL = "W6ELA-8"
    BEACON_TEXT = "W6ELA-8 BBS - Ed's BBS Palo Alto CA"

    def _build_beacon(self, dest: str = "BEACON") -> bytes:
        ax25 = _build_ax25_ui_frame(
            self.BBS_CALL, dest, self.BEACON_TEXT.encode("ascii")
        )
        return build_kiss_frame(0, ax25)

    def test_beacon_decodes_without_error(self):
        kiss = self._build_beacon()
        frame = decode_frame(kiss[1:-1])
        assert frame is not None

    def test_beacon_src_is_bbs(self):
        kiss = self._build_beacon()
        frame = decode_frame(kiss[1:-1])
        assert frame.src_call == self.BBS_CALL

    def test_beacon_dest_default_is_beacon(self):
        kiss = self._build_beacon()
        frame = decode_frame(kiss[1:-1])
        assert frame.dest_call == "BEACON"

    def test_beacon_dest_can_be_qst(self):
        kiss = self._build_beacon(dest="QST")
        frame = decode_frame(kiss[1:-1])
        assert frame.dest_call == "QST"

    def test_beacon_dest_can_be_kanode_id(self):
        # Ka-Node-style advertisement: dest "ID", payload "FOLSM/N,FOLSM/B,FOLSM/C"
        kiss = self._build_beacon(dest="ID")
        frame = decode_frame(kiss[1:-1])
        assert frame.dest_call == "ID"

    def test_beacon_payload_matches_text(self):
        kiss = self._build_beacon()
        frame = decode_frame(kiss[1:-1])
        assert frame.payload == self.BEACON_TEXT.encode("ascii")

    def test_beacon_is_ui_frame(self):
        kiss = self._build_beacon()
        frame = decode_frame(kiss[1:-1])
        assert frame.is_ui is True

    def test_beacon_interval_minimum_is_1_minute(self):
        # _KISSBaseTransport clamps interval to max(1, value) minutes
        cfg = {"beacon_text": "test", "beacon_interval": 0}
        from bbs.transport.kiss import KISSTCPTransport
        t = KISSTCPTransport(cfg, "W1BBS")
        assert t._beacon_interval >= 60  # at least 1 minute converted to seconds

    def test_no_beacon_text_disables_beacon(self):
        cfg = {}  # no beacon_text key
        from bbs.transport.kiss import KISSTCPTransport
        t = KISSTCPTransport(cfg, "W1BBS")
        assert t._beacon_text == ""


# ---------------------------------------------------------------------------
# Digipeater (via) path in UI frames
# ---------------------------------------------------------------------------

class TestUIFrameVia:
    """
    Verify that _build_ax25_ui_frame() correctly emits the digipeater address
    chain when `via` is supplied, and that decode_frame() round-trips it.
    """

    def _roundtrip(self, src: str, dest: str, payload: bytes, via: list[str] | None):
        ax25 = _build_ax25_ui_frame(src, dest, payload, via=via)
        kiss = build_kiss_frame(0, ax25)
        return decode_frame(kiss[1:-1])

    def test_no_via_defaults_to_empty_list(self):
        frame = self._roundtrip("W1AW", "BEACON", b"hi", via=None)
        assert frame is not None
        assert frame.via == []

    def test_empty_via_list_emits_no_digis(self):
        frame = self._roundtrip("W1AW", "BEACON", b"hi", via=[])
        assert frame is not None
        assert frame.via == []

    def test_single_via_roundtrips(self):
        frame = self._roundtrip("W1AW", "BEACON", b"hi", via=["WIDE1-1"])
        assert frame is not None
        assert frame.via == ["WIDE1-1"]

    def test_multiple_vias_preserve_order(self):
        frame = self._roundtrip(
            "W1AW", "BEACON", b"hi", via=["WIDE1-1", "WIDE2-1"]
        )
        assert frame is not None
        assert frame.via == ["WIDE1-1", "WIDE2-1"]

    def test_via_h_bit_clear_on_originated_frame(self):
        # When WE originate the frame, no digi has repeated it yet — decoder
        # marks repeated digis with a trailing "*"; ours must be unmarked.
        frame = self._roundtrip(
            "W1AW", "BEACON", b"hi", via=["KD6XYZ-3", "W6ABC"]
        )
        assert frame is not None
        assert all("*" not in v for v in frame.via)

    def test_via_lowercases_normalized(self):
        # parse() upper-cases callsigns; vias should follow suit on the wire.
        frame = self._roundtrip("W1AW", "BEACON", b"hi", via=["wide1-1"])
        assert frame is not None
        assert frame.via == ["WIDE1-1"]

    def test_payload_preserved_with_via(self):
        payload = b"under digi path"
        frame = self._roundtrip(
            "W1AW", "BEACON", payload, via=["WIDE1-1", "WIDE2-1"]
        )
        assert frame is not None
        assert frame.payload == payload


# ---------------------------------------------------------------------------
# AX.25 v2.0 command/response and digipeater H-bit encoding on the wire
# ---------------------------------------------------------------------------

class TestCRandHBits:
    """
    The decoder strips bit 7 of each SSID byte when extracting the callsign
    and SSID, so we inspect the raw AX.25 bytes directly to confirm:
      - dest SSID bit 7 = 1 (C-bit set: v2 command frame)
      - src  SSID bit 7 = 0 (C-bit clear: v2 command frame)
      - every via SSID bit 7 = 0 (H-bit clear: not yet repeated)
    """

    def test_dest_c_bit_set_on_originated_ui(self):
        ax25 = _build_ax25_ui_frame("W1AW", "BEACON", b"hi")
        dest_ssid_byte = ax25[6]
        assert dest_ssid_byte & 0x80 == 0x80

    def test_src_c_bit_clear_on_originated_ui(self):
        ax25 = _build_ax25_ui_frame("W1AW", "BEACON", b"hi")
        src_ssid_byte = ax25[13]
        assert src_ssid_byte & 0x80 == 0x00

    def test_via_h_bit_clear_in_raw_frame(self):
        ax25 = _build_ax25_ui_frame(
            "W1AW", "BEACON", b"hi", via=["WIDE1-1", "WIDE2-1"]
        )
        # dest=0-6, src=7-13, via1=14-20, via2=21-27
        assert ax25[20] & 0x80 == 0x00
        assert ax25[27] & 0x80 == 0x00

    def test_reserved_bits_set(self):
        # AX.25 spec requires SSID bits 6 and 5 to be 1 on every address byte.
        ax25 = _build_ax25_ui_frame(
            "W1AW", "BEACON", b"hi", via=["WIDE1-1"]
        )
        for ssid_idx in (6, 13, 20):
            assert ax25[ssid_idx] & 0x60 == 0x60, (
                f"reserved bits 6,5 must be set at byte {ssid_idx}"
            )

    def test_end_of_address_bit_on_last_via_only(self):
        ax25 = _build_ax25_ui_frame(
            "W1AW", "BEACON", b"hi", via=["WIDE1-1", "WIDE2-1"]
        )
        # src (byte 13) and via1 (byte 20) must NOT be end; via2 (byte 27) MUST.
        assert ax25[13] & 0x01 == 0x00
        assert ax25[20] & 0x01 == 0x00
        assert ax25[27] & 0x01 == 0x01

    def test_end_of_address_bit_on_src_when_no_via(self):
        ax25 = _build_ax25_ui_frame("W1AW", "BEACON", b"hi")
        # dest not end, src is end.
        assert ax25[6] & 0x01 == 0x00
        assert ax25[13] & 0x01 == 0x01


# ---------------------------------------------------------------------------
# Transport config parsing for beacon_dest and beacon_path
# ---------------------------------------------------------------------------

class TestBeaconConfigParsing:
    """
    Verify _KISSBaseTransport correctly reads beacon_dest and beacon_path
    from the config dict and applies sensible defaults.
    """

    def _make(self, **cfg):
        from bbs.transport.kiss import KISSTCPTransport
        return KISSTCPTransport(cfg, "W1BBS")

    def test_default_beacon_dest_is_beacon(self):
        t = self._make()
        assert t._beacon_dest == "BEACON"

    def test_beacon_dest_uppercased(self):
        t = self._make(beacon_dest="id")
        assert t._beacon_dest == "ID"

    def test_beacon_dest_empty_string_falls_back_to_beacon(self):
        t = self._make(beacon_dest="   ")
        assert t._beacon_dest == "BEACON"

    def test_default_beacon_path_is_empty(self):
        t = self._make()
        assert t._beacon_path == []

    def test_beacon_path_csv_parsed(self):
        t = self._make(beacon_path="wide1-1, wide2-1")
        assert t._beacon_path == ["WIDE1-1", "WIDE2-1"]

    def test_beacon_path_empty_entries_skipped(self):
        t = self._make(beacon_path=" , ,WIDE1-1, ,")
        assert t._beacon_path == ["WIDE1-1"]

    def test_beacon_path_uppercased(self):
        t = self._make(beacon_path="kd6xyz-3,w6abc")
        assert t._beacon_path == ["KD6XYZ-3", "W6ABC"]
