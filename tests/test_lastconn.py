"""
tests/test_lastconn.py — Tests for the Last Connections plugin and its DB helpers.

Two layers:
  - Unit tests: bbs.db.connections helpers directly against a temp SQLite DB.
  - Integration tests: full TCP session via the shared bbs_server fixture.
"""
from __future__ import annotations

import time
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from bbs.db.schema import init_db
from bbs.db.connections import (
    get_recent_connections,
    prune_old_connections,
    upsert_connection,
)
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_db() -> str:
    """Create a fresh temp DB and return its path string."""
    tmp = tempfile.mkdtemp(prefix="bbs2_lc_test_")
    db_path = str(Path(tmp) / "test.db")
    await init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Unit tests — bbs.db.connections
# ---------------------------------------------------------------------------

class TestUpsertConnection:
    async def test_first_insert_creates_row(self):
        db = await _make_db()
        now = time.time()
        await upsert_connection(db, "W1AW", "tcp", now, 1)

        rows = await get_recent_connections(db, days=30)
        assert len(rows) == 1
        assert rows[0]["callsign"] == "W1AW"
        assert rows[0]["transport"] == "tcp"
        assert rows[0]["auth_level"] == 1
        assert rows[0]["connected"] == 0

    async def test_callsign_stored_uppercase(self):
        db = await _make_db()
        await upsert_connection(db, "w1aw", "tcp", time.time(), 1)
        rows = await get_recent_connections(db, days=30)
        assert rows[0]["callsign"] == "W1AW"

    async def test_first_seen_not_overwritten_on_update(self):
        db = await _make_db()
        t0 = time.time() - 1000
        await upsert_connection(db, "W1AW", "tcp", t0, 1)
        await upsert_connection(db, "W1AW", "tcp", time.time(), 1)

        rows = await get_recent_connections(db, days=30)
        assert rows[0]["first_seen"] == int(t0)

    async def test_auth_level_promoted_to_highest(self):
        db = await _make_db()
        await upsert_connection(db, "W1AW", "tcp", time.time(), 1)
        await upsert_connection(db, "W1AW", "tcp", time.time(), 3)
        await upsert_connection(db, "W1AW", "tcp", time.time(), 2)

        rows = await get_recent_connections(db, days=30)
        assert rows[0]["auth_level"] == 3

    async def test_connected_flag_set_and_cleared(self):
        db = await _make_db()
        await upsert_connection(db, "W1AW", "tcp", time.time(), 1, connected=1)

        rows = await get_recent_connections(db, days=30)
        assert rows[0]["connected"] == 1

        await upsert_connection(db, "W1AW", "tcp", time.time(), 1, connected=0)
        rows = await get_recent_connections(db, days=30)
        assert rows[0]["connected"] == 0

    async def test_transport_updated_on_reconnect(self):
        db = await _make_db()
        await upsert_connection(db, "W1AW", "tcp", time.time(), 1)
        await upsert_connection(db, "W1AW", "agwpe", time.time(), 1)

        rows = await get_recent_connections(db, days=30)
        assert rows[0]["transport"] == "agwpe"


class TestGetRecentConnections:
    async def test_respects_days_cutoff(self):
        db = await _make_db()
        old_time = time.time() - 40 * 86400  # 40 days ago
        await upsert_connection(db, "W1OLD", "tcp", old_time, 1)
        # Force last_seen back in time directly
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "UPDATE connection_log SET last_seen = ? WHERE callsign = 'W1OLD'",
                (int(old_time),),
            )
            await conn.commit()

        rows = await get_recent_connections(db, days=30)
        callsigns = [r["callsign"] for r in rows]
        assert "W1OLD" not in callsigns

    async def test_active_station_bypasses_cutoff(self):
        """A connected=1 station appears even if last_seen is older than the cutoff."""
        db = await _make_db()
        old_time = time.time() - 40 * 86400
        await upsert_connection(db, "W1OLD", "tcp", old_time, 1, connected=1)
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "UPDATE connection_log SET last_seen = ? WHERE callsign = 'W1OLD'",
                (int(old_time),),
            )
            await conn.commit()

        rows = await get_recent_connections(db, days=30)
        callsigns = [r["callsign"] for r in rows]
        assert "W1OLD" in callsigns

    async def test_active_stations_sort_first(self):
        db = await _make_db()
        t = time.time()
        await upsert_connection(db, "W1LATER", "tcp", t, 1, connected=0)
        await upsert_connection(db, "W1ACTIVE", "tcp", t - 100, 1, connected=1)

        rows = await get_recent_connections(db, days=30)
        assert rows[0]["callsign"] == "W1ACTIVE"

    async def test_limit_is_respected(self):
        db = await _make_db()
        for i in range(10):
            await upsert_connection(db, f"W{i}TST", "tcp", time.time(), 1)

        rows = await get_recent_connections(db, days=30, limit=5)
        assert len(rows) == 5

    async def test_empty_when_no_records(self):
        db = await _make_db()
        rows = await get_recent_connections(db, days=30)
        assert rows == []


class TestPruneOldConnections:
    async def test_prunes_records_older_than_cutoff(self):
        db = await _make_db()
        old_time = time.time() - 40 * 86400
        await upsert_connection(db, "W1OLD", "tcp", old_time, 1)
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "UPDATE connection_log SET last_seen = ? WHERE callsign = 'W1OLD'",
                (int(old_time),),
            )
            await conn.commit()

        await prune_old_connections(db, days=30)
        rows = await get_recent_connections(db, days=365)
        assert all(r["callsign"] != "W1OLD" for r in rows)

    async def test_keeps_recent_records(self):
        db = await _make_db()
        await upsert_connection(db, "W1NEW", "tcp", time.time(), 1)

        await prune_old_connections(db, days=30)
        rows = await get_recent_connections(db, days=30)
        assert any(r["callsign"] == "W1NEW" for r in rows)


class TestStartupClearsConnectedFlags:
    async def test_init_db_resets_stale_connected_flags(self):
        """init_db clears connected=1 flags left by an unclean shutdown."""
        db = await _make_db()
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "INSERT INTO connection_log (callsign, transport, first_seen, last_seen, auth_level, connected)"
                " VALUES ('W1STALE', 'tcp', ?, ?, 1, 1)",
                (int(time.time()), int(time.time())),
            )
            await conn.commit()

        # Re-run init_db as if the BBS restarted
        await init_db(db)

        rows = await get_recent_connections(db, days=30)
        stale = next(r for r in rows if r["callsign"] == "W1STALE")
        assert stale["connected"] == 0


# ---------------------------------------------------------------------------
# Integration tests — lastconn plugin via full TCP session
# ---------------------------------------------------------------------------

class TestLastConnIntegration:
    async def test_no_connections_message(self, bbs_server: _BbsServerHandle):
        """Fresh DB shows a 'no connections' message until someone logs in."""
        # Use a second connection to check — the fixture's shared server already
        # has sessions from other tests, so just verify the command is accessible.
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login("W1LC")
            await c.sendln("LC")
            text = await c.read_all()
        # Either the header or the "no connections" message should appear
        assert "LAST CONNECTIONS" in text or "No connections" in text

    async def test_own_callsign_appears_after_login(self, bbs_server: _BbsServerHandle):
        """After logging in, the user's callsign should appear in LC as Active."""
        callsign = "W1LCSHOW"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await c.sendln("LC")
            text = await c.read_all()
        assert callsign in text.upper()

    async def test_active_marker_shown_while_connected(self, bbs_server: _BbsServerHandle):
        """A currently-connected station should show Active in the LC listing."""
        callsign = "W1ACTIVE"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            # Open a second connection to query LC while the first is still live
            async with BbsTestClient(bbs_server.host, bbs_server.port) as observer:
                await observer.do_login("W1OBS")
                await observer.sendln("LC")
                text = await observer.read_all()
            # W1ACTIVE is still connected at this point
        assert "Active" in text

    async def test_callsign_present_after_disconnect(self, bbs_server: _BbsServerHandle):
        """After a session ends, the callsign persists in the journal."""
        callsign = "W1GONE"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
        # Session closed — now query from another client
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c2:
            await c2.do_login("W1QUERY")
            await c2.sendln("LC")
            text = await c2.read_all()
        assert callsign in text.upper()
