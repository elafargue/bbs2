from __future__ import annotations

import asyncio
import sqlite3

from tests.conftest import _BbsServerHandle


async def _read_until(
    reader: asyncio.StreamReader,
    pattern: str,
    timeout: float = 10.0,
) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    buf = b""
    encoded = pattern.encode("ascii")

    while encoded not in buf:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError(
                f"Timed out waiting for {pattern!r}. Buffer so far: {buf!r}"
            )
        chunk = await asyncio.wait_for(reader.read(4096), timeout=min(0.2, remaining))
        if not chunk:
            raise AssertionError(
                f"Connection closed while waiting for {pattern!r}. Buffer so far: {buf!r}"
            )
        buf += chunk

    return buf.decode("ascii", errors="replace")


async def _login_raw(
    host: str,
    port: int,
    callsign: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    reader, writer = await asyncio.open_connection(host, port)
    await _read_until(reader, "Callsign:")
    writer.write(f"{callsign}\r\n".encode("ascii"))
    await writer.drain()
    text = await _read_until(reader, ">")
    return reader, writer, text


async def _set_color_mode(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    key: str,
) -> str:
    writer.write(b"CO\r\n")
    await writer.drain()
    await _read_until(reader, "Selection")
    writer.write(f"{key}\r\n".encode("ascii"))
    await writer.drain()
    return await _read_until(reader, ">")


async def _disconnect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        writer.write(b"B\r\n")
        await writer.drain()
        await _read_until(reader, "73")
    finally:
        writer.close()
        await writer.wait_closed()


class TestColorModes:
    async def test_color_command_persists_ansi16_mode(self, bbs_server: _BbsServerHandle):
        callsign = "W1COL16"
        reader, writer, _ = await _login_raw(bbs_server.host, bbs_server.port, callsign)
        text = await _set_color_mode(reader, writer, "A")
        assert "ANSI 16-color" in text
        await _disconnect(reader, writer)

        db_path = f"{bbs_server.tmp_dir}/test.db"
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT color_mode FROM users WHERE callsign = ?",
                (callsign,),
            ).fetchone()
        assert row is not None
        assert row[0] == "ansi16"

        reader, writer, text = await _login_raw(bbs_server.host, bbs_server.port, callsign)
        try:
            assert "\x1b[96m" in text
            assert "\x1b[38;2;" not in text
        finally:
            await _disconnect(reader, writer)

    async def test_color_command_persists_truecolor_mode(self, bbs_server: _BbsServerHandle):
        callsign = "W1COL24"
        reader, writer, _ = await _login_raw(bbs_server.host, bbs_server.port, callsign)
        text = await _set_color_mode(reader, writer, "T")
        assert "truecolor" in text.lower()
        await _disconnect(reader, writer)

        reader, writer, text = await _login_raw(bbs_server.host, bbs_server.port, callsign)
        try:
            assert "\x1b[38;2;" in text or "\x1b[48;2;" in text
        finally:
            await _disconnect(reader, writer)