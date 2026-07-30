"""
tests/test_services_engine.py — engine-level ax25d dispatch wiring.

Verifies BBSEngine._on_connection routes EXEC → external service, REFUSE →
close, PASS → internal BBS, and that _run_external_service enforces the
service-session cap.
"""
from __future__ import annotations

import asyncio
import sys
import time

from bbs.config import BBSConfig
from bbs.core.engine import BBSEngine
from bbs.services.dispatcher import ServiceDispatcher, ServiceRoute
from bbs.transport.base import Connection

_ECHO = (
    "import sys\n"
    "while True:\n"
    "    line = sys.stdin.buffer.readline()\n"
    "    if not line: break\n"
    "    sys.stdout.buffer.write(line); sys.stdout.buffer.flush()\n"
)


async def _wait_until(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _cfg(services: dict) -> BBSConfig:
    return BBSConfig(
        callsign="W6ELA", ssid=1, name="Test", sysop="W6ELA", location="",
        max_users=10, idle_timeout=60, write_timeout=30,
        path_length_medium_hops=1, path_length_long_hops=3,
        transports={}, database={"path": ":memory:"}, auth={}, plugins={},
        web={}, logging={}, netrom={}, services=services,
    )


def _engine(services: dict) -> BBSEngine:
    eng = BBSEngine(_cfg(services))
    eng._services = ServiceDispatcher(services)
    return eng


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def _conn(remote: str = "KN6PE-7", local: str = "W6ELA-2", hop: int = 0) -> Connection:
    return Connection(
        remote_addr=remote,
        reader=object(),          # type: ignore[arg-type]
        writer=_FakeWriter(),     # type: ignore[arg-type]
        transport_id="agwpe",
        hop_count=hop,
        local_addr=local,
    )


_SVC = {
    "enabled": True,
    "lockout": ["NOCALL"],
    "max_sessions": 2,
    "routes": {"W6ELA-2": {"exec": "/bin/cat", "args": ["cat"]}},
}


async def test_exec_route_goes_to_service(monkeypatch):
    eng = _engine(_SVC)
    seen: list[str] = []

    async def spy(conn, route):
        seen.append(route.called)

    monkeypatch.setattr(eng, "_run_external_service", spy)
    await eng._on_connection(_conn(local="W6ELA-2"))
    assert seen == ["W6ELA-2"]
    assert eng._sessions == {}          # no internal BBS session created


async def test_no_route_falls_through_to_bbs(monkeypatch):
    eng = _engine(_SVC)
    svc: list = []
    bbs: list = []

    async def svc_spy(conn, route):
        svc.append(route)

    async def bbs_spy(session):
        bbs.append(session)

    monkeypatch.setattr(eng, "_run_external_service", svc_spy)
    monkeypatch.setattr(eng, "_run_session", bbs_spy)
    await eng._on_connection(_conn(local="W6ELA-1"))   # BBS SSID, no route
    assert svc == []
    assert len(bbs) == 1                # fell through to the internal BBS


async def test_refuse_closes_connection(monkeypatch):
    eng = _engine(_SVC)

    async def svc_spy(conn, route):
        raise AssertionError("locked-out caller must not reach the service")

    monkeypatch.setattr(eng, "_run_external_service", svc_spy)
    conn = _conn(remote="NOCALL", local="W6ELA-2")     # lockout list
    await eng._on_connection(conn)
    assert conn.writer.closed           # connection closed
    assert eng._sessions == {}          # and no BBS session


async def test_max_sessions_cap_refuses(monkeypatch):
    eng = _engine(_SVC)
    eng._service_sessions = {"a": None, "b": None}      # already at cap (2)
    conn = _conn(local="W6ELA-2")
    route = ServiceRoute(called="W6ELA-2", exec_path="/bin/cat", args=["cat"])
    await eng._run_external_service(conn, route)
    assert conn.writer.closed
    assert len(eng._service_sessions) == 2              # unchanged; nothing spawned


def test_reload_services_rebuilds_dispatcher():
    eng = BBSEngine(_cfg({}))                 # starts with no services
    eng.reload_services()
    assert eng._services is not None and not eng._services.enabled
    # Update the in-memory config and hot-reload.
    eng.cfg.services = {
        "enabled": True,
        "routes": {"W6ELA-2": {"exec": "/bin/cat", "args": ["cat"]}},
    }
    eng.reload_services()
    assert eng._services.enabled
    assert "W6ELA-2" in eng._services.route_callsigns()


async def test_run_external_service_end_to_end():
    eng = _engine(_SVC)
    reader = asyncio.StreamReader()
    writer = _FakeWriter()
    conn = Connection(
        remote_addr="KN6PE-7", reader=reader, writer=writer,   # type: ignore[arg-type]
        transport_id="agwpe", local_addr="W6ELA-2",
    )
    route = ServiceRoute(
        called="W6ELA-2", exec_path=sys.executable,
        args=["echoprog", "-c", _ECHO], quiet=True,   # quiet skips DB journaling
    )
    task = asyncio.create_task(eng._run_external_service(conn, route))
    reader.feed_data(b"hey\n")
    await _wait_until(lambda: b"hey\n" in bytes(writer.buffer))
    assert len(eng._service_sessions) == 1            # tracked while running
    reader.feed_eof()
    await asyncio.wait_for(task, timeout=10)
    assert eng._service_sessions == {}                # cleaned up on exit
    assert writer.closed
