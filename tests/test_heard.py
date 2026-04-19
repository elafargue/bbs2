"""
tests/test_heard.py — Tests for the Heard Stations plugin.

Two layers:
  - Unit tests: HeardPlugin DB helpers against a temp SQLite DB.
  - Integration tests: full TCP session via the shared bbs_server fixture.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import aiosqlite
import pytest

from bbs.core.auth import compute_totp_code
from bbs.plugins.heard.heard import HeardPlugin, _merge_via
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle

_SECRET = b"HeardTestSecret!!" + b"\x00" * 3  # 20 bytes

HEARD_PROMPT = "Enter choice:"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_plugin(max_age_hours: int = 24) -> HeardPlugin:
    """Create and initialize a HeardPlugin backed by a fresh temp DB."""
    tmp = tempfile.mkdtemp(prefix="bbs2_heard_test_")
    db_path = str(Path(tmp) / "test.db")
    plugin = HeardPlugin()
    await plugin.initialize({"enabled": True, "max_age_hours": max_age_hours}, db_path)
    return plugin


async def _do_sysop_auth(client: BbsTestClient, db_path: str, callsign: str) -> None:
    """Set a TOTP secret in the DB and authenticate callsign to SYSOP level."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET totp_secret=?, otp_type='totp', hotp_counter=0 "
            "WHERE callsign=? COLLATE NOCASE",
            (_SECRET, callsign),
        )
        await db.commit()
    await client.sendln("A")
    await client.wait_for("OTP")
    await client.sendln(compute_totp_code(_SECRET))
    await client.wait_for(">")  # main menu redisplayed after successful auth


# ---------------------------------------------------------------------------
# Unit tests — _merge_via()
# ---------------------------------------------------------------------------

class TestMergeVia:
    def test_empty_stored_returns_incoming(self):
        assert _merge_via("", "KJOHN*,KBULN") == "KJOHN*,KBULN"

    def test_empty_incoming_returns_stored(self):
        assert _merge_via("KJOHN*,KBULN", "") == "KJOHN*,KBULN"

    def test_or_combines_stars(self):
        # First copy: KJOHN already repeated, WOODY not yet
        # Second copy: WOODY has now repeated
        result = _merge_via("KJOHN*,KBULN,WOODY,KBETH", "KJOHN*,KBULN,WOODY*,KBETH")
        assert result == "KJOHN*,KBULN,WOODY*,KBETH"

    def test_star_propagates_from_stored(self):
        result = _merge_via("KJOHN*,KBULN,WOODY*,KBETH", "KJOHN*,KBULN,WOODY,KBETH")
        assert result == "KJOHN*,KBULN,WOODY*,KBETH"

    def test_no_stars_stays_clean(self):
        assert _merge_via("KJOHN,KBULN", "KJOHN,KBULN") == "KJOHN,KBULN"

    def test_different_length_returns_incoming(self):
        # Completely different path structure — just take incoming
        result = _merge_via("KJOHN*,KBULN", "WIDE1-1*,WIDE2-1,EXTRA")
        assert result == "WIDE1-1*,WIDE2-1,EXTRA"

    def test_different_callsigns_returns_incoming(self):
        result = _merge_via("KJOHN*,KBULN", "WIDE1*,WIDE2")
        assert result == "WIDE1*,WIDE2"

    def test_single_entry_or(self):
        assert _merge_via("KJOHN", "KJOHN*") == "KJOHN*"
        assert _merge_via("KJOHN*", "KJOHN") == "KJOHN*"


# ---------------------------------------------------------------------------
# Unit tests — HeardPlugin.on_heard()
# ---------------------------------------------------------------------------

class TestOnHeard:
    async def test_multiple_receptions_merge_stars(self):
        """Two copies of the same frame received via different digipath states
        should accumulate all * flags in heard_stations.via."""
        plugin = await _make_plugin()
        now = int(time.time())
        # First copy heard via KJOHN (already repeated), WOODY not yet
        await plugin.on_heard("KF6ANX", "BEACON",
                               ["KJOHN*", "KBULN", "WOODY", "KBETH"], now, "agwpe")
        # Second copy heard after WOODY also repeated
        await plugin.on_heard("KF6ANX", "BEACON",
                               ["KJOHN*", "KBULN", "WOODY*", "KBETH"], now + 1, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT via FROM heard_stations WHERE callsign='KF6ANX'"
            )).fetchone()
        assert row[0] == "KJOHN*,KBULN,WOODY*,KBETH"

    async def test_inserts_new_station(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W1AW", "APRS", [], now, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM heard_stations WHERE callsign='W1AW'"
            )).fetchone()
        assert row is not None
        assert row["dest"] == "APRS"
        assert row["transport"] == "agwpe"
        assert row["count"] == 1
        assert row["via"] == ""

    async def test_upsert_increments_count(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W1AW", "APRS", [], now, "agwpe")
        await plugin.on_heard("W1AW", "APRS", [], now + 10, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count FROM heard_stations WHERE callsign='W1AW'"
            )).fetchone()
        assert row[0] == 2

    async def test_stores_via_string(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W6ELA", "APRS", ["WIDE1-1*", "WIDE2-1"], now, "kiss")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT via FROM heard_stations WHERE callsign='W6ELA'"
            )).fetchone()
        assert row[0] == "WIDE1-1*,WIDE2-1"

    async def test_via_inserts_heard_paths_row(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W6ELA", "APRS", ["WIDE1-1*", "WIDE2-1"], now, "kiss")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT via, via_base, count FROM heard_paths WHERE callsign='W6ELA'"
            )).fetchone()
        assert row is not None
        assert row[0] == "WIDE1-1*,WIDE2-1"   # OR'd starred path
        assert row[1] == "WIDE1-1,WIDE2-1"    # normalised base
        assert row[2] == 1

    async def test_direct_frame_creates_direct_path_row(self):
        """An empty-via frame is a direct reception and must create a
        heard_paths row with via_base='' so the display can show 'direct'."""
        plugin = await _make_plugin()
        await plugin.on_heard("W6ELA", "APRS", [], int(time.time()), "kiss")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT via_base, via FROM heard_paths WHERE callsign='W6ELA'"
            )).fetchone()
        assert row is not None
        assert row[0] == ""  # direct: via_base is empty
        assert row[1] == ""  # direct: via is empty

    async def test_no_star_via_creates_direct_path_row(self):
        """Via path with no '*' means the BBS heard it directly before any
        digipeater repeated the frame.  Must create a direct path row."""
        plugin = await _make_plugin()
        await plugin.on_heard("W6ELA", "APRS", ["WOODY", "WIDE2-1"], int(time.time()), "kiss")

        async with aiosqlite.connect(plugin._db_path) as db:
            rows = await (await db.execute(
                "SELECT via_base FROM heard_paths WHERE callsign='W6ELA'"
            )).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == ""  # filed as direct, not as via-WOODY

    async def test_direct_and_digi_creates_two_path_rows(self):
        """Direct copy first (Via WOODY, no *), then relayed copy (Via WOODY*):
        must produce one direct row and one digi row."""
        plugin = await _make_plugin()
        now = int(time.time())
        # Direct: BBS heard W6OAK before WOODY relayed it.
        await plugin.on_heard("W6OAK", "BEACON", ["WOODY"], now, "agwpe")
        # Relayed: WOODY now has H-bit set.
        await plugin.on_heard("W6OAK", "BEACON", ["WOODY*"], now + 1, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            rows = await (await db.execute(
                "SELECT via_base FROM heard_paths WHERE callsign='W6OAK' ORDER BY via_base"
            )).fetchall()
        bases = [r[0] for r in rows]
        assert "" in bases       # direct path
        assert "WOODY" in bases  # digi path
        assert len(rows) == 2

    async def test_star_via_does_not_create_direct_row(self):
        """When at least one digi has '*', the frame was relayed — no direct row."""
        plugin = await _make_plugin()
        await plugin.on_heard("W6ELA", "APRS", ["WIDE1-1*", "WIDE2-1"], int(time.time()), "kiss")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM heard_paths WHERE callsign='W6ELA' AND via_base=''"
            )).fetchone()
        assert row[0] == 0  # no direct row

    async def test_callsign_stored_uppercase(self):
        plugin = await _make_plugin()
        await plugin.on_heard("w6ela", "aprs", [], int(time.time()), "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT callsign FROM heard_stations WHERE callsign='W6ELA'"
            )).fetchone()
        assert row is not None

    async def test_same_base_path_merges_stars_in_heard_paths(self):
        """Two receptions of the same base path must produce ONE heard_paths row
        with OR'd stars, not two separate rows."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("KF6ANX", "BEACON",
                               ["KJOHN*", "KBULN", "WOODY", "KBETH"], now, "agwpe")
        await plugin.on_heard("KF6ANX", "BEACON",
                               ["KJOHN*", "KBULN", "WOODY*", "KBETH"], now + 1, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            rows = await (await db.execute(
                "SELECT via_base, via FROM heard_paths WHERE callsign='KF6ANX'"
            )).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "KJOHN,KBULN,WOODY,KBETH"   # one base path
        assert rows[0][1] == "KJOHN*,KBULN,WOODY*,KBETH" # both stars OR'd

    async def test_different_base_path_creates_separate_heard_paths_row(self):
        """When a station is heard via a completely different digipath, a new
        heard_paths row must be created."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W1AW", "APRS", ["WIDE1-1*", "WIDE2-1"], now, "agwpe")
        await plugin.on_heard("W1AW", "APRS", ["KJOHN*", "KBERR"], now + 60, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            rows = await (await db.execute(
                "SELECT via_base FROM heard_paths WHERE callsign='W1AW' ORDER BY via_base"
            )).fetchall()
        bases = [r[0] for r in rows]
        assert "WIDE1-1,WIDE2-1" in bases
        assert "KJOHN,KBERR" in bases
        assert len(rows) == 2

    async def test_paths_upsert_increments_path_count(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W1AW", "APRS", ["WIDE1-1*"], now, "agwpe")
        await plugin.on_heard("W1AW", "APRS", ["WIDE1-1*"], now + 60, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count FROM heard_paths WHERE callsign='W1AW'"
            )).fetchone()
        assert row[0] == 2

    async def test_different_paths_create_separate_rows(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W1AW", "APRS", ["WIDE1-1*"], now, "agwpe")
        await plugin.on_heard("W1AW", "APRS", ["KD6XYZ-3*", "WIDE2-1"], now + 60, "agwpe")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM heard_paths WHERE callsign='W1AW'"
            )).fetchone()
        assert row[0] == 2


# ---------------------------------------------------------------------------
# Unit tests — HeardPlugin._prune()
# ---------------------------------------------------------------------------

class TestPrune:
    async def test_prune_removes_old_entries(self):
        plugin = await _make_plugin(max_age_hours=1)
        old_ts = int(time.time()) - 7200   # 2 hours ago
        new_ts = int(time.time())
        await plugin.on_heard("W1OLD", "APRS", [], old_ts, "agwpe")
        await plugin.on_heard("W1NEW", "APRS", [], new_ts, "agwpe")

        removed = await plugin._prune()
        assert removed == 1

        async with aiosqlite.connect(plugin._db_path) as db:
            calls = [r[0] for r in await (await db.execute(
                "SELECT callsign FROM heard_stations"
            )).fetchall()]
        assert "W1NEW" in calls
        assert "W1OLD" not in calls

    async def test_prune_zero_max_age_keeps_all(self):
        plugin = await _make_plugin(max_age_hours=0)
        old_ts = int(time.time()) - 100_000
        await plugin.on_heard("W1OLD", "APRS", [], old_ts, "agwpe")

        removed = await plugin._prune()
        assert removed == 0

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM heard_stations"
            )).fetchone()
        assert row[0] == 1

    async def test_prune_also_clears_heard_paths(self):
        plugin = await _make_plugin(max_age_hours=1)
        old_ts = int(time.time()) - 7200
        await plugin.on_heard("W1OLD", "APRS", ["WIDE1-1*"], old_ts, "agwpe")

        await plugin._prune()

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM heard_paths"
            )).fetchone()
        assert row[0] == 0


# ---------------------------------------------------------------------------
# Unit tests — HeardPlugin._clear()
# ---------------------------------------------------------------------------

class TestClear:
    async def test_clear_removes_all_stations(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W1AA", "APRS", [], now, "agwpe")
        await plugin.on_heard("W2BB", "APRS", [], now, "agwpe")

        removed = await plugin._clear()
        assert removed == 2

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM heard_stations"
            )).fetchone()
        assert row[0] == 0

    async def test_clear_removes_heard_paths(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard("W1AA", "APRS", ["WIDE1-1*"], now, "agwpe")

        await plugin._clear()

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM heard_paths"
            )).fetchone()
        assert row[0] == 0

    async def test_clear_idempotent_when_empty(self):
        plugin = await _make_plugin()
        removed = await plugin._clear()
        assert removed == 0


# ---------------------------------------------------------------------------
# Integration tests — BBS session (TCP)
# ---------------------------------------------------------------------------

class TestHeardMenu:
    async def test_enter_heard_shows_header(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("H")
        text = await logged_in_client.wait_for("HEARD STATIONS")
        assert "HEARD STATIONS" in text

    async def test_heard_menu_shows_quit_option(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("H")
        text = await logged_in_client.wait_for(HEARD_PROMPT)
        assert "[Q]" in text or "Quit" in text

    async def test_quit_returns_to_main_menu(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("H")
        await logged_in_client.wait_for(HEARD_PROMPT)
        await logged_in_client.sendln("Q")
        text = await logged_in_client.wait_for(">")
        assert "[B]" in text or "Bye" in text


class TestNonSysopAccess:
    async def test_configure_not_in_menu_for_regular_user(
        self, logged_in_client: BbsTestClient
    ):
        """A non-sysop must not see [C] Configure in the heard menu."""
        await logged_in_client.sendln("H")
        text = await logged_in_client.wait_for(HEARD_PROMPT)
        assert "[C]" not in text
        await logged_in_client.sendln("Q")
        await logged_in_client.wait_for(">")

    async def test_configure_key_ignored_for_regular_user(
        self, logged_in_client: BbsTestClient
    ):
        """Typing C as a non-sysop must redisplay the log, not open configure."""
        await logged_in_client.sendln("H")
        await logged_in_client.wait_for(HEARD_PROMPT)
        await logged_in_client.sendln("C")
        # The loop redisplays; wait for the menu prompt again
        text = await logged_in_client.wait_for(HEARD_PROMPT)
        assert "configure" not in text.lower()
        assert "max age" not in text.lower()
        await logged_in_client.sendln("Q")
        await logged_in_client.wait_for(">")


class TestSysopAccess:
    async def test_configure_appears_in_menu_for_sysop(
        self, bbs_server: _BbsServerHandle
    ):
        """A sysop must see [C] Configure in the heard menu."""
        sysop_call = bbs_server.engine.cfg.callsign.upper()
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(sysop_call)
            await _do_sysop_auth(c, str(bbs_server.engine.cfg.db_path), sysop_call)
            await c.sendln("H")
            text = await c.wait_for(HEARD_PROMPT)
            assert "[C]" in text

    async def test_configure_key_opens_sub_menu_for_sysop(
        self, bbs_server: _BbsServerHandle
    ):
        """Typing C as sysop must open the configure sub-menu."""
        sysop_call = bbs_server.engine.cfg.callsign.upper()
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(sysop_call)
            await _do_sysop_auth(c, str(bbs_server.engine.cfg.db_path), sysop_call)
            await c.sendln("H")
            await c.wait_for(HEARD_PROMPT)
            await c.sendln("C")
            # Wait for the configure sub-menu prompt (after all items are printed)
            text = await c.wait_for(HEARD_PROMPT)
            assert "[A]" in text
            assert "[X]" in text

    async def test_sysop_configure_back_returns_to_heard_menu(
        self, bbs_server: _BbsServerHandle
    ):
        """Q in the configure sub-menu must return to the heard menu."""
        sysop_call = bbs_server.engine.cfg.callsign.upper()
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(sysop_call)
            await _do_sysop_auth(c, str(bbs_server.engine.cfg.db_path), sysop_call)
            await c.sendln("H")
            await c.wait_for(HEARD_PROMPT)
            await c.sendln("C")
            await c.wait_for("CONFIGURE")
            await c.sendln("Q")  # back from configure
            text = await c.wait_for(HEARD_PROMPT)
            assert "HEARD" in text
