"""
tests/test_heard_paths.py — characterization ("golden") tests for the finicky
path-proving logic that the station-identity refactor must NOT regress:

  • _merge_via()  — OR-merge of the H-bit (*) flags across repeated receptions.
  • is_direct / via_base derivation in on_heard() that populates heard_paths
    (direct rows use via_base=''; relayed rows keep the SSID-stripped base and
    the starred `via` string).

These pin the CURRENT behavior; they are the regression tripwire and must stay
green through every later milestone. (confirmed_edges is already covered by
tests/test_heard_graph.py::TestConfirmedEdges.)
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from bbs.plugins.heard.heard import HeardPlugin, _merge_via


# ── _merge_via — OR-merge of the has-been-repeated (*) flag ────────────────────

class TestMergeVia:
    def test_empty_stored_returns_incoming(self):
        assert _merge_via("", "KJOHN*,KBULN") == "KJOHN*,KBULN"

    def test_empty_incoming_returns_stored(self):
        assert _merge_via("KJOHN*,KBULN", "") == "KJOHN*,KBULN"

    def test_or_merges_star_flags(self):
        assert _merge_via(
            "KJOHN*,KBULN,WOODY,KBETH",
            "KJOHN*,KBULN,WOODY*,KBETH",
        ) == "KJOHN*,KBULN,WOODY*,KBETH"

    def test_or_merges_both_directions(self):
        assert _merge_via("KJOHN*,KBULN", "KJOHN,KBULN*") == "KJOHN*,KBULN*"

    def test_different_length_returns_incoming(self):
        assert _merge_via("A*,B", "A*,B,C") == "A*,B,C"

    def test_different_chain_returns_incoming(self):
        assert _merge_via("A*,B", "A*,C") == "A*,C"

    def test_whitespace_tolerant(self):
        assert _merge_via("A* , B", "A , B*") == "A*,B*"


# ── is_direct / via_base derivation (through on_heard → heard_paths) ───────────

async def _plugin() -> HeardPlugin:
    tmp = tempfile.mkdtemp(prefix="bbs2_paths_test_")
    plugin = HeardPlugin()
    await plugin.initialize(
        {"enabled": True, "max_age_hours": 0}, str(Path(tmp) / "test.db")
    )
    return plugin


def _paths(plugin: HeardPlugin, callsign: str) -> list[tuple]:
    db = sqlite3.connect(plugin._db_path)
    try:
        return db.execute(
            "SELECT via_base, via, count FROM heard_paths"
            " WHERE callsign=? ORDER BY via_base",
            (callsign.upper(),),
        ).fetchall()
    finally:
        db.close()


def _station(plugin: HeardPlugin, callsign: str, cols: str) -> tuple:
    db = sqlite3.connect(plugin._db_path)
    try:
        return db.execute(
            f"SELECT {cols} FROM stations WHERE callsign=?", (callsign.upper(),)
        ).fetchone()
    finally:
        db.close()


def _entity_count(plugin: HeardPlugin) -> int:
    db = sqlite3.connect(plugin._db_path)
    try:
        return db.execute("SELECT COUNT(*) FROM station_entities").fetchone()[0]
    finally:
        db.close()


class TestEntityIngest:
    async def test_new_station_gets_base_call_and_entity(self):
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("W6ELA-5", "BEACON", [], now, "agwpe", info="node")
        assert _station(p, "W6ELA-5", "base_call") == ("W6ELA",)
        assert _entity_count(p) == 1

    async def test_ssids_of_one_call_share_one_entity(self):
        p = await _plugin()
        now = int(time.time())
        for call in ("W6ELA-1", "W6ELA-5", "K6XX-2"):
            await p.on_heard(call, "BEACON", [], now, "agwpe")
        # W6ELA-1 + W6ELA-5 → one entity; K6XX-2 → another.
        assert _entity_count(p) == 2
        assert _station(p, "W6ELA-1", "base_call") == ("W6ELA",)
        assert _station(p, "W6ELA-5", "base_call") == ("W6ELA",)


class TestLastBeaconText:
    async def test_stores_latest_beacon_text(self):
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("K6ZZ", "BEACON", [], now, "agwpe", info="K6ZZ Palo Alto BBS")
        assert _station(p, "K6ZZ", "last_beacon_text, last_beacon_ts") == (
            "K6ZZ Palo Alto BBS", now,
        )

    async def test_latest_wins_and_empty_is_ignored(self):
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("K6ZZ", "BEACON", [], now, "agwpe", info="first")
        await p.on_heard("K6ZZ", "BEACON", [], now + 1, "agwpe", info="   ")  # blank: no overwrite
        await p.on_heard("K6ZZ", "BEACON", [], now + 2, "agwpe", info="second")
        assert _station(p, "K6ZZ", "last_beacon_text, last_beacon_ts") == ("second", now + 2)

    async def test_beacon_text_is_length_capped(self):
        from bbs.plugins.heard.heard import _MAX_BEACON_TEXT
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("K6ZZ", "BEACON", [], now, "agwpe", info="x" * (_MAX_BEACON_TEXT + 50))
        (text,) = _station(p, "K6ZZ", "last_beacon_text")
        assert len(text) == _MAX_BEACON_TEXT


class TestViaBaseDerivation:
    async def test_direct_frame_is_direct_row(self):
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("KF6ANX", "BEACON", [], now, "agwpe")
        assert _paths(p, "KF6ANX") == [("", "", 1)]           # direct: via_base=''

    async def test_starred_via_is_relayed_row(self):
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("W6OAK", "BEACON", ["WOODY*"], now, "agwpe")
        assert _paths(p, "W6OAK") == [("WOODY", "WOODY*", 1)]  # relayed, star kept

    async def test_via_without_star_counts_as_direct(self):
        # "Via WOODY" with no * means WOODY had not yet relayed → heard direct.
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("N6XYZ", "BEACON", ["WOODY"], now, "agwpe")
        assert _paths(p, "N6XYZ") == [("", "", 1)]             # recorded DIRECT

    async def test_multi_digi_via_base_strips_stars(self):
        p = await _plugin()
        now = int(time.time())
        await p.on_heard("N6YP", "APRS", ["KJOHN*", "KBULN"], now, "agwpe")
        assert _paths(p, "N6YP") == [("KJOHN,KBULN", "KJOHN*,KBULN", 1)]

    async def test_repeat_merges_star_flags_in_path(self):
        p = await _plugin()
        now = int(time.time())
        # Same station, same digi chain, a different digi starred each time.
        await p.on_heard("N6YP", "APRS", ["KJOHN*", "WOODY"], now, "agwpe")
        await p.on_heard("N6YP", "APRS", ["KJOHN", "WOODY*"], now + 1, "agwpe")
        # One relayed row, count 2, `via` OR-merged to both stars.
        assert _paths(p, "N6YP") == [("KJOHN,WOODY", "KJOHN*,WOODY*", 2)]
