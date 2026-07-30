"""
tests/test_services_bridge.py — the external-service subprocess bridge.

Spawns real short-lived Python children so bytes actually flow through
conn.reader → stdin and stdout → conn.writer, and teardown/reaping are
exercised end-to-end.
"""
from __future__ import annotations

import asyncio
import sys
import time

import pytest

from bbs.services.bridge import run_service
from bbs.services.dispatcher import ServiceRoute
from bbs.transport.base import Connection

# A line-echo child that flushes each line (so output arrives deterministically,
# unlike block-buffered `cat`).
_ECHO = (
    "import sys\n"
    "while True:\n"
    "    line = sys.stdin.buffer.readline()\n"
    "    if not line: break\n"
    "    sys.stdout.buffer.write(line); sys.stdout.buffer.flush()\n"
)
# Dumps its environment then exits — used to prove env is empty.
_ENVDUMP = "import os,sys; sys.stdout.buffer.write(repr(dict(os.environ)).encode()); sys.stdout.flush()"
# Sleeps without any I/O — used to exercise the idle reaper.
_SLEEP = "import time; time.sleep(30)"


class _FakeWriter:
    """Collects written bytes; satisfies the StreamWriter surface Connection uses."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        pass


def _conn() -> tuple[Connection, asyncio.StreamReader, _FakeWriter]:
    reader = asyncio.StreamReader()
    writer = _FakeWriter()
    conn = Connection(
        remote_addr="KN6PE-7",
        reader=reader,
        writer=writer,           # type: ignore[arg-type]
        transport_id="agwpe",
        local_addr="W6ELA-2",
    )
    return conn, reader, writer


def _route(prog: str, *, idle_timeout: int = 0, crlf: bool = False,
           env: dict | None = None) -> tuple[ServiceRoute, list[str]]:
    argv = ["echoprog", "-c", prog]
    route = ServiceRoute(
        called="W6ELA-2", exec_path=sys.executable, args=argv,
        idle_timeout=idle_timeout, crlf=crlf, env=env or {},
    )
    return route, argv


async def _wait_until(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


async def test_bidirectional_echo_and_teardown_on_peer_eof():
    conn, reader, writer = _conn()
    route, argv = _route(_ECHO)
    task = asyncio.create_task(run_service(conn, route, argv))

    reader.feed_data(b"ping\n")
    await _wait_until(lambda: b"ping\n" in bytes(writer.buffer))

    # Peer disconnects → bridge should close the child and finish cleanly.
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=10)
    assert writer.is_closing()  # conn closed on teardown


async def test_child_gets_minimal_env_not_parent(monkeypatch):
    # The child gets a minimal env (PATH), NOT the parent's — prove the
    # parent's sentinel does not leak while PATH is present.
    monkeypatch.setenv("BBS_SVC_SENTINEL", "leaked")
    conn, reader, writer = _conn()
    route, argv = _route(_ENVDUMP)
    await asyncio.wait_for(run_service(conn, route, argv), timeout=10)
    out = bytes(writer.buffer)
    assert b"BBS_SVC_SENTINEL" not in out and b"leaked" not in out
    assert b"PATH" in out                       # minimal env provides PATH


async def test_route_env_override(monkeypatch):
    conn, reader, writer = _conn()
    route, argv = _route(_ENVDUMP, env={"BBS_SVC_EXTRA": "xyz"})
    await asyncio.wait_for(run_service(conn, route, argv), timeout=10)
    out = bytes(writer.buffer)
    assert b"BBS_SVC_EXTRA" in out and b"xyz" in out


async def test_program_exit_ends_session():
    conn, reader, writer = _conn()
    # Child exits immediately without reading input.
    route, argv = _route("import sys; sys.exit(0)")
    await asyncio.wait_for(run_service(conn, route, argv), timeout=10)
    assert writer.is_closing()


async def test_spawn_failure_closes_connection():
    conn, reader, writer = _conn()
    route = ServiceRoute(called="W6ELA-2", exec_path="/nonexistent/prog", args=["prog"])
    await asyncio.wait_for(run_service(conn, route, ["prog"]), timeout=5)
    assert writer.is_closing()  # connection closed even though exec failed


async def test_idle_timeout_reaps_silent_session():
    conn, reader, writer = _conn()
    route, argv = _route(_SLEEP, idle_timeout=1)
    # No I/O at all: pumps block, process sleeps → idle reaper must fire ~1s.
    start = time.monotonic()
    await asyncio.wait_for(run_service(conn, route, argv), timeout=10)
    assert time.monotonic() - start < 5   # reaped promptly, not after sleep(30)
    assert writer.is_closing()


async def test_crlf_translation():
    conn, reader, writer = _conn()
    route, argv = _route(_ECHO, crlf=True)
    task = asyncio.create_task(run_service(conn, route, argv))
    # Radio sends bare-CR line; with crlf on, the child sees LF and echoes LF,
    # which is translated back to CR toward the radio.
    reader.feed_data(b"hi\r")
    await _wait_until(lambda: b"hi\r" in bytes(writer.buffer))
    assert b"\n" not in bytes(writer.buffer)  # no Unix LF leaks to the radio
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=10)
