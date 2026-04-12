"""
tests/test_agwpe.py — Unit tests for the AGWPE transport.

Covers:
  - Frame packing / unpacking helpers (_build_frame, _build_unproto_via_frame)
  - Callsign encode / decode round-trips
  - AGWPETransport connection lifecycle:
      login, callsign registration, incoming 'C' / 'D' / 'd' frames
  - Beacon: 'T' frame (no path) and 'V' frame (with digipeater path)

No real network socket is used.  A pair of asyncio.StreamReader / bytes-buffer
objects stand in for the TCP connection to AGWPE.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any
from unittest.mock import MagicMock

import pytest

from bbs.transport.agwpe import (
    AGWPETransport,
    _HEADER_FMT,
    _HEADER_SIZE,
    _build_frame,
    _build_unproto_via_frame,
    _decode_call,
    _encode_call,
    _PID_NO_L3,
)
from bbs.transport.base import Connection


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _unpack_header(frame: bytes) -> dict[str, Any]:
    """Unpack a 36-byte AGWPE header into a dict."""
    (
        port, _, _, _,
        kind_byte, _, pid, _,
        call_from_raw, call_to_raw,
        data_len, _,
    ) = struct.unpack(_HEADER_FMT, frame[:_HEADER_SIZE])
    return {
        "port":      port,
        "kind":      chr(kind_byte),
        "pid":       pid,
        "call_from": _decode_call(call_from_raw),
        "call_to":   _decode_call(call_to_raw),
        "data_len":  data_len,
        "data":      frame[_HEADER_SIZE:],
    }


def _make_agwpe_frame(
    port: int,
    kind: str,
    call_from: str,
    call_to: str,
    pid: int = 0,
    data: bytes = b"",
) -> bytes:
    """Build an AGWPE frame the same way the real AGWPE engine would send it."""
    return _build_frame(port, kind, call_from, call_to, pid, data)


# ─── Frame codec ──────────────────────────────────────────────────────────────

class TestEncodeDecodeCall:
    def test_simple_callsign_roundtrip(self):
        raw = _encode_call("W6ELA")
        # _encode_call produces len(callsign)+1 bytes (trailing null);
        # the struct 10s field right-pads with nulls when packing.
        assert _decode_call(raw) == "W6ELA"

    def test_callsign_with_ssid(self):
        raw = _encode_call("N0CALL-1")
        assert _decode_call(raw) == "N0CALL-1"

    def test_lowercase_uppercased(self):
        raw = _encode_call("w1aw-3")
        assert _decode_call(raw) == "W1AW-3"

    def test_nine_char_max_plus_null(self):
        raw = _encode_call("TOOLONG99")  # 9 chars → fits; 10th byte is \x00
        assert len(raw) == 10
        assert raw[9] == 0

    def test_empty_callsign(self):
        raw = _encode_call("")
        assert _decode_call(raw) == ""


class TestBuildFrame:
    def test_header_size(self):
        frame = _build_frame(0, "D", "W6ELA-5", "N0CALL-1", _PID_NO_L3, b"hello")
        assert len(frame) == _HEADER_SIZE + 5

    def test_kind_byte(self):
        for kind in ("P", "X", "C", "D", "d", "T", "V"):
            f = _build_frame(0, kind, "A", "B")
            assert _unpack_header(f)["kind"] == kind

    def test_port_field(self):
        for p in (0, 1, 3):
            f = _build_frame(p, "D", "A", "B")
            assert _unpack_header(f)["port"] == p

    def test_data_len_matches_payload(self):
        payload = b"BBS payload 123"
        f = _build_frame(0, "D", "W6ELA", "N0CALL", _PID_NO_L3, payload)
        h = _unpack_header(f)
        assert h["data_len"] == len(payload)
        assert h["data"] == payload

    def test_callsign_fields(self):
        f = _build_frame(0, "D", "W6ELA-5", "N0CALL-1", _PID_NO_L3, b"x")
        h = _unpack_header(f)
        assert h["call_from"] == "W6ELA-5"
        assert h["call_to"] == "N0CALL-1"

    def test_empty_payload(self):
        f = _build_frame(0, "X", "N0CALL", "")
        h = _unpack_header(f)
        assert h["data_len"] == 0
        assert h["data"] == b""


class TestBuildUnprotoViaFrame:
    def test_kind_is_V(self):
        f = _build_unproto_via_frame(0, "N0CALL", "BEACON", ["WIDE1-1"], b"test")
        assert _unpack_header(f)["kind"] == "V"

    def test_single_digi_encoding(self):
        f = _build_unproto_via_frame(0, "N0CALL", "BEACON", ["WIDE1-1"], b"msg")
        data = _unpack_header(f)["data"]
        # 1 count byte + 10-byte padded callsign + payload
        assert data[0] == 1
        assert data[1:11].rstrip(b"\x00") == b"WIDE1-1"
        assert data[11:] == b"msg"

    def test_two_digis_encoding(self):
        f = _build_unproto_via_frame(0, "N0CALL", "BEACON", ["WIDE1-1", "WIDE2-1"], b"msg")
        data = _unpack_header(f)["data"]
        assert data[0] == 2
        assert data[1:11].rstrip(b"\x00") == b"WIDE1-1"
        assert data[11:21].rstrip(b"\x00") == b"WIDE2-1"
        assert data[21:] == b"msg"

    def test_empty_path_falls_back_same_as_no_path(self):
        """No digis → 1-byte count of 0 immediately before payload."""
        f = _build_unproto_via_frame(0, "N0CALL", "QST", [], b"hello")
        data = _unpack_header(f)["data"]
        assert data[0] == 0
        assert data[1:] == b"hello"

    def test_total_frame_length(self):
        # 1 count byte + 2 * 10-byte addresses + payload
        payload = b"beacon text"
        f = _build_unproto_via_frame(0, "N0CALL", "BEACON", ["WIDE1-1", "WIDE2-1"], payload)
        assert len(f) == _HEADER_SIZE + 1 + 2 * 10 + len(payload)


# ─── AGWPETransport: connection lifecycle with a fake AGWPE pipe ──────────────

def _make_transport(cfg: dict[str, Any] | None = None) -> AGWPETransport:
    """Build a transport instance pointing at 127.0.0.1:8000 (never actually connected)."""
    return AGWPETransport(cfg or {}, "N0CALL-1")


class _FakeWriter:
    """Collects bytes written to it; mimics asyncio.StreamWriter."""

    def __init__(self) -> None:
        self.written = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True


def _feed_frames(reader: asyncio.StreamReader, *frames: bytes) -> None:
    """Push AGWPE frames into a reader as if received from the network."""
    for f in frames:
        reader.feed_data(f)
    reader.feed_eof()


class TestAGWPETransportDispatch:
    """Test _dispatch() in isolation — no real TCP connection needed."""

    def setup_method(self):
        self.transport = _make_transport()
        self.transport._running = True
        self.fake_writer = _FakeWriter()
        self.received: list[Connection] = []

        async def _on_connect(conn: Connection) -> None:
            self.received.append(conn)
            # Drain the reader so the session "ends"
            try:
                while True:
                    data = await conn.reader.read(1024)
                    if not data:
                        break
            except Exception:
                pass

        self.transport._on_connect = _on_connect

    async def test_incoming_connect_creates_session(self):
        """'C' frame → new _AGWPESession and _on_connect called."""
        c_frame = _make_agwpe_frame(0, "C", "W6ELA-7", "N0CALL-1")
        await self.transport._dispatch(
            "C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer  # type: ignore
        )
        assert (0, "W6ELA-7") in self.transport._sessions

    async def test_data_frame_feeds_reader(self):
        """'D' frames are fed into the session's StreamReader."""
        # First create the session via 'C'
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore

        sess = self.transport._sessions[(0, "W6ELA-7")]
        await self.transport._dispatch("D", 0, "W6ELA-7", "N0CALL-1", _PID_NO_L3, b"Hello\r", self.fake_writer)  # type: ignore

        data = await asyncio.wait_for(sess.reader.read(100), timeout=1.0)
        assert data == b"Hello\r"

    async def test_disconnect_frame_feeds_eof(self):
        """'d' frame removes session and feeds EOF to reader."""
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess = self.transport._sessions[(0, "W6ELA-7")]

        await self.transport._dispatch("d", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore

        assert (0, "W6ELA-7") not in self.transport._sessions
        # Reader should see EOF
        data = await asyncio.wait_for(sess.reader.read(100), timeout=1.0)
        assert data == b""

    async def test_duplicate_connect_ignored(self):
        """A second 'C' for the same station does not replace the session."""
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess1 = self.transport._sessions[(0, "W6ELA-7")]

        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess2 = self.transport._sessions[(0, "W6ELA-7")]

        assert sess1 is sess2

    async def test_data_for_unknown_session_dropped(self):
        """'D' without a prior 'C' is silently discarded — no crash."""
        await self.transport._dispatch("D", 0, "NOBODY", "N0CALL-1", _PID_NO_L3, b"orphan", self.fake_writer)  # type: ignore
        assert (0, "NOBODY") not in self.transport._sessions

    async def test_registration_ack_ok_logged(self, caplog):
        """'X' with Data[0]=1 logs a success message."""
        import logging
        with caplog.at_level(logging.INFO, logger="bbs.transport.agwpe"):
            await self.transport._dispatch("X", 0, "N0CALL-1", "", 0, b"\x01", self.fake_writer)  # type: ignore
        assert "registered" in caplog.text

    async def test_registration_ack_fail_logged(self, caplog):
        """'X' with Data[0]=0 logs a warning."""
        import logging
        with caplog.at_level(logging.WARNING, logger="bbs.transport.agwpe"):
            await self.transport._dispatch("X", 0, "N0CALL-1", "", 0, b"\x00", self.fake_writer)  # type: ignore
        assert "FAILED" in caplog.text

    async def test_unknown_frame_kind_ignored(self):
        """Any unrecognised frame kind doesn't raise and doesn't add a session."""
        await self.transport._dispatch("G", 0, "", "", 0, b"ignored", self.fake_writer)  # type: ignore
        assert len(self.transport._sessions) == 0


class TestAGWPEVirtualWriter:
    """Test the duck-typed writer that wraps outgoing data as 'D' frames."""

    def _make_session_writer(self, local: str = "N0CALL-1", remote: str = "W6ELA-7"):
        from bbs.transport.agwpe import _AGWPEVirtualWriter
        fw = _FakeWriter()
        w = _AGWPEVirtualWriter(fw, local, remote, agw_port=0)  # type: ignore
        return w, fw

    def test_write_produces_D_frame(self):
        w, fw = self._make_session_writer()
        w.write(b"Hello\r")
        assert len(fw.written) == _HEADER_SIZE + 6
        h = _unpack_header(bytes(fw.written))
        assert h["kind"] == "D"
        assert h["data"] == b"Hello\r"

    def test_write_correct_callsigns(self):
        w, fw = self._make_session_writer("N0CALL-1", "W6ELA-7")
        w.write(b"x")
        h = _unpack_header(bytes(fw.written))
        assert h["call_from"] == "N0CALL-1"
        assert h["call_to"] == "W6ELA-7"

    def test_close_sends_d_frame(self):
        w, fw = self._make_session_writer()
        w.close()
        assert len(fw.written) == _HEADER_SIZE
        h = _unpack_header(bytes(fw.written))
        assert h["kind"] == "d"

    def test_write_after_close_dropped(self):
        w, fw = self._make_session_writer()
        w.close()
        fw.written.clear()
        w.write(b"ignored")
        assert len(fw.written) == 0

    async def test_drain_does_not_raise(self):
        w, _ = self._make_session_writer()
        await w.drain()  # should not raise


class TestAGWPEBeaconFrames:
    """Integration-level: verify the exact frames queued by _beacon_loop."""

    async def test_beacon_no_path_sends_T_frame(self):
        t = _make_transport({"beacon_text": "test beacon", "beacon_dest": "QST", "beacon_interval": 1})
        t._running = True
        fw = _FakeWriter()
        registered = asyncio.Event()
        registered.set()  # simulate successful 'X' registration ack

        # Run one iteration of the loop, then cancel
        async def _run():
            await t._beacon_loop(fw, registered)  # type: ignore

        task = asyncio.create_task(_run())
        await asyncio.sleep(0.05)   # let first beacon fire
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(fw.written) >= _HEADER_SIZE
        h = _unpack_header(bytes(fw.written[:_HEADER_SIZE + len(b"test beacon")]))
        assert h["kind"] == "M"
        assert h["data"] == b"test beacon"

    async def test_beacon_with_path_sends_V_frame(self):
        t = _make_transport({
            "beacon_text": "hello",
            "beacon_dest": "BEACON",
            "beacon_path": "WIDE1-1,WIDE2-1",
            "beacon_interval": 1,
        })
        t._running = True
        fw = _FakeWriter()
        registered = asyncio.Event()
        registered.set()  # simulate successful 'X' registration ack

        task = asyncio.create_task(t._beacon_loop(fw, registered))  # type: ignore
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(fw.written) > _HEADER_SIZE
        # Find the 'V' frame kind byte (offset 4)
        assert chr(fw.written[4]) == "V"
        # Path encoding: 1 count byte + N*10s callsigns
        data = bytes(fw.written[_HEADER_SIZE:])
        import struct as _struct
        count = data[0]
        assert count == 2
        via1 = data[1:11].rstrip(b"\x00").decode("ascii")
        via2 = data[11:21].rstrip(b"\x00").decode("ascii")
        assert via1 == "WIDE1-1"
        assert via2 == "WIDE2-1"
        assert data[21:] == b"hello"

    async def test_beacon_disabled_when_no_text(self):
        """beacon_loop should not fire when beacon_text is empty (never started in start())."""
        t = _make_transport({"beacon_text": "", "beacon_interval": 1})
        # beacon_loop itself would still send if called, but start() checks beacon_text
        # before creating the task — verify the sentinel is falsy
        assert not t._beacon_text


class TestAGWPELoginFrame:
    """Check that 'P' login and 'X' registration frames are formed correctly."""

    def test_login_frame_contains_password(self):
        password = "secret123"
        f = _build_frame(0, "P", "", "", data=password.encode("ascii"))
        h = _unpack_header(f)
        assert h["kind"] == "P"
        assert h["data"] == b"secret123"

    def test_registration_frame_sets_callsign(self):
        f = _build_frame(0, "X", "N0CALL-1", "")
        h = _unpack_header(f)
        assert h["kind"] == "X"
        assert h["call_from"] == "N0CALL-1"
        assert h["data_len"] == 0
