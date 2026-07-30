"""
tests/test_netrom_router.py — Unit tests for NetromRouter.
"""
import asyncio
import time

import pytest

from bbs.ax25.netrom_frame import (
    NodeEntry,
    decode_nodes_broadcast,
    encode_nodes_broadcast,
)
from bbs.netrom.router import NetromRouter, RouteEntry


def _nodes_payload(alias: str, entries: list[NodeEntry]) -> bytes:
    return encode_nodes_broadcast(alias, entries)


class TestNetromRouterReceive:
    def test_empty_table(self):
        r = NetromRouter("W6ELA-1", "PALO")
        assert r.node_count == 0
        assert r.routing_table == []

    def test_processes_nodes_broadcast(self):
        r = NetromRouter("W6ELA-1", "PALO")
        payload = _nodes_payload("WBAY", [
            NodeEntry("K6FB-5", "ROCK", "K6FB-5", 200),
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", payload))
        # Two destinations: the K6FB-5 entry, plus the broadcast source
        # N6ZX-5 auto-added as a direct neighbor.
        assert r.node_count == 2
        route = r.get_route("K6FB-5")
        assert route is not None
        assert route.alias == "ROCK"
        assert route.quality == 200
        assert route.via_call == "N6ZX-5"
        assert route.via_alias == "WBAY"

    def test_auto_add_broadcast_source_as_direct_neighbor(self):
        """Receiving any NODES broadcast — even header-only — implies the
        source is RF-reachable.  Add them as a direct route to themselves."""
        r = NetromRouter("W6ELA-1", "PALO")
        # Len=7 header-only NODES, like KI6ZHD-5 / SCLARA emits.
        payload = _nodes_payload("SCLARA", [])
        asyncio.run(r.on_netrom_frame("KI6ZHD-5", "NODES", payload))
        assert r.node_count == 1
        route = r.get_route("KI6ZHD-5")
        assert route is not None
        assert route.alias == "SCLARA"
        assert route.via_call == "KI6ZHD-5"
        assert route.quality == 192   # NORCAL direct-neighbor convention
        # Also reachable by alias.
        assert r.get_route("SCLARA") is route

    def test_non_nodes_frame_ignored(self):
        r = NetromRouter("W6ELA-1", "PALO")
        asyncio.run(r.on_netrom_frame("N6ZX-5", "W6ELA-1", b"\x01\x02\x03"))
        assert r.node_count == 0

    def test_get_route_by_alias(self):
        r = NetromRouter("W6ELA-1", "PALO")
        payload = _nodes_payload("WBAY", [NodeEntry("N6ZX-5", "WBAY", "N6ZX-5", 192)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", payload))
        assert r.get_route("WBAY") is r.get_route("N6ZX-5")

    def test_best_quality_wins(self):
        """When two neighbors advertise the same dest, get_route returns the higher-quality one."""
        r = NetromRouter("W6ELA-1", "PALO")
        p1 = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "COOL", "K2YE-5", 180)])
        p2 = _nodes_payload("ROCK", [NodeEntry("K2YE-5", "COOL", "K6FB-5", 220)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p1))
        asyncio.run(r.on_netrom_frame("K6FB-5", "NODES", p2))
        route = r.get_route("K2YE-5")
        assert route is not None
        assert route.quality == 220
        assert route.via_call == "K6FB-5"


class TestNetromRouterMultiRoute:
    def test_keeps_up_to_three_alternates(self):
        """The router keeps up to 3 alternate routes per destination, sorted by quality."""
        r = NetromRouter("W6ELA-1", "PALO")
        # Four different advertisers, each advertising K2YE-5 with different quality.
        for via, alias, q in [
            ("N6ZX-5",   "WBAY",  150),
            ("K6FB-5",   "ROCK",  220),
            ("KF6ANX-4", "JOHN",  180),
            ("W6OAK-5",  "OAK",   100),  # lowest — should be dropped
        ]:
            p = _nodes_payload(alias, [NodeEntry("K2YE-5", "COOL", "K2YE-5", q)])
            asyncio.run(r.on_netrom_frame(via, "NODES", p))

        routes = r.get_routes("K2YE-5")
        assert len(routes) == 3
        assert [rt.quality for rt in routes] == [220, 180, 150]
        # W6OAK-5 (lowest quality) was dropped
        assert all(rt.via_call != "W6OAK-5" for rt in routes)

    def test_same_advertiser_upserts(self):
        """Re-broadcast from the same neighbor refreshes its entry, not appends."""
        r = NetromRouter("W6ELA-1", "PALO")
        p1 = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "COOL", "K2YE-5", 150)])
        p2 = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "COOL", "K2YE-5", 180)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p1))
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p2))
        routes = r.get_routes("K2YE-5")
        assert len(routes) == 1
        assert routes[0].quality == 180

    def test_routing_table_includes_alternates(self):
        r = NetromRouter("W6ELA-1", "PALO")
        p1 = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "COOL", "K2YE-5", 150)])
        p2 = _nodes_payload("ROCK", [NodeEntry("K2YE-5", "COOL", "K6FB-5", 220)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p1))
        asyncio.run(r.on_netrom_frame("K6FB-5", "NODES", p2))
        # K2YE-5 has both alternates; N6ZX-5 and K6FB-5 are also auto-added
        # as direct neighbors → 4 total routes across 3 destinations.
        k2ye_routes = r.get_routes("K2YE-5")
        assert len(k2ye_routes) == 2
        assert r.node_count == 3


class TestNetromRouterPrune:
    def test_prune_removes_stale_routes(self):
        r = NetromRouter("W6ELA-1", "PALO", route_ttl_seconds=60)
        p = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "COOL", "K2YE-5", 200)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p))
        # K2YE-5 (entry) + N6ZX-5 (auto-added source) = 2 destinations.
        assert r.node_count == 2

        # Simulate "now" being 120s after the routes were learned.
        future = time.time() + 120
        pruned = r.prune_stale_routes(now=future)
        assert pruned == 2
        assert r.node_count == 0

    def test_prune_keeps_fresh_routes(self):
        r = NetromRouter("W6ELA-1", "PALO", route_ttl_seconds=60)
        p = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "COOL", "K2YE-5", 200)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p))
        # 30s later: still fresh
        pruned = r.prune_stale_routes(now=time.time() + 30)
        assert pruned == 0
        assert r.node_count == 2   # K2YE-5 + N6ZX-5 (auto-added)

    def test_prune_only_removes_expired_alternates(self):
        r = NetromRouter("W6ELA-1", "PALO", route_ttl_seconds=60)
        # First advertiser is at t=0
        p1 = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "COOL", "K2YE-5", 150)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p1))
        old_ts = time.time()
        # Manually backdate N6ZX-5's K2YE-5 entry AND N6ZX-5's own auto-added
        # route so they get pruned together (focus this test on the K2YE-5
        # alternate-prune behavior; N6ZX-5's aging is unrelated).
        for route in r._routes["K2YE-5"]:                # type: ignore[attr-defined]
            if route.via_call.upper() == "N6ZX-5":
                route.last_seen = old_ts - 120
        for route in r._routes["N6ZX-5"]:                # type: ignore[attr-defined]
            route.last_seen = old_ts - 120

        # Second advertiser is fresh now
        p2 = _nodes_payload("ROCK", [NodeEntry("K2YE-5", "COOL", "K6FB-5", 220)])
        asyncio.run(r.on_netrom_frame("K6FB-5", "NODES", p2))

        pruned = r.prune_stale_routes()
        # Two pruned: N6ZX-5's K2YE-5 alternate, and N6ZX-5 itself.
        assert pruned == 2
        routes = r.get_routes("K2YE-5")
        assert len(routes) == 1
        assert routes[0].via_call == "K6FB-5"


class TestNetromRouterBuildNodes:
    """Transit-node mode (`advertise_self_only=False`): re-advertise learned
    routes with hop_cost degradation, neighbor substitution, and min-quality
    filtering. Default mode is polite-client (self-only); these tests opt in
    to transit mode explicitly."""

    def test_empty_table_returns_none(self):
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False)
        assert r.build_nodes_payload() is None

    def test_payload_has_correct_alias(self):
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False)
        payload = _nodes_payload("WBAY", [NodeEntry("N6ZX-5", "WBAY", "N6ZX-5", 200)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", payload))

        result = r.build_nodes_payload()
        assert result is not None
        frame = decode_nodes_broadcast("W6ELA-1", result)
        assert frame is not None
        assert frame.source_alias == "PALO"

    def test_payload_uses_via_call_as_neighbor(self):
        """In our broadcast, the neighbor_call must be OUR adjacent neighbor,
        not whatever the upstream advertised."""
        r = NetromRouter("W6ELA-1", "PALO", hop_cost=0, advertise_self_only=False)
        # Our neighbor N6ZX-5 says K2YE-5 is reachable through its neighbor "ABC-1".
        payload = _nodes_payload("WBAY", [
            NodeEntry("K2YE-5", "COOL", "ABC-1", 200),
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", payload))

        result = r.build_nodes_payload()
        frame = decode_nodes_broadcast("W6ELA-1", result)
        assert frame is not None
        # Entries: K2YE-5 (re-advertised) and N6ZX-5 (auto-added direct).
        k2ye = next(e for e in frame.entries if e.dest_call == "K2YE-5")
        # Our broadcast advertises N6ZX-5 (our actual next hop), NOT ABC-1.
        assert k2ye.neighbor_call == "N6ZX-5"

    def test_payload_degrades_quality(self):
        r = NetromRouter("W6ELA-1", "PALO", hop_cost=25, advertise_self_only=False)
        payload = _nodes_payload("WBAY", [
            NodeEntry("K2YE-5", "COOL", "K2YE-5", 200),
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", payload))

        result = r.build_nodes_payload()
        frame = decode_nodes_broadcast("W6ELA-1", result)
        assert frame is not None
        # 200 - 25 = 175
        assert frame.entries[0].quality == 175

    def test_payload_skips_below_min_quality(self):
        r = NetromRouter(
            "W6ELA-1", "PALO", hop_cost=25, min_advert_quality=180,
            advertise_self_only=False,
        )
        payload = _nodes_payload("WBAY", [
            NodeEntry("K2YE-5", "COOL", "K2YE-5", 200),  # → degraded 175 < 180, skipped
            NodeEntry("KK6-9",  "GOOD", "KK6-9",  220),  # → degraded 195 ≥ 180, kept
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", payload))

        result = r.build_nodes_payload()
        frame = decode_nodes_broadcast("W6ELA-1", result)
        assert frame is not None
        dest_calls = {e.dest_call for e in frame.entries}
        assert dest_calls == {"KK6-9"}

    def test_payload_returns_none_when_all_skipped(self):
        r = NetromRouter(
            "W6ELA-1", "PALO", hop_cost=100, min_advert_quality=200,
            advertise_self_only=False,
        )
        payload = _nodes_payload("WBAY", [
            NodeEntry("K2YE-5", "COOL", "K2YE-5", 150),  # degraded 50 < 200
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", payload))
        assert r.build_nodes_payload() is None

    def test_payload_uses_only_best_alternate(self):
        """When alternates exist, only the best-quality one is advertised."""
        r = NetromRouter("W6ELA-1", "PALO", hop_cost=0, advertise_self_only=False)
        for via, alias, q in [
            ("N6ZX-5",   "WBAY", 150),
            ("K6FB-5",   "ROCK", 220),
        ]:
            p = _nodes_payload(alias, [NodeEntry("K2YE-5", "COOL", "K2YE-5", q)])
            asyncio.run(r.on_netrom_frame(via, "NODES", p))

        result = r.build_nodes_payload()
        frame = decode_nodes_broadcast("W6ELA-1", result)
        assert frame is not None
        # Entries: K2YE-5 (best alternate only) + N6ZX-5 + K6FB-5 (auto-added).
        k2ye = next(e for e in frame.entries if e.dest_call == "K2YE-5")
        # Best route is via K6FB-5 with quality 220.
        assert k2ye.neighbor_call == "K6FB-5"
        assert k2ye.quality == 220


# ── Polite-client (self-only) mode ──────────────────────────────────────────

class TestNetromRouterSelfOnlyMode:
    """Default mode (`advertise_self_only=True`): the broadcast is a 7-byte
    header announcing only our existence — no route entries. Peers add the
    source as a direct neighbor on RX regardless of entry count, which is
    how client/endpoint stations on NORCAL 145.05 (e.g. KI6ZHD-5 SCLARA)
    operate per the network admin convention."""

    def test_default_mode_is_self_only(self):
        r = NetromRouter("W6ELA-1", "PALO")
        assert r._advertise_self_only is True

    def test_empty_table_still_broadcasts(self):
        """Cold-start polite-client: we have nothing in our routing table
        but we still announce our own presence on every broadcast tick."""
        r = NetromRouter("W6ELA-1", "PALO")
        payload = r.build_nodes_payload()
        assert payload is not None
        assert len(payload) == 7   # 1-byte discriminator + 6-byte alias

    def test_full_table_still_emits_header_only(self):
        """Even when our routing table has entries, we don't propagate
        them — only the 7-byte header goes out."""
        r = NetromRouter("W6ELA-1", "PALO")
        # Seed via a learned NODES broadcast.
        learned = _nodes_payload("WBAY", [
            NodeEntry("K2YE-5", "COOL", "K2YE-5", 200),
            NodeEntry("KK6-9",  "GOOD", "KK6-9",  220),
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", learned))
        # K2YE-5 + KK6-9 (entries) + N6ZX-5 (auto-added source) = 3.
        assert r.node_count == 3
        payload = r.build_nodes_payload()
        assert payload is not None
        assert len(payload) == 7

    def test_header_only_decodes_to_zero_entries(self):
        r = NetromRouter("W6ELA-1", "PALO")
        payload = r.build_nodes_payload()
        frame = decode_nodes_broadcast("W6ELA-1", payload)
        assert frame is not None
        assert frame.source_alias == "PALO"
        assert frame.entries == []

    def test_alias_truncated_to_6_chars(self):
        r = NetromRouter("W6ELA-1", "TOOLONGALIAS")
        payload = r.build_nodes_payload()
        assert len(payload) == 7   # header still exactly 7 bytes
        frame = decode_nodes_broadcast("W6ELA-1", payload)
        assert frame.source_alias == "TOOLON"   # truncated to 6 chars

    def test_setting_false_restores_transit_behavior(self):
        """Sanity: opting into transit mode emits the route table again."""
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False)
        learned = _nodes_payload("WBAY", [
            NodeEntry("K2YE-5", "COOL", "K2YE-5", 200),
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", learned))
        payload = r.build_nodes_payload()
        assert payload is not None
        assert len(payload) > 7   # has at least one entry
        frame = decode_nodes_broadcast("W6ELA-1", payload)
        # K2YE-5 (learned entry) + N6ZX-5 (auto-added source).
        assert {e.dest_call for e in frame.entries} == {"K2YE-5", "N6ZX-5"}


# ── Seed from heard DB ──────────────────────────────────────────────────────

import aiosqlite


async def _make_heard_db(tmp_path, rows: list[tuple]) -> str:
    """Create a heard-style DB with a netrom_routes table populated by rows.

    Each row tuple: (dest_call, alias, neighbor_call, quality, via_call,
    via_alias, last_seen).
    """
    db_path = str(tmp_path / "bbs.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS netrom_routes (
                dest_call     TEXT NOT NULL COLLATE NOCASE,
                neighbor_call TEXT NOT NULL COLLATE NOCASE,
                alias         TEXT NOT NULL DEFAULT '',
                quality       INTEGER NOT NULL DEFAULT 0,
                via_call      TEXT NOT NULL COLLATE NOCASE,
                via_alias     TEXT NOT NULL DEFAULT '',
                last_seen     INTEGER NOT NULL,
                PRIMARY KEY (dest_call, neighbor_call)
            )
        """)
        await db.executemany(
            "INSERT INTO netrom_routes "
            "(dest_call, alias, neighbor_call, quality, via_call, "
            " via_alias, last_seen) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        await db.commit()
    return db_path


class TestSeedFromDB:
    async def test_loads_fresh_routes(self, tmp_path):
        now = int(time.time())
        db = await _make_heard_db(tmp_path, [
            ("N6ZX-5",  "WBAY", "N6ZX-5", 255, "N6ZX-5", "WBAY",  now - 60),
            ("K2YE-5",  "MONTC", "K2YE-5", 200, "N6ZX-5", "WBAY", now - 60),
            ("KF6ANX-4","JOHN", "KF6ANX-4", 180, "KF6ANX-4", "JOHN", now - 120),
        ])
        r = NetromRouter("W6ELA-1", "PALO")
        loaded = await r.seed_from_db(db)
        assert loaded == 3
        assert r.node_count == 3
        assert r.get_route("N6ZX-5") is not None
        assert r.get_route("N6ZX-5").quality == 255

    async def test_skips_stale_routes(self, tmp_path):
        # TTL = 1h; one entry within, one expired.
        now = int(time.time())
        db = await _make_heard_db(tmp_path, [
            ("N6ZX-5", "WBAY", "N6ZX-5", 255, "N6ZX-5", "WBAY", now - 60),
            ("STALE",  "OLD",  "STALE",  255, "STALE",  "OLD",  now - 7200),
        ])
        r = NetromRouter("W6ELA-1", "PALO", route_ttl_seconds=3600)
        loaded = await r.seed_from_db(db)
        assert loaded == 1
        assert r.get_route("N6ZX-5") is not None
        assert r.get_route("STALE") is None

    async def test_missing_table_returns_zero(self, tmp_path):
        # Empty DB file — no netrom_routes table.
        db_path = str(tmp_path / "empty.db")
        async with aiosqlite.connect(db_path) as db:
            await db.execute("CREATE TABLE other (x INTEGER)")
            await db.commit()
        r = NetromRouter("W6ELA-1", "PALO")
        loaded = await r.seed_from_db(db_path)
        assert loaded == 0
        assert r.node_count == 0

    async def test_seeding_does_not_fire_observer(self, tmp_path):
        now = int(time.time())
        db = await _make_heard_db(tmp_path, [
            ("N6ZX-5", "WBAY", "N6ZX-5", 255, "N6ZX-5", "WBAY", now - 60),
        ])
        r = NetromRouter("W6ELA-1", "PALO")
        observed: list[object] = []

        async def _observer(frame):
            observed.append(frame)

        r.set_nodes_observer(_observer)
        await r.seed_from_db(db)
        # Seeding bypasses the observer — heard rows are not re-emitted.
        assert observed == []

    async def test_seeded_routes_in_payload_when_transit_mode(self, tmp_path):
        # After seeding, build_nodes_payload() returns a real payload —
        # exactly the cold-start fix we are paying for. Default polite-client
        # mode emits a header-only broadcast regardless of seed state, so
        # this test exercises transit mode explicitly to verify the seed
        # made it into the router's internal table.
        now = int(time.time())
        db = await _make_heard_db(tmp_path, [
            ("N6ZX-5", "WBAY", "N6ZX-5", 255, "N6ZX-5", "WBAY", now - 60),
        ])
        r = NetromRouter("W6ELA-1", "PALO", hop_cost=25, advertise_self_only=False)
        await r.seed_from_db(db)
        payload = r.build_nodes_payload()
        assert payload is not None
        frame = decode_nodes_broadcast("W6ELA-1", payload)
        assert frame is not None
        assert any(e.dest_call == "N6ZX-5" for e in frame.entries)

    async def test_alternates_preserved_with_quality_order(self, tmp_path):
        # The heard DB PK is (dest_call, neighbor_call) — multiple alternates
        # for the same destination must therefore differ on neighbor_call.
        # That mirrors reality: different upstream broadcasters typically
        # advertise different next-hops for the same destination.
        now = int(time.time())
        db = await _make_heard_db(tmp_path, [
            ("K2YE-5", "MONTC", "K2YE-5",  180, "N6ZX-5",  "WBAY", now - 60),
            ("K2YE-5", "MONTC", "REPEAT-1", 240, "K6FB-5",  "ROCK", now - 60),
            ("K2YE-5", "MONTC", "K2YE-7",  120, "W6OAK-5", "OAK",  now - 60),
        ])
        r = NetromRouter("W6ELA-1", "PALO")
        await r.seed_from_db(db)
        routes = r.get_routes("K2YE-5")
        assert len(routes) == 3
        # Sorted by quality desc.
        qualities = [rt.quality for rt in routes]
        assert qualities == sorted(qualities, reverse=True)

