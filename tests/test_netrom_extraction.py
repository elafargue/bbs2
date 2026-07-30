"""
tests/test_netrom_extraction.py — Verify that _extract_binary_info correctly
recovers the raw binary AX.25 info field from an AGWPE 'U' monitor frame.

AGWPE 'U' payloads are TNC2-format strings where the ASCII header ends with
">[HH:MM:SS]\\r" and the binary NETROM payload follows immediately.  The
challenge is that this regex split must work on raw bytes before any
ASCII decoding mangles the binary data.
"""
import pytest

from bbs.ax25.netrom_frame import (
    NodeEntry,
    decode_nodes_broadcast,
    encode_nodes_broadcast,
    encode_wire_call,
)
from bbs.transport.agwpe import _extract_binary_info


def _agwpe_monitor_frame(src: str, dest: str, binary_info: bytes) -> bytes:
    """
    Build a realistic AGWPE 'U' monitor frame payload as Direwolf sends it.

    Format: " 1:Fm <src> To <dest> <UI pid=CF Len=N P=0 >[HH:MM:SS]\\r<info>\\r\\r\\x00"
    """
    header = (
        f" 1:Fm {src} To {dest} "
        f"<UI pid=CF Len={len(binary_info)} P=0 >"
        f"[15:14:21]\r"
    ).encode("ascii")
    return header + binary_info + b"\r\r\x00"


class TestExtractBinaryInfo:
    def test_extracts_single_byte_payload(self):
        raw = _agwpe_monitor_frame("N6ZX-5", "NODES", b"\xff")
        result = _extract_binary_info(raw)
        assert result == b"\xff"

    def test_extracts_netrom_nodes_payload(self):
        # Build a real 1-entry NODES payload and round-trip it through
        # the AGWPE frame wrapper and binary extractor.
        entries = [NodeEntry("N6ZX-5", "WBAY", "K6FB-5", 200)]
        nodes_payload = encode_nodes_broadcast("PALO", entries)

        raw = _agwpe_monitor_frame("W6ELA-1", "NODES", nodes_payload)
        extracted = _extract_binary_info(raw)

        assert extracted == nodes_payload

    def test_extracted_payload_decodes_correctly(self):
        entries = [
            NodeEntry("N6ZX-5",   "WBAY",   "K6FB-5",  200),
            NodeEntry("K2YE-5",   "COOL",   "N6ZX-5",  180),
            NodeEntry("KI6ZHD-5", "BETHEL", "N6ZX-5",  160),
        ]
        nodes_payload = encode_nodes_broadcast("WBAY", entries)
        raw = _agwpe_monitor_frame("N6ZX-5", "NODES", nodes_payload)

        extracted = _extract_binary_info(raw)
        assert extracted is not None

        frame = decode_nodes_broadcast("N6ZX-5", extracted)
        assert frame is not None
        assert frame.source_alias == "WBAY"
        assert len(frame.entries) == 3
        assert frame.entries[0].dest_call == "N6ZX-5"
        assert frame.entries[1].alias     == "COOL"
        assert frame.entries[2].quality   == 160

    def test_binary_survives_high_bytes(self):
        # Wire-encoded callsigns have many bytes > 0x7F (e.g. 'N'<<1 = 0x9C).
        # These must pass through the extractor unchanged — no ASCII mangling.
        raw_call = encode_wire_call("N6ZX-5")   # contains bytes > 0x7F
        assert any(b > 0x7F for b in raw_call), "test requires high bytes"

        payload = b"\xff" + b"WBAY  " + raw_call + b"COOL  " + raw_call + bytes([200])
        raw = _agwpe_monitor_frame("N6ZX-5", "NODES", payload)

        extracted = _extract_binary_info(raw)
        assert extracted == payload

    def test_trailing_garbage_stripped(self):
        payload = b"\xff" + b"PALO  "
        raw = _agwpe_monitor_frame("W6ELA-1", "NODES", payload)
        extracted = _extract_binary_info(raw)
        # Trailing \r\r\x00 must be stripped
        assert extracted is not None
        assert not extracted.endswith(b"\r")
        assert not extracted.endswith(b"\x00")

    def test_no_timestamp_returns_none(self):
        # Malformed frame with no >[HH:MM:SS] marker
        assert _extract_binary_info(b"garbage no timestamp here") is None

    def test_real_network_frame_size(self):
        # 6-entry NODES from WBAY = 133 bytes; verify the size survives extraction
        entries = [
            NodeEntry("N6ZX-5",   "WBAY",   "N6ZX-5",  255),
            NodeEntry("K6FB-5",   "SKUNK",  "N6ZX-5",  200),
            NodeEntry("W6OAK-5",  "OAK",    "N6ZX-5",  180),
            NodeEntry("K2YE-5",   "COOL",   "N6ZX-5",  170),
            NodeEntry("KI6ZHD-5", "BETHEL", "N6ZX-5",  160),
            NodeEntry("K6BER-5",  "BRKNRG", "N6ZX-5",  150),
        ]
        nodes_payload = encode_nodes_broadcast("WBAY", entries)
        assert len(nodes_payload) == 133

        raw = _agwpe_monitor_frame("N6ZX-5", "NODES", nodes_payload)
        extracted = _extract_binary_info(raw)
        assert extracted is not None
        assert len(extracted) == 133
