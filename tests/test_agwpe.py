"""
tests/test_agwpe.py — Unit tests for the AGWPE transport.

Covers:
  - Frame packing / unpacking helpers (_build_frame, _build_unproto_via_frame)
  - Callsign encode / decode round-trips
  - AGWPETransport connection lifecycle:
      login, callsign registration, incoming 'C' / 'D' / 'd' frames
  - Beacon: 'M' frame (no path) and 'V' frame (with digipeater path)

No real network socket is used.  A pair of asyncio.StreamReader / bytes-buffer
objects stand in for the TCP connection to AGWPE.
"""
from __future__ import annotations

import asyncio
import re
import struct
import time
from pathlib import Path
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
    _decode_signal,
    _encode_call,
    _PID_NO_L3,
    _parse_info,
    _parse_via,
)
from bbs.ax25.netrom_frame import PID_NETROM
from bbs.netrom.circuit import NetromCircuitManager
from bbs.transport.base import Connection, Transport


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


# ─── Monitor string parsers ───────────────────────────────────────────────────

class TestParseInfo:
    """
    _parse_info must return the AX.25 payload that follows >[HH:MM:SS]\\r.

    Actual Direwolf AGWPE monitor format:
      " 1:Fm CALL To DEST [Via PATH ]<UI pid=F0 Len=NN PF=0 >[HH:MM:SS]\\rPAYLOAD\\r\\r\\x00"
    The <...> block is AGWPE frame-control metadata; the info payload follows
    the timestamp marker.
    """

    def test_no_via(self):
        # Real sample: N6ZX direct beacon (no Via)
        monitor = " 1:Fm N6ZX To BEACON <UI pid=F0 Len=55 PF=0 >[15:13:21]\rN6ZX Kings Mt. ARC\r\r\x00"
        assert _parse_info(monitor) == "N6ZX Kings Mt. ARC"

    def test_via_present(self):
        # Real sample: W6ABJ-12 ID frame digipeated via KJOHN*
        monitor = " 1:Fm W6ABJ-12 To ID Via KJOHN*,KBETH,KBERR,WOODY <UI pid=F0 Len=34 PF=0 >[15:16:37]\rW6ABJ-12/R KBULN/D\r\r\x00"
        assert _parse_info(monitor) == "W6ABJ-12/R KBULN/D"

    def test_same_payload_different_timestamps(self):
        """Same station re-heard via different starred digi → identical info (enables dedup)."""
        m1 = " 1:Fm W6ABJ-12 To ID Via KJOHN*,KBETH,KBERR,WOODY <UI pid=F0 Len=34 PF=0 >[15:16:37]\rW6ABJ-12/R KBULN/D\r\r\x00"
        m2 = " 1:Fm W6ABJ-12 To ID Via KJOHN,KBETH,KBERR*,WOODY <UI pid=F0 Len=34 PF=0 >[15:16:39]\rW6ABJ-12/R KBULN/D\r\r\x00"
        assert _parse_info(m1) == _parse_info(m2)

    def test_empty_payload(self):
        assert _parse_info("") == ""


class TestParseVia:
    # Real-world Via format: "Via KJOHN*,KBETH,KBERR,WOODY <UI pid=F0 ..."
    # The \\s*< in _MONITOR_VIA_RE strips the space before <.

    def test_starred_digi(self):
        monitor = " 1:Fm W6ABJ-12 To ID Via KJOHN*,KBETH,KBERR,WOODY <UI pid=F0 Len=34 PF=0 >[15:16:37]\rW6ABJ-12/R KBULN/D\r\r\x00"
        assert _parse_via(monitor) == ["KJOHN*", "KBETH", "KBERR", "WOODY"]

    def test_no_via(self):
        monitor = " 1:Fm N6ZX To BEACON <UI pid=F0 Len=55 PF=0 >[15:13:21]\rN6ZX Kings Mt. ARC\r\r\x00"
        assert _parse_via(monitor) == []


# ─── Fixture-based tests (real captured AGWPE traffic) ────────────────────────

_FIXTURE_LOG  = Path(__file__).parent / "fixtures" / "agwpe_monitor.log"
_LOG_LINE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\t(.+)", re.DOTALL)


def _load_monitor_log(path: Path = _FIXTURE_LOG) -> list[str]:
    """Return raw monitor strings from an agwpe_monitor.log fixture file."""
    entries = []
    with open(path, "rb") as f:
        for raw in f:
            try:
                line = raw.decode("latin-1").rstrip("\n")
            except Exception:
                continue
            m = _LOG_LINE_RE.match(line)
            if m:
                entries.append(m.group(1))
    return entries


@pytest.mark.skipif(not _FIXTURE_LOG.exists(), reason="fixture log not present")
class TestParseFromLog:
    """Verify _parse_info / _parse_via against real captured AGWPE traffic."""

    def test_info_never_contains_ctrl_prefix(self):
        """No parsed info field should start with the AGWPE frame-control metadata."""
        for monitor in _load_monitor_log():
            info = _parse_info(monitor)
            assert not info.startswith("UI pid="), (
                f"_parse_info leaked CTRL block for:\n  {monitor!r}"
            )

    def test_info_consistent_for_same_beacon(self):
        """W6ABJ-12 beacon heard 3× via different digis → same info each time."""
        monitors = [m for m in _load_monitor_log() if "W6ABJ-12" in m and " To ID " in m]
        assert len(monitors) >= 2, "Fixture needs ≥2 W6ABJ-12 ID frames"
        infos = {_parse_info(m) for m in monitors}
        assert len(infos) == 1, f"Expected identical info, got: {infos}"

    def test_via_entries_have_no_ctrl_metadata(self):
        """No via entry should contain angle-brackets or AGWPE metadata."""
        for monitor in _load_monitor_log():
            for digi in _parse_via(monitor):
                assert "<" not in digi and ">" not in digi and "pid" not in digi, (
                    f"via entry looks like CTRL metadata: {digi!r}"
                )


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
        self.transport._drain_lock = asyncio.Lock()

    async def test_incoming_connect_creates_session(self):
        """'C' frame → new _AGWPESession and _on_connect called."""
        c_frame = _make_agwpe_frame(0, "C", "W6ELA-7", "N0CALL-1")
        await self.transport._dispatch(
            "C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer  # type: ignore
        )
        assert (0, "W6ELA-7") in self.transport._sessions

    async def test_outbound_frames_sourced_from_called_ssid(self):
        """Outbound frames must be sourced from the callsign the caller dialed
        (call_to), not the fixed BBS callsign — otherwise Direwolf can't match
        them to the connected-mode stream for a service SSID and drops all
        output. (transport local call here is N0CALL-1; caller dialed W6ELA-9.)"""
        await self.transport._dispatch(
            "C", 0, "W6ELA-15", "W6ELA-9", 0, b"", self.fake_writer,  # type: ignore
        )
        sess = self.transport._sessions[(0, "W6ELA-15")]
        assert sess.writer._local == "W6ELA-9"
        # A real outbound 'D' frame carries CallFrom=W6ELA-9, CallTo=W6ELA-15.
        self.fake_writer.written.clear()
        sess.writer.write(b"hi")
        hdr = _unpack_header(bytes(self.fake_writer.written))
        assert hdr["kind"] == "D"
        assert hdr["call_from"] == "W6ELA-9"
        assert hdr["call_to"] == "W6ELA-15"

    async def test_monitoring_enabled_exactly_once_across_registrations(self):
        """Direwolf's 'm' TOGGLES monitoring, so it must be sent exactly once.
        With extra service SSIDs each callsign gets its own 'X' ack; re-sending
        'm' per ack (an even number) would silence the heard/display plugins."""
        self.transport._registered = asyncio.Event()
        self.transport._monitoring_on = False
        # Two registration acks: BBS callsign + one service SSID.
        for _ in range(2):
            await self.transport._dispatch(
                "X", 0, "", "", 0, b"\x01", self.fake_writer,  # type: ignore[arg-type]
            )
        # Walk the frames written back; count the 'm' (enable-monitoring) frames.
        buf = bytes(self.fake_writer.written)
        m_count, i = 0, 0
        while i + _HEADER_SIZE <= len(buf):
            hdr = _unpack_header(buf[i:])
            if hdr["kind"] == "m":
                m_count += 1
            i += _HEADER_SIZE + hdr["data_len"]
        assert m_count == 1

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

    async def test_duplicate_connect_replaces_session(self):
        """A second 'C' (AX.25 reconnect) replaces the old session with a new one."""
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess1 = self.transport._sessions[(0, "W6ELA-7")]

        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess2 = self.transport._sessions[(0, "W6ELA-7")]

        assert sess1 is not sess2

    async def test_duplicate_connect_feeds_eof_to_old_session(self):
        """The old session's reader gets EOF when a duplicate 'C' arrives."""
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess1 = self.transport._sessions[(0, "W6ELA-7")]

        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore

        data = await asyncio.wait_for(sess1.reader.read(100), timeout=1.0)
        assert data == b"", "old session reader should have received EOF"

    async def test_duplicate_connect_on_connect_called_twice(self):
        """_on_connect is invoked for each 'C', including the reconnect."""
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        # Yield so the first session task starts running.
        await asyncio.sleep(0)

        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        await asyncio.sleep(0)

        assert len(self.received) == 2
        assert all(c.remote_addr == "W6ELA-7" for c in self.received)

    async def test_duplicate_connect_data_goes_to_new_session(self):
        """Data frames after a reconnect are delivered to the new session, not the old."""
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess1 = self.transport._sessions[(0, "W6ELA-7")]

        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", self.fake_writer)  # type: ignore
        sess2 = self.transport._sessions[(0, "W6ELA-7")]

        await self.transport._dispatch("D", 0, "W6ELA-7", "N0CALL-1", _PID_NO_L3, b"fresh\r", self.fake_writer)  # type: ignore

        data = await asyncio.wait_for(sess2.reader.read(100), timeout=1.0)
        assert data == b"fresh\r"
        # Old session should not have received any data (only EOF).
        assert sess1.reader._buffer == bytearray()

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

    async def test_concurrent_drain_does_not_assert(self):
        """Two sessions draining concurrently must not raise AssertionError.

        Python 3.9 asyncio uses a single _drain_waiter on StreamWriter and
        asserts it is None before creating a new one.  The shared drain_lock
        must serialise concurrent drain() calls to prevent that crash.
        """
        drain_entered = asyncio.Event()

        class _SlowFakeWriter(_FakeWriter):
            async def drain(self) -> None:
                drain_entered.set()
                await asyncio.sleep(0.05)  # hold drain open so overlap is certain

        slow_fw = _SlowFakeWriter()

        # Two sessions on different ports but sharing the same underlying writer.
        await self.transport._dispatch("C", 0, "W6ELA-7", "N0CALL-1", 0, b"", slow_fw)  # type: ignore
        await self.transport._dispatch("C", 1, "W6ELA-8", "N0CALL-1", 0, b"", slow_fw)  # type: ignore

        sess1 = self.transport._sessions[(0, "W6ELA-7")]
        sess2 = self.transport._sessions[(1, "W6ELA-8")]

        # Both sessions drain concurrently — must not raise AssertionError.
        await asyncio.gather(sess1.writer.drain(), sess2.writer.drain())


class TestAGWPEVirtualWriter:
    """Test the duck-typed writer that wraps outgoing data as 'D' frames."""

    def _make_session_writer(self, local: str = "N0CALL-1", remote: str = "W6ELA-7"):
        from bbs.transport.agwpe import _AGWPEVirtualWriter
        fw = _FakeWriter()
        lock = asyncio.Lock()
        w = _AGWPEVirtualWriter(fw, local, remote, agw_port=0, drain_lock=lock)  # type: ignore
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
        lock = asyncio.Lock()

        async def _run():
            await t._beacon_loop(fw, registered, lock)  # type: ignore

        task = asyncio.create_task(_run())
        await asyncio.sleep(0.05)   # let first beacon fire
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(fw.written) >= _HEADER_SIZE
        h = _unpack_header(bytes(fw.written[:_HEADER_SIZE + len(b"test beacon")]))
        # AGWPE 'M' = Send UNPROTO Information (no via). 'T' was a server→
        # client confirmation; Direwolf rejects it as INVALID when sent
        # client→server.
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

        lock = asyncio.Lock()
        task = asyncio.create_task(t._beacon_loop(fw, registered, lock))  # type: ignore
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


# ─── Hop-count caching from monitored SABM/SABME frames ──────────────────────

def _make_sabm_monitor_text(call_from: str, call_to: str, via: list[str]) -> bytes:
    """Build a Direwolf-style AGWPE monitor payload for a SABME frame.

    Direwolf sends this under datakind 'S' (Supervisory + non-UI U-frames).
    Each digi that handled the frame gets a trailing '*'.
    """
    via_str = f" Via {','.join(v + '*' for v in via)}" if via else ""
    text = f" 1:Fm {call_from} To {call_to}{via_str} <SABME PF=1 >[12:34:56]\r\r\x00"
    return text.encode("ascii")


class TestHopCountFromMonitoredSABM:
    """SABM/SABME monitor frames cache the via-path length so the upcoming
    'C' (Connected) event can attach the correct hop_count to Connection.

    Regression coverage for: bbs2 used to only check datakind 'U', but
    Direwolf sends SABMs under datakind 'S' — so hop_count silently stayed 0
    on every incoming connection no matter how many digis were in the path."""

    def setup_method(self):
        self.transport = _make_transport()
        self.transport._running = True
        self.fake_writer = _FakeWriter()
        self.received: list[Connection] = []

        async def _on_connect(conn: Connection) -> None:
            self.received.append(conn)
            try:
                while await conn.reader.read(1024):
                    pass
            except Exception:
                pass

        self.transport._on_connect = _on_connect
        self.transport._drain_lock = asyncio.Lock()

    async def _send_sabm(self, kind: str, call_from: str, via: list[str]) -> None:
        payload = _make_sabm_monitor_text(call_from, "N0CALL-1", via)
        await self.transport._dispatch(
            kind, 0, call_from, "N0CALL-1", 0, payload, self.fake_writer,  # type: ignore[arg-type]
        )

    async def _send_connect(self, call_from: str) -> None:
        await self.transport._dispatch(
            "C", 0, call_from, "N0CALL-1", 0, b"", self.fake_writer,  # type: ignore[arg-type]
        )

    async def test_sabm_under_S_caches_hop_count(self):
        """Direwolf sends SABM under datakind 'S' — that path must populate the cache."""
        await self._send_sabm("S", "W6ELA-7", ["HMKR", "KRDG", "KBANN", "WOODY"])
        assert self.transport._pending_hop_counts.get("W6ELA-7") == 4

    async def test_sabm_under_U_also_caches_hop_count(self):
        """Some monitor implementations may deliver SABM under 'U'; accept that too."""
        await self._send_sabm("U", "W6ELA-7", ["HMKR", "KRDG"])
        assert self.transport._pending_hop_counts.get("W6ELA-7") == 2

    async def test_connect_after_sabm_attaches_hop_count(self):
        """'S' then 'C' → Connection.hop_count reflects the cached via-length."""
        await self._send_sabm("S", "W6ELA-7", ["HMKR", "KRDG", "KBANN"])
        await self._send_connect("W6ELA-7")
        await asyncio.sleep(0)  # let session task start
        assert len(self.received) == 1
        assert self.received[0].hop_count == 3
        # The cache entry is consumed on use.
        assert "W6ELA-7" not in self.transport._pending_hop_counts

    async def test_direct_sabm_no_via_yields_zero_hops(self):
        await self._send_sabm("S", "W6ELA-7", [])
        await self._send_connect("W6ELA-7")
        await asyncio.sleep(0)
        assert self.received[0].hop_count == 0

    async def test_connect_without_prior_sabm_defaults_to_zero(self):
        """If the SABM monitor was missed (e.g. race), hop_count silently defaults to 0."""
        await self._send_connect("W6ELA-7")
        await asyncio.sleep(0)
        assert self.received[0].hop_count == 0

    async def test_sabm_for_other_callsign_not_cached(self):
        """SABM directed at another callsign on the same channel must not pollute our cache."""
        payload = _make_sabm_monitor_text("W6ELA-7", "SOMEONE-ELSE", ["HMKR", "KRDG"])
        await self.transport._dispatch(
            "S", 0, "W6ELA-7", "SOMEONE-ELSE", 0, payload, self.fake_writer,  # type: ignore[arg-type]
        )
        assert "W6ELA-7" not in self.transport._pending_hop_counts

    async def test_non_sabm_S_frame_does_not_cache(self):
        """A non-SABM 'S' frame (e.g. RR, UA, DM) must not populate the cache."""
        text = " 1:Fm W6ELA-7 To N0CALL-1 Via HMKR*,KRDG* <RR P=0 R=2 >[12:34:56]\r\r\x00"
        await self.transport._dispatch(
            "S", 0, "W6ELA-7", "N0CALL-1", 0, text.encode("ascii"), self.fake_writer,  # type: ignore[arg-type]
        )
        assert "W6ELA-7" not in self.transport._pending_hop_counts


class TestReadLoopIsolatesDispatchErrors:
    """A failure handling one frame must not tear down the whole read loop
    (which would drop every connected user and bounce the TCP link)."""

    async def test_dispatch_exception_does_not_kill_read_loop(self):
        transport = _make_transport()
        transport._running = True
        transport._drain_lock = asyncio.Lock()

        calls: list[str] = []

        async def _boom_then_ok(kind, port, call_from, call_to, pid, payload, writer, user_reserved=b""):
            calls.append(kind)
            if len(calls) == 1:
                raise RuntimeError("simulated per-frame dispatch failure")

        transport._dispatch = _boom_then_ok  # type: ignore[assignment]

        reader = asyncio.StreamReader()
        _feed_frames(
            reader,
            _build_frame(0, "C", "W6ELA-7", "N0CALL-1"),
            _build_frame(0, "d", "W6ELA-7", "N0CALL-1"),
        )

        # Should return normally when the reader hits EOF — NOT propagate the
        # RuntimeError raised while dispatching the first frame.
        await transport._read_loop(reader, _FakeWriter())  # type: ignore[arg-type]

        # Both frames were dispatched: the exception on the first did not
        # abort processing of the second.
        assert calls == ["C", "d"]

    async def test_cancellation_still_propagates(self):
        """CancelledError must escape the guard so the loop can be torn down."""
        transport = _make_transport()
        transport._running = True
        transport._drain_lock = asyncio.Lock()

        async def _cancel(kind, port, call_from, call_to, pid, payload, writer, user_reserved=b""):
            raise asyncio.CancelledError()

        transport._dispatch = _cancel  # type: ignore[assignment]

        reader = asyncio.StreamReader()
        _feed_frames(reader, _build_frame(0, "C", "W6ELA-7", "N0CALL-1"))

        with pytest.raises(asyncio.CancelledError):
            await transport._read_loop(reader, _FakeWriter())  # type: ignore[arg-type]


# ─── N1: outbound NETROM crosslink origination (connect_out) ──────────────────

def _iter_headers(buf: bytes):
    """Yield unpacked header dicts for each AGWPE frame in *buf*."""
    i = 0
    while i + _HEADER_SIZE <= len(buf):
        h = _unpack_header(buf[i:])
        yield h
        i += _HEADER_SIZE + h["data_len"]


def _count_kind(buf: bytes, kind: str) -> int:
    return sum(1 for h in _iter_headers(buf) if h["kind"] == kind)


_CONNECTED_WITH = b"*** CONNECTED With Station W6ELA-2\r"
_RETRYOUT       = b"*** DISCONNECTED RETRYOUT With W6ELA-2\r"


class TestConnectOut:
    """N1: originate an outbound AX.25 crosslink to a NETROM neighbor.

    Drives connect_out() against a fake AGWPE socket writer and simulates
    Direwolf's 'C' confirmation / 'd' rejection by feeding _dispatch().
    """

    def _make_connected_transport(self):
        """Transport wired as if the AGWPE TCP link is up and registered."""
        t = _make_transport()
        t._running = True
        t._drain_lock = asyncio.Lock()
        fw = _FakeWriter()
        t._sock_writer = fw  # type: ignore[assignment]

        async def _on_connect(conn: Connection) -> None:
            try:
                while await conn.reader.read(1024):
                    pass
            except Exception:
                pass

        t._on_connect = _on_connect
        return t, fw

    async def test_connect_out_sends_wellformed_C(self):
        """connect_out emits a 'C' frame from our call to the neighbor."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        try:
            hdrs = [h for h in _iter_headers(bytes(fw.written)) if h["kind"] == "C"]
            assert len(hdrs) == 1
            assert hdrs[0]["call_from"] == "N0CALL-1"
            assert hdrs[0]["call_to"] == "W6ELA-2"
            assert (0, "W6ELA-2") in t._pending_connects
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def test_connect_out_confirmation_returns_manager(self):
        """Simulated 'C' confirmation resolves connect_out to a manager and
        registers the crosslink session."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        mgr = await asyncio.wait_for(task, timeout=1.0)
        assert isinstance(mgr, NetromCircuitManager)
        assert (0, "W6ELA-2") in t._sessions
        assert t._sessions[(0, "W6ELA-2")].netrom_manager is mgr
        # pending entry consumed
        assert (0, "W6ELA-2") not in t._pending_connects

    async def test_connect_out_no_bbs_session_task_started(self):
        """The outbound crosslink must NOT spin up a BBS session task."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        await asyncio.wait_for(task, timeout=1.0)
        assert (0, "W6ELA-2") not in t._session_tasks

    async def test_originate_circuit_works_over_new_crosslink(self):
        """After connect_out, originate_circuit sends a NETROM CONNECT REQ
        ('D' frame, PID=0xCF) out on the same socket writer."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        mgr = await asyncio.wait_for(task, timeout=1.0)

        fw.written.clear()
        # originate_circuit blocks awaiting a CONNECT ACK that never comes;
        # run it briefly, then verify the CONNECT REQ went on the wire.
        oc = asyncio.create_task(mgr.originate_circuit("W6ELA-2", "N0USER-1"))
        await asyncio.sleep(0)
        try:
            d_frames = [h for h in _iter_headers(bytes(fw.written)) if h["kind"] == "D"]
            assert len(d_frames) == 1
            assert d_frames[0]["pid"] == PID_NETROM
            assert d_frames[0]["call_from"] == "N0CALL-1"
            assert d_frames[0]["call_to"] == "W6ELA-2"
        finally:
            oc.cancel()
            try:
                await oc
            except (asyncio.CancelledError, Exception):
                pass

    async def test_retryout_rejects_connect(self):
        """A 'd'/RETRYOUT while pending → connect_out raises ConnectionError."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch(
            "d", 0, "W6ELA-2", "N0CALL-1", 0, _RETRYOUT, fw,  # type: ignore[arg-type]
        )
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, timeout=1.0)
        assert (0, "W6ELA-2") not in t._sessions
        assert (0, "W6ELA-2") not in t._pending_connects

    async def test_reuse_returns_existing_manager(self):
        """A second connect_out to a neighbor with an existing crosslink returns
        the same manager and sends no new 'C'."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        mgr1 = await asyncio.wait_for(task, timeout=1.0)

        fw.written.clear()
        mgr2 = await t.connect_out("W6ELA-2")
        assert mgr2 is mgr1
        assert _count_kind(bytes(fw.written), "C") == 0

    async def test_concurrent_connects_coalesce(self):
        """Two concurrent connect_out to the same neighbor share one 'C' and one
        future, both resolving to the same manager."""
        t, fw = self._make_connected_transport()
        task1 = asyncio.create_task(t.connect_out("W6ELA-2"))
        task2 = asyncio.create_task(t.connect_out("W6ELA-2"))
        # Let both run up to their awaits.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert _count_kind(bytes(fw.written), "C") == 1
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        mgr1 = await asyncio.wait_for(task1, timeout=1.0)
        mgr2 = await asyncio.wait_for(task2, timeout=1.0)
        assert mgr1 is mgr2

    async def test_timeout_cleans_up_pending(self):
        """No confirmation → TimeoutError and the pending entry is removed."""
        t, fw = self._make_connected_transport()
        with pytest.raises(asyncio.TimeoutError):
            await t.connect_out("W6ELA-2", timeout=0.05)
        assert (0, "W6ELA-2") not in t._pending_connects

    async def test_not_connected_raises(self):
        """connect_out with no live socket writer raises ConnectionError."""
        t = _make_transport()
        t._running = True
        t._drain_lock = asyncio.Lock()
        t._sock_writer = None
        with pytest.raises(ConnectionError):
            await t.connect_out("W6ELA-2")

    async def test_tcp_drop_fails_pending(self):
        """Simulated TCP drop fails an in-flight connect future."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        assert (0, "W6ELA-2") in t._pending_connects
        # Mirror start()'s finally-block cleanup on TCP drop.
        t._sock_writer = None
        t._fail_pending_connects(ConnectionError("TCP lost"))
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, timeout=1.0)
        assert not t._pending_connects

    async def test_crosslink_observer_fires_up_and_down(self):
        """A live crosslink is proof of adjacency — connect_out fires the
        observer up on confirmation and down on teardown (N0.5)."""
        t, fw = self._make_connected_transport()
        events: list[tuple[str, bool]] = []
        t.set_netrom_crosslink_observer(lambda call, up: events.append((call, up)))
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch("C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw)  # type: ignore[arg-type]
        await asyncio.wait_for(task, timeout=1.0)
        assert ("W6ELA-2", True) in events
        await t._dispatch("d", 0, "W6ELA-2", "N0CALL-1", 0, b"", fw)  # type: ignore[arg-type]
        assert ("W6ELA-2", False) in events

    async def test_confirmation_C_does_not_trip_duplicate_teardown(self):
        """An outbound 'C' confirmation must be handled by the pending path,
        not the inbound duplicate-'C' reconnect teardown."""
        t, fw = self._make_connected_transport()
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        mgr = await asyncio.wait_for(task, timeout=1.0)
        # A duplicate inbound-style 'C' now (no pending) should replace the
        # session — proving the first 'C' created exactly one crosslink and the
        # pending path returned before the duplicate logic.
        assert t._sessions[(0, "W6ELA-2")].netrom_manager is mgr


class TestConnectNetromBaseDefault:
    """base.Transport.connect_netrom default returns None."""

    async def test_default_returns_none(self):
        class _StubTransport(Transport):
            async def start(self, on_connect):  # pragma: no cover - trivial
                pass

            async def stop(self):  # pragma: no cover - trivial
                pass

        t = _StubTransport()
        assert await t.connect_netrom("W6ELA-2") is None

    async def test_agwpe_connect_netrom_delegates_to_connect_out(self):
        """AGWPE override returns the manager connect_out produced."""
        t = _make_transport()
        t._running = True
        t._drain_lock = asyncio.Lock()
        fw = _FakeWriter()
        t._sock_writer = fw  # type: ignore[assignment]

        async def _on_connect(conn: Connection) -> None:
            pass

        t._on_connect = _on_connect
        task = asyncio.create_task(t.connect_netrom("W6ELA-2"))
        await asyncio.sleep(0)
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-1", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        mgr = await asyncio.wait_for(task, timeout=1.0)
        assert isinstance(mgr, NetromCircuitManager)


# ─── N3: node-identity sourcing (set_netrom_node_call) ────────────────────────

class TestNetromNodeCallSourcing:
    """N3: outbound crosslinks + NODES originate from the NET/ROM node call
    (the node SSID when configured, else the BBS callsign)."""

    def _connected(self, node_call: str | None = None):
        t = _make_transport()          # BBS call = N0CALL-1
        if node_call is not None:
            t.set_netrom_node_call(node_call)
        t._running = True
        t._drain_lock = asyncio.Lock()
        fw = _FakeWriter()
        t._sock_writer = fw  # type: ignore[assignment]

        async def _on_connect(conn: Connection) -> None:
            try:
                while await conn.reader.read(1024):
                    pass
            except Exception:
                pass

        t._on_connect = _on_connect
        return t, fw

    def test_default_node_call_is_bbs_call(self):
        t = _make_transport()
        assert t._netrom_node_call == "N0CALL-1"

    async def test_connect_out_sources_C_from_node_call(self):
        t, fw = self._connected("N0CALL-5")
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        try:
            c = [h for h in _iter_headers(bytes(fw.written)) if h["kind"] == "C"]
            assert c[0]["call_from"] == "N0CALL-5"     # node SSID, not the BBS call
            assert c[0]["call_to"] == "W6ELA-2"
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def test_outbound_crosslink_and_circuit_source_from_node_call(self):
        t, fw = self._connected("N0CALL-5")
        task = asyncio.create_task(t.connect_out("W6ELA-2"))
        await asyncio.sleep(0)
        # Direwolf confirms; the outbound crosslink is built from the node call.
        await t._dispatch(
            "C", 0, "W6ELA-2", "N0CALL-5", 0, _CONNECTED_WITH, fw,  # type: ignore[arg-type]
        )
        mgr = await asyncio.wait_for(task, timeout=1.0)
        sess = t._sessions[(0, "W6ELA-2")]
        assert sess.writer._local == "N0CALL-5"        # AGWPE 'D' CallFrom
        # And the L3 circuit CONNECT REQ sources from the node call too.
        fw.written.clear()
        oc = asyncio.create_task(mgr.originate_circuit("W6ELA-2", "N0USER-1"))
        await asyncio.sleep(0)
        try:
            d = [h for h in _iter_headers(bytes(fw.written)) if h["kind"] == "D"]
            assert d[0]["call_from"] == "N0CALL-5"
            assert d[0]["pid"] == PID_NETROM
        finally:
            oc.cancel()
            try:
                await oc
            except (asyncio.CancelledError, Exception):
                pass

    async def test_nodes_broadcast_sources_from_node_call(self):
        t = _make_transport()
        t.set_netrom_node_call("N0CALL-5")
        t._running = True
        t._netrom_nodes_builder = lambda: [b"\xffPALO  "]   # one non-empty frame
        fw = _FakeWriter()
        registered = asyncio.Event()
        registered.set()
        lock = asyncio.Lock()
        task = asyncio.create_task(t._netrom_nodes_loop(fw, registered, lock))  # type: ignore
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        m = [h for h in _iter_headers(bytes(fw.written)) if h["kind"] == "M"]
        assert m and m[0]["call_from"] == "N0CALL-5"    # NODES from the node SSID
        assert m[0]["call_to"] == "NODES"

    async def test_nodes_broadcast_sends_one_frame_per_payload(self):
        # N6a: a fragmented routing table yields several payloads; the loop must
        # send one NODES UI frame per payload (not concatenate into one frame).
        t = _make_transport()
        t.set_netrom_node_call("N0CALL-5")
        t._running = True
        t._netrom_nodes_builder = lambda: [
            b"\xffPALO  \x01", b"\xffPALO  \x02", b"\xffPALO  \x03",
        ]
        fw = _FakeWriter()
        registered = asyncio.Event()
        registered.set()
        lock = asyncio.Lock()
        task = asyncio.create_task(t._netrom_nodes_loop(fw, registered, lock))  # type: ignore
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        m = [h for h in _iter_headers(bytes(fw.written)) if h["kind"] == "M"]
        assert len(m) == 3                              # one cycle → three frames
        assert all(h["call_to"] == "NODES" for h in m)

    def test_set_extra_callsigns_dedups_node_call(self):
        t = _make_transport()
        t.set_netrom_node_call("N0CALL-5")
        t.set_extra_callsigns(["N0CALL-5", "W6ELA-9"])
        assert "N0CALL-5" not in t._extra_callsigns   # registered once, in start()
        assert "W6ELA-9" in t._extra_callsigns


# ─── Broadcast cadence across restarts (beacon / NODES timestamp persistence) ──

class TestBroadcastCadence:
    """QoL: persist the last beacon/NODES broadcast so a restart respects the
    configured cadence instead of transmitting immediately (politer on air)."""

    def test_no_state_path_sends_immediately(self):
        t = _make_transport()                          # persistence not wired
        assert t._initial_broadcast_delay("beacon", 1200) == 0.0

    def test_never_broadcast_sends_immediately(self, tmp_path):
        t = _make_transport()
        t.set_broadcast_state_path(str(tmp_path / "s.json"))
        assert t._initial_broadcast_delay("nodes", 1800) == 0.0

    def test_recent_broadcast_defers_the_remainder(self, tmp_path):
        t = _make_transport()
        t.set_broadcast_state_path(str(tmp_path / "s.json"))
        t._save_broadcast_state("beacon", time.time())     # just transmitted
        d = t._initial_broadcast_delay("beacon", 1200)
        assert 1100 < d <= 1200                            # ~full interval left

    def test_overdue_broadcast_sends_immediately(self, tmp_path):
        t = _make_transport()
        t.set_broadcast_state_path(str(tmp_path / "s.json"))
        t._save_broadcast_state("nodes", time.time() - 3600)   # an hour ago
        assert t._initial_broadcast_delay("nodes", 1800) == 0.0

    def test_state_roundtrips_with_independent_keys(self, tmp_path):
        p = str(tmp_path / "s.json")
        t = _make_transport(); t.set_broadcast_state_path(p)
        t._save_broadcast_state("beacon", 100.0)
        t._save_broadcast_state("nodes", 200.0)
        t2 = _make_transport(); t2.set_broadcast_state_path(p)  # a "restart"
        st = t2._load_broadcast_state()
        assert st["beacon"] == 100.0 and st["nodes"] == 200.0

    def test_corrupt_state_file_is_ignored(self, tmp_path):
        p = tmp_path / "s.json"; p.write_text("not json{")
        t = _make_transport(); t.set_broadcast_state_path(str(p))
        assert t._load_broadcast_state() == {}
        assert t._initial_broadcast_delay("beacon", 1200) == 0.0


# ─── Signal-quality extension (Direwolf AGWPE opt-in) ─────────────────────────

class TestDecodeSignal:
    """_decode_signal() — extract per-frame signal from user_reserved bytes."""

    def test_populated(self):
        assert _decode_signal(bytes([28, 10, 6, 0])) == (28, 10, 6, 0)

    def test_non_afsk_sentinel_passed_through(self):
        # 0xFF mark/space (non-AFSK) decoded verbatim; the plugin maps it to NULL.
        assert _decode_signal(bytes([40, 0xFF, 0xFF, 1])) == (40, 255, 255, 1)

    def test_all_zero_is_none(self):
        assert _decode_signal(bytes([0, 0, 0, 0])) is None

    def test_short_or_empty_is_none(self):
        assert _decode_signal(b"\x1c") is None
        assert _decode_signal(b"") is None


class TestSignalQualityExtension:
    """Opt-in 'q' handshake and per-frame signal on monitor frames."""

    _MON = (b" 1:Fm N6ZX To BEACON <UI pid=F0 Len=5 PF=0 >[15:13:21]\rhi\r\r\x00")

    def setup_method(self):
        self.transport = _make_transport()
        self.transport._running = True
        self.transport._drain_lock = asyncio.Lock()
        self.fake_writer = _FakeWriter()
        self.heard: list[tuple] = []

        async def _observer(src, dest, via, ts, transport, info, signal=None,
                            count_it=True, log_signal=True):
            self.heard.append((src, dest, via, info, signal, count_it, log_signal))

        self.transport._heard_observer = _observer

    @staticmethod
    def _written_kinds(writer) -> list[str]:
        buf = bytes(writer.written)
        kinds, i = [], 0
        while i + _HEADER_SIZE <= len(buf):
            hdr = _unpack_header(buf[i:])
            kinds.append(hdr["kind"])
            i += _HEADER_SIZE + hdr["data_len"]
        return kinds

    async def test_q_opt_in_sent_after_m(self):
        self.transport._registered = asyncio.Event()
        self.transport._monitoring_on = False
        await self.transport._dispatch("X", 0, "", "", 0, b"\x01", self.fake_writer)
        kinds = self._written_kinds(self.fake_writer)
        assert "m" in kinds and "q" in kinds
        assert kinds.index("q") > kinds.index("m")  # opt-in follows monitor-enable

    async def test_q_opt_in_suppressed_when_disabled(self):
        t = _make_transport({"signal_quality": False})
        t._running = True
        t._drain_lock = asyncio.Lock()
        t._registered = asyncio.Event()
        w = _FakeWriter()
        await t._dispatch("X", 0, "", "", 0, b"\x01", w)
        kinds = self._written_kinds(w)
        assert "m" in kinds and "q" not in kinds

    async def test_q_ack_activates_extension(self):
        assert self.transport._ext_sig_active is False
        await self.transport._dispatch("q", 0, "", "", 0, b"ExtSig=1\x00", self.fake_writer)
        assert self.transport._ext_sig_active is True

    async def test_monitor_frame_carries_signal_when_active(self):
        self.transport._ext_sig_active = True
        await self.transport._dispatch(
            "U", 0, "N6ZX", "BEACON", 0xF0, self._MON,
            self.fake_writer, bytes([28, 10, 6, 0]),
        )
        assert len(self.heard) == 1
        assert self.heard[0][0] == "N6ZX"
        assert self.heard[0][4] == (28, 10, 6, 0)

    async def test_monitor_frame_no_signal_when_inactive(self):
        # Un-acked transport must ignore the bytes even if populated.
        self.transport._ext_sig_active = False
        await self.transport._dispatch(
            "U", 0, "N6ZX", "BEACON", 0xF0, self._MON,
            self.fake_writer, bytes([28, 10, 6, 0]),
        )
        assert len(self.heard) == 1
        assert self.heard[0][4] is None

    _NODES = (b" 1:Fm KI6ZHD-5 To NODES <UI pid=CF >[15:13:21]\r\x01SCLARA\r\x00")

    async def test_netrom_nodes_frame_records_signal_uncounted(self):
        # NET/ROM frames go to the routing observer AND now feed a signal-only,
        # uncounted heard record for the transmitter (beacon text suppressed).
        self.transport._ext_sig_active = True
        netrom_seen = []

        async def _netrom(src, dest, binary):
            netrom_seen.append((src, dest, binary))
        self.transport._netrom_observer = _netrom

        await self.transport._dispatch(
            "U", 0, "KI6ZHD-5", "NODES", 0xF0, self._NODES,
            self.fake_writer, bytes([57, 11, 9, 0]),
        )
        assert netrom_seen and netrom_seen[0][0] == "KI6ZHD-5"   # routing still ran
        assert len(self.heard) == 1
        src, dest, via, info, signal, count_it, log_signal = self.heard[0]
        assert src == "KI6ZHD-5"
        assert signal == (57, 11, 9, 0)
        assert info == ""            # binary payload not stored as beacon text
        assert count_it is False     # not double-counted

    async def test_netrom_frame_no_signal_when_extension_inactive(self):
        # Without the extension the NET/ROM path is unchanged: routing only.
        self.transport._ext_sig_active = False

        async def _netrom(src, dest, binary):
            pass
        self.transport._netrom_observer = _netrom

        await self.transport._dispatch(
            "U", 0, "KI6ZHD-5", "NODES", 0xF0, self._NODES,
            self.fake_writer, bytes([57, 11, 9, 0]),
        )
        assert len(self.heard) == 0

    # Connected-mode monitored frames (a live BBS caller): 'I' = info, 'S' = RR/SABM.
    _INFO = b" 1:Fm KK6FPP To W6ELA-1 <I S0 R0 pid=F0 >[10:29:43]\rh\r\x00"
    _RR   = b" 1:Fm KK6FPP To W6ELA-1 <RR R15 >[10:29:28]\r\x00"

    async def test_connected_info_frame_records_signal_realtime(self):
        self.transport._ext_sig_active = True
        await self.transport._dispatch(
            "I", 0, "KK6FPP", "W6ELA-1", 0xF0, self._INFO,
            self.fake_writer, bytes([59, 16, 11, 0]),
        )
        assert len(self.heard) == 1
        src, dest, via, info, signal, count_it, log_signal = self.heard[0]
        assert src == "KK6FPP"
        assert signal == (59, 16, 11, 0)   # the caller's live level
        assert info == ""                  # session data, not beacon text
        assert count_it is False           # not counted per-frame
        assert log_signal is False         # quiet: no per-frame log spam

    async def test_connected_supervisory_frame_records_signal(self):
        self.transport._ext_sig_active = True
        await self.transport._dispatch(
            "S", 0, "KK6FPP", "W6ELA-1", 0xF0, self._RR,
            self.fake_writer, bytes([61, 16, 11, 0]),
        )
        assert len(self.heard) == 1
        assert self.heard[0][0] == "KK6FPP"
        assert self.heard[0][4] == (61, 16, 11, 0)

    async def test_connected_own_frame_not_recorded(self):
        # Our own transmitted frames arrive as 'T', but guard anyway.
        self.transport._ext_sig_active = True
        await self.transport._dispatch(
            "I", 0, "N0CALL-1", "KK6FPP", 0xF0, self._INFO,   # local call = N0CALL-1
            self.fake_writer, bytes([59, 16, 11, 0]),
        )
        assert len(self.heard) == 0

    async def test_connected_frame_no_signal_when_extension_inactive(self):
        self.transport._ext_sig_active = False
        await self.transport._dispatch(
            "I", 0, "KK6FPP", "W6ELA-1", 0xF0, self._INFO,
            self.fake_writer, bytes([59, 16, 11, 0]),
        )
        assert len(self.heard) == 0

    async def test_garbage_source_sentinel_dropped(self):
        # Direwolf's "??????" placeholder = "(Not AX.25)" noise with no valid
        # source address. Must be dropped before ANY subsystem — no phantom
        # heard/signal row, and not dispatched to the NET/ROM observer either.
        self.transport._ext_sig_active = True
        netrom_seen = []

        async def _netrom(src, dest, binary):
            netrom_seen.append(src)
        self.transport._netrom_observer = _netrom

        for k in ("U", "S", "I"):
            await self.transport._dispatch(
                k, 0, "??????", "??????", 0xF0, self._MON,
                self.fake_writer, bytes([70, 11, 10, 0]),
            )
        assert self.heard == []
        assert netrom_seen == []


def test_set_netrom_node_alias_normalizes():
    t = _make_transport()
    assert t._netrom_node_alias == ""      # default: no alias
    t.set_netrom_node_alias(" palo ")
    assert t._netrom_node_alias == "PALO"  # uppercased + stripped
    t.set_netrom_node_alias("")
    assert t._netrom_node_alias == ""


def test_netrom_circuits_snapshot_empty_by_default():
    assert _make_transport().netrom_circuits_snapshot() == []


def test_netrom_circuits_snapshot_aggregates_across_sessions():
    """The transport flattens every crosslink's active circuits into one list,
    skipping sessions that carry no NET/ROM manager."""
    t = _make_transport()

    class _Circ:
        def __init__(self, user):
            self.user = user
        def describe(self):
            return {"user": self.user, "state": "CONNECTED"}

    class _Mgr:
        def __init__(self, circuits):
            self.active_circuits = circuits

    class _Sess:
        def __init__(self, mgr):
            self.netrom_manager = mgr

    t._sessions = {
        ("p", "A"): _Sess(_Mgr([_Circ("KN6PE-7")])),
        ("p", "B"): _Sess(None),                         # no crosslink manager
        ("p", "C"): _Sess(_Mgr([_Circ("KF6ANX-9")])),
    }
    users = {c["user"] for c in t.netrom_circuits_snapshot()}
    assert users == {"KN6PE-7", "KF6ANX-9"}


def test_base_transport_has_all_engine_netrom_setters():
    """The engine wires NET/ROM onto EVERY transport via t.(set_)netrom_*(...);
    each such method must exist on the base Transport so non-AGWPE transports
    (TCP, KISS) don't AttributeError at startup.  Regressions: set_netrom_node_alias
    (a setter) and netrom_circuits_snapshot (a reader called from netrom_snapshot)
    were each added to AGWPE only, which would crash a station with a TCP transport."""
    import re, inspect
    from bbs.core import engine as engine_mod
    src = inspect.getsource(engine_mod)
    called = set(re.findall(r"\bt\.((?:set_)?netrom_[a-z_]+)\s*\(", src))
    assert called  # sanity: we actually found some
    missing = sorted(m for m in called if not hasattr(Transport, m))
    assert not missing, f"base Transport missing engine-called methods: {missing}"
