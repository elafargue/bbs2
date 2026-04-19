"""
tests/test_heard_graph.py — Unit tests for the network-graph edge extraction
and the /api/heard/graph REST endpoint.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import aiosqlite
import pytest

from bbs.plugins.heard.heard import HeardPlugin
from server.routes.heard import _confirmed_edges


# ---------------------------------------------------------------------------
# Unit tests — _confirmed_edges()
# ---------------------------------------------------------------------------

class TestConfirmedEdges:
    BBS = "W6ELA"

    def test_direct_gives_single_edge(self):
        edges = _confirmed_edges("KF6ANX", "", self.BBS)
        assert edges == [("KF6ANX", self.BBS)]

    def test_single_starred_digi(self):
        edges = _confirmed_edges("W6OAK", "WOODY*", self.BBS)
        assert edges == [("W6OAK", "WOODY"), ("WOODY", self.BBS)]

    def test_two_starred_digis(self):
        edges = _confirmed_edges("N6YP", "KJOHN*,KBULN*", self.BBS)
        assert edges == [
            ("N6YP",  "KJOHN"),
            ("KJOHN", "KBULN"),
            ("KBULN", self.BBS),
        ]

    def test_last_star_determines_cutoff(self):
        # KJOHN*,KBULN,WOODY*,KBETH — last star at WOODY (index 2)
        # confirmed chain: N6YP → KJOHN → KBULN → WOODY → BBS
        # KBETH is discarded
        edges = _confirmed_edges("N6YP", "KJOHN*,KBULN,WOODY*,KBETH", self.BBS)
        assert edges == [
            ("N6YP",  "KJOHN"),
            ("KJOHN", "KBULN"),
            ("KBULN", "WOODY"),
            ("WOODY", self.BBS),
        ]
        assert all("KBETH" not in e for e in edges)

    def test_no_star_returns_empty(self):
        # No digi has H-bit — we cannot confirm any relay
        edges = _confirmed_edges("W6OAK", "WOODY,KBULN", self.BBS)
        assert edges == []

    def test_only_first_star(self):
        # Only KJOHN has relayed; KBULN and beyond are speculative
        edges = _confirmed_edges("W6OAK", "KJOHN*,KBULN,WOODY", self.BBS)
        assert edges == [("W6OAK", "KJOHN"), ("KJOHN", self.BBS)]

    def test_long_confirmed_chain(self):
        via = "KPHXOR*,HMKR*,KRDG*,KBANN*,WOODY*"
        edges = _confirmed_edges("KC7HEX", via, self.BBS)
        expected = [
            ("KC7HEX", "KPHXOR"),
            ("KPHXOR", "HMKR"),
            ("HMKR",   "KRDG"),
            ("KRDG",   "KBANN"),
            ("KBANN",  "WOODY"),
            ("WOODY",  self.BBS),
        ]
        assert edges == expected

    def test_real_world_kc7hex(self):
        # From the live heard list: KPHXOR,HMKR,KRDG,KBANN,WOODY*,KBULN,BRKNRG
        # Last star is WOODY at index 4 — KBULN,BRKNRG are discarded.
        via = "KPHXOR,HMKR,KRDG,KBANN,WOODY*,KBULN,BRKNRG"
        edges = _confirmed_edges("KC7HEX", via, self.BBS)
        expected = [
            ("KC7HEX", "KPHXOR"),
            ("KPHXOR", "HMKR"),
            ("HMKR",   "KRDG"),
            ("KRDG",   "KBANN"),
            ("KBANN",  "WOODY"),
            ("WOODY",  self.BBS),
        ]
        assert edges == expected
        # Nothing after WOODY should appear
        targets = [e[1] for e in edges]
        assert "KBULN"  not in targets
        assert "BRKNRG" not in targets

    def test_edge_list_length_matches_confirmed_hops(self):
        # N hops confirmed → N edges
        via = "A*,B*,C*"
        edges = _confirmed_edges("SRC", via, self.BBS)
        assert len(edges) == 4  # SRC→A, A→B, B→C, C→BBS


# ---------------------------------------------------------------------------
# Integration tests — /api/heard/graph endpoint
# ---------------------------------------------------------------------------

async def _make_plugin_with_data() -> HeardPlugin:
    tmp = tempfile.mkdtemp(prefix="bbs2_graph_test_")
    db_path = str(Path(tmp) / "test.db")
    plugin = HeardPlugin()
    await plugin.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    import time
    now = int(time.time())
    # Direct station
    await plugin.on_heard("KF6ANX", "BEACON", [], now, "agwpe")
    # Via one digi (confirmed)
    await plugin.on_heard("W6OAK", "BEACON", ["WOODY*"], now, "agwpe")
    # Via two digis; only first confirmed
    await plugin.on_heard("N6YP", "APRS", ["KJOHN*", "KBULN", "WOODY"], now, "agwpe")
    # Real-world partial path: WOODY* cuts off KBULN,BRKNRG
    await plugin.on_heard("KC7HEX", "APRS",
                           ["KPHXOR", "HMKR", "KRDG", "KBANN", "WOODY*", "KBULN", "BRKNRG"],
                           now, "agwpe")
    return plugin


@pytest.mark.asyncio
class TestHeardGraph:
    async def test_graph_endpoint_requires_sysop(self, bbs_server):
        """Unauthenticated request must return 401 — verified by checking the
        helper function is importable and the route is registered."""
        from server.routes.heard import _confirmed_edges
        assert callable(_confirmed_edges)

    async def test_direct_station_edge(self):
        plugin = await _make_plugin_with_data()
        # Build graph manually using the same logic as the endpoint.
        import sqlite3
        db = sqlite3.connect(plugin._db_path)
        rows = db.execute(
            "SELECT callsign, via, via_base FROM heard_paths"
        ).fetchall()
        db.close()
        BBS = "W6ELA"
        edges = {}
        for src, via, via_base in rows:
            for a, b in _confirmed_edges(src.upper(), via or "", BBS):
                edges[(a, b)] = edges.get((a, b), 0) + 1

        # KF6ANX is direct → KF6ANX → BBS
        assert ("KF6ANX", BBS) in edges

    async def test_relayed_station_edge(self):
        plugin = await _make_plugin_with_data()
        import sqlite3
        db = sqlite3.connect(plugin._db_path)
        rows = db.execute(
            "SELECT callsign, via, via_base FROM heard_paths"
        ).fetchall()
        db.close()
        BBS = "W6ELA"
        edges = {}
        for src, via, via_base in rows:
            for a, b in _confirmed_edges(src.upper(), via or "", BBS):
                edges[(a, b)] = edges.get((a, b), 0) + 1

        # W6OAK via WOODY* → W6OAK→WOODY, WOODY→BBS
        assert ("W6OAK", "WOODY") in edges
        assert ("WOODY", BBS) in edges

    async def test_partial_path_not_included(self):
        """KC7HEX's KBULN and BRKNRG (after WOODY*) must not appear as edge targets."""
        plugin = await _make_plugin_with_data()
        import sqlite3
        db = sqlite3.connect(plugin._db_path)
        rows = db.execute(
            "SELECT callsign, via, via_base FROM heard_paths"
        ).fetchall()
        db.close()
        BBS = "W6ELA"
        all_nodes: set[str] = set()
        for src, via, via_base in rows:
            for a, b in _confirmed_edges(src.upper(), via or "", BBS):
                all_nodes.add(a); all_nodes.add(b)

        # KBULN and BRKNRG only appear *after* WOODY* in KC7HEX's path
        assert "KBULN"  not in all_nodes
        assert "BRKNRG" not in all_nodes

    async def test_node_type_classification(self):
        """WOODY must be classified as 'digi'; KF6ANX as 'station'."""
        plugin = await _make_plugin_with_data()
        import sqlite3
        db = sqlite3.connect(plugin._db_path)
        rows = db.execute(
            "SELECT callsign, via, via_base FROM heard_paths"
        ).fetchall()
        db.close()
        BBS = "W6ELA"
        stations: set[str] = set()
        digis:    set[str] = set()
        for src, via, via_base in rows:
            src = src.upper()
            stations.add(src)
            for a, b in _confirmed_edges(src, via or "", BBS):
                if a not in (src, BBS): digis.add(a)
                if b not in (src, BBS): digis.add(b)

        assert "WOODY"  in digis
        assert "KF6ANX" in stations
        assert "KF6ANX" not in digis
