"""
tests/conftest.py — Shared pytest fixtures for BBS integration tests.

The ``bbs_server`` session-scoped fixture spins up a full BBS engine on a
random free TCP port backed by a temporary SQLite database.  It tears down
cleanly after the test session.

Every test can also request a ``bbs_client`` function-scoped fixture that
opens a fresh TCP connection for that test alone.
"""
from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio

from bbs.config import BBSConfig
from bbs.core.engine import BBSEngine
from tests.client import BbsTestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_test_config(db_path: str, tcp_port: int) -> BBSConfig:
    """Build a minimal BBSConfig suitable for testing."""
    return BBSConfig(
        callsign="W1TEST",
        ssid=0,
        name="Test BBS",
        sysop="W1TEST",
        location="Test Lab",
        max_users=10,
        idle_timeout=60,
        transports={
            "tcp": {"enabled": True, "host": "127.0.0.1", "port": tcp_port},
            # All other transports disabled
            "kernel_ax25": {"enabled": False},
            "kiss_tcp":    {"enabled": False},
            "kiss_serial": {"enabled": False},

        },
        database={"path": db_path},
        auth={
            "nonce_hex_length": 32,
            "max_attempts": 3,
            "lockout_seconds": 60,
        },
        plugins={
            "bulletins": {
                "enabled": True,
                "default_areas": [
                    {"name": "GENERAL", "description": "General discussion"},
                    {"name": "TECH",    "description": "Technical topics"},
                ],
                "max_body_bytes": 4096,
                "max_subject_chars": 25,
            },
            "chat": {
                "enabled": True,
                "default_rooms": [{"name": "main", "description": "Main chat room"}],
                "history_lines": 10,
            },
            "heard": {
                "enabled": True,
                "max_age_hours": 24,
            },
        },
        web={
            "host": "127.0.0.1",
            "port": 0,
            "secret_key": "test-secret",
            "sysop_password_hash": "",
        },
        logging={"level": "WARNING", "file": ""},
    )


# ---------------------------------------------------------------------------
# Session-scoped BBS server fixture
# ---------------------------------------------------------------------------

class _BbsServerHandle:
    """Holds references to the running test BBS."""

    def __init__(self, engine: BBSEngine, port: int, tmp_dir: str) -> None:
        self.engine = engine
        self.port = port
        self.tmp_dir = tmp_dir
        self.host = "127.0.0.1"


@pytest.fixture(scope="session")
def bbs_server() -> Generator[_BbsServerHandle, None, None]:
    """
    Start a BBS engine in a background event loop thread.  Yields a handle
    with ``.host`` and ``.port`` for connecting.  Tears down after all tests.
    """
    tmp = tempfile.mkdtemp(prefix="bbs2_test_")
    db_path = str(Path(tmp) / "test.db")
    port = _free_port()
    cfg = _make_test_config(db_path, port)

    engine = BBSEngine(cfg)
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    error: list[Exception] = []

    def _run() -> None:
        asyncio.set_event_loop(loop)

        async def _start() -> None:
            try:
                # Patch engine.run() to signal readiness once transports are up
                original_run = engine.run

                async def _patched_run() -> None:
                    from bbs.db.schema import init_db
                    from bbs.transport import build_transports

                    # Must be created here (inside the running loop) to avoid
                    # "Future attached to a different loop" on Python 3.9
                    engine._stop_event = asyncio.Event()

                    await init_db(str(cfg.db_path))
                    await engine.plugin_registry.load_plugins()

                    transports = build_transports(cfg)
                    transport_tasks = [
                        asyncio.create_task(
                            t.start(engine._on_connection),
                            name=f"transport:{t.transport_id}",
                        )
                        for t in transports
                    ]

                    engine._emit_log(f"BBS {cfg.full_callsign} online (test mode)")
                    ready.set()  # signal that the server is accepting connections

                    await engine._stop_event.wait()

                    for task in transport_tasks:
                        task.cancel()
                    for task in list(engine._session_tasks.values()):
                        task.cancel()
                    for plugin in engine.plugin_registry:
                        try:
                            await plugin.shutdown()
                        except Exception:
                            pass

                await _patched_run()
            except Exception as exc:
                error.append(exc)
                ready.set()

        loop.run_until_complete(_start())

    thread = threading.Thread(target=_run, name="bbs-test-engine", daemon=True)
    thread.start()

    ready.wait(timeout=10)
    if error:
        raise RuntimeError(f"BBS test server failed to start: {error[0]}") from error[0]

    handle = _BbsServerHandle(engine, port, tmp)
    yield handle

    # Teardown
    loop.call_soon_threadsafe(engine.stop)
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Function-scoped async client fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def bbs_client(bbs_server: _BbsServerHandle) -> AsyncGenerator[BbsTestClient, None]:
    """
    Open a fresh BBS TCP connection for a single test.
    Automatically closes after the test completes.
    """
    client = BbsTestClient(bbs_server.host, bbs_server.port)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def logged_in_client(bbs_client: BbsTestClient) -> BbsTestClient:
    """A client that has already completed callsign identification."""
    await bbs_client.do_login("W1TEST")
    return bbs_client
