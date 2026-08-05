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
from bbs.netrom.router import (
    NetromRouter, RouteEntry, _is_routable_callsign, _route_quality,
)


def _nodes_payload(alias: str, entries: list[NodeEntry]) -> bytes:
    return encode_nodes_broadcast(alias, entries)


class TestRouteQuality:
    """N5: receive-side quality = ((advertised × path_quality) + 128) >> 8
    (1987 manual p.65, rule 5)."""

    def test_formula_values(self):
        assert _route_quality(200, 192) == 150     # (38400+128)>>8
        assert _route_quality(220, 192) == 165
        assert _route_quality(255, 255) == 254     # near-perfect link, near-perfect route
        assert _route_quality(0, 192) == 0
        assert _route_quality(100, 0) == 0         # path quality 0 → ignore neighbour

    def test_composition_caps_below_direct(self):
        # A transit route through a 192-quality link can never beat a 1-hop
        # direct route (quality == path quality 192): max is _route_quality(255,192).
        assert _route_quality(255, 192) < 192


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
        assert route.quality == 150   # 200 advertised × 192 link ÷ 256 (N5)
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
        assert route.quality == 165   # 220 × 192 ÷ 256 (composed; ordering preserved)
        assert route.via_call == "K6FB-5"


class TestReceiveAlgorithm:
    """N5 — the 1987 receive-a-NODES algorithm (manual p.65, rules 1-9)."""

    def test_direct_route_is_path_quality_transit_is_composed(self):
        r = NetromRouter("W6ELA-1", "PALO", channel_quality=192)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert r.get_route("N6ZX-5").quality == 192                 # rule 4: direct = path q
        assert r.get_route("K2YE-5").quality == _route_quality(200, 192)  # rule 5

    def test_per_neighbour_override_changes_all_routes_via_it(self):
        r = NetromRouter("W6ELA-1", "PALO", neighbour_quality={"N6ZX-5": 255})
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert r.get_route("N6ZX-5").quality == 255                 # direct = override
        assert r.get_route("K2YE-5").quality == _route_quality(200, 255)

    def test_trivial_loop_route_dropped(self):
        # A route whose advertised best-neighbour is US → q0 → below worst_quality.
        r = NetromRouter("W6ELA-1", "PALO")
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "W6ELA-1", 200)])))
        assert r.get_route("K2YE-5") is None        # rule 6 + rule 8
        assert r.get_route("N6ZX-5") is not None    # source still added

    def test_worst_quality_filters_low_routes(self):
        r = NetromRouter("W6ELA-1", "PALO", worst_quality=100)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 60)])))
        assert r.get_route("K2YE-5") is None        # composed 45 < 100 (rule 8)

    def test_worst_quality_zero_ignores_all_nodes(self):
        r = NetromRouter("W6ELA-1", "PALO", worst_quality=0)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert r.node_count == 0                     # rule 1

    def test_path_quality_zero_disregards_broadcast(self):
        r = NetromRouter("W6ELA-1", "PALO", neighbour_quality={"N6ZX-5": 0})
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert r.node_count == 0                     # neighbour disabled (p.23)

    def test_max_destinations_cap(self):
        r = NetromRouter("W6ELA-1", "PALO", max_destinations=2)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", _nodes_payload("WBAY", [
            NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200),
            NodeEntry("K6FB-5", "ROCK", "K6FB-5", 200),
        ])))
        assert r.node_count == 2                     # rule 9: source + 1 dest fill the cap
        assert r.get_route("K2YE-5") is not None
        assert r.get_route("K6FB-5") is None


class TestObsolescence:
    """N5b — obsolescence-count lifecycle (1987 manual p.66)."""

    def test_route_gets_obs_initializer(self):
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=6)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert r.get_route("K2YE-5").obs_count == 6
        assert r.get_route("N6ZX-5").obs_count == 6      # the direct source route too

    def test_decay_decrements_then_deletes_at_zero(self):
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=3)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert r.decay_obsolescence() == 0 and r.get_route("K2YE-5").obs_count == 2
        assert r.decay_obsolescence() == 0 and r.get_route("K2YE-5").obs_count == 1
        assert r.decay_obsolescence() == 2               # K2YE-5 + N6ZX-5 hit 0 → deleted
        assert r.node_count == 0

    def test_rebroadcast_reinitialises_obs(self):
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=6)
        p = _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p))
        r.decay_obsolescence(); r.decay_obsolescence()
        assert r.get_route("K2YE-5").obs_count == 4
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", p))   # heard again
        assert r.get_route("K2YE-5").obs_count == 6            # refreshed to initializer

    def test_obs_initializer_zero_disables_decay(self):
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=0)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert r.get_route("K2YE-5").obs_count == 0           # permanent
        assert r.decay_obsolescence() == 0                    # scan is a no-op
        assert r.node_count == 2

    def test_locked_route_untouched_by_decay(self):
        # A manually-added route with obs_count == 0 (locked) survives the scan.
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=6)
        r._upsert_route(RouteEntry(
            "K2YE-5", "MONTC", "K2YE-5", 200, "K2YE-5", "MONTC", time.time(),
            obs_count=0))
        r.decay_obsolescence()
        assert r.get_route("K2YE-5") is not None
        assert r.get_route("K2YE-5").obs_count == 0           # still locked


class TestPersistence:
    """N5b/b2 — the router owns netrom_routes + netrom_neighbours, persisting
    composed quality + obs, surviving a restart, and auto-pruning."""

    def _populated(self):
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=6)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        return r

    def test_persist_seed_roundtrip(self, tmp_path):
        db = str(tmp_path / "n.db")
        r = self._populated(); r.set_db_path(db)
        asyncio.run(r.persist())
        r2 = NetromRouter("W6ELA-1", "PALO")
        loaded = asyncio.run(r2.seed_from_db(db))
        assert loaded >= 1
        assert r2.get_route("K2YE-5").quality == _route_quality(200, 192)  # composed
        assert r2.get_route("K2YE-5").obs_count == 6                        # obs survived
        assert r2.is_direct_neighbor("N6ZX-5") is True                     # neighbour survived
        assert "N6ZX-5" in r2.adjacent_neighbors

    def test_persist_snapshot_prunes_stale_rows(self, tmp_path):
        db = str(tmp_path / "n.db")
        r = self._populated(); r.set_db_path(db)
        asyncio.run(r.persist())
        r._routes.clear(); r._neighbours.clear()      # everything decayed away
        asyncio.run(r.persist())                       # snapshot is now empty
        r2 = NetromRouter("W6ELA-1", "PALO")
        assert asyncio.run(r2.seed_from_db(db)) == 0
        assert r2.adjacent_neighbors == set()

    def test_persist_is_noop_without_db_path(self):
        asyncio.run(self._populated().persist())       # must not raise

    def test_seed_migrates_pre_n5_routes(self, tmp_path):
        import sqlite3
        db = str(tmp_path / "old.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE netrom_routes (dest_call TEXT, neighbor_call TEXT,"
                    " alias TEXT, quality INT, via_call TEXT, via_alias TEXT,"
                    " last_seen INT, PRIMARY KEY(dest_call, neighbor_call))")
        con.execute("INSERT INTO netrom_routes VALUES "
                    "('K2YE-5','K2YE-5','MONTC',150,'N6ZX-5','WBAY',?)",
                    (int(time.time()),))
        con.commit(); con.close()
        r = NetromRouter("W6ELA-1", "PALO")
        assert asyncio.run(r.seed_from_db(db)) == 1
        assert r.get_route("K2YE-5").obs_count == 6    # column added, default filled


class TestNeighbourLifecycle:
    """N5b — neighbour-list lifecycle + is_direct_neighbor keyed off it."""

    def test_adjacent_neighbors_are_the_broadcast_sources(self):
        r = NetromRouter("W6ELA-1", "PALO")
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        # N6ZX-5 broadcast → a neighbour; K2YE-5 is transit-only → not one.
        assert r.adjacent_neighbors == {"N6ZX-5"}
        assert r.is_direct_neighbor("N6ZX-5") is True
        assert r.is_direct_neighbor("K2YE-5") is False

    def test_neighbour_ages_out_when_routes_decay(self):
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=1)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 200)])))
        assert "N6ZX-5" in r.adjacent_neighbors
        r.decay_obsolescence()      # obs 1→0: routes deleted → neighbour reconciled out
        assert r.is_direct_neighbor("N6ZX-5") is False
        assert r.adjacent_neighbors == set()

    def test_crosslink_pins_neighbour_until_link_drops(self):
        r = NetromRouter("W6ELA-1", "PALO", obs_initializer=1)
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", _nodes_payload("WBAY", [])))
        r.note_crosslink("N6ZX-5", up=True)
        r.decay_obsolescence()      # routes gone, but the live crosslink pins it
        assert r.is_direct_neighbor("N6ZX-5") is True
        r.note_crosslink("N6ZX-5", up=False)   # link down → reconciled out
        assert r.is_direct_neighbor("N6ZX-5") is False


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
        # composed via ×192÷256: 220→165, 180→135, 150→113 (order preserved)
        assert [rt.quality for rt in routes] == [165, 135, 113]
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
        assert routes[0].quality == 135   # 180 × 192 ÷ 256 (refreshed, not appended)

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

    def test_payload_advertises_composed_quality(self):
        # N5: no hop_cost degradation — we advertise our composed route quality
        # verbatim (the receiver re-composes through its own link to us).
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False)
        r._upsert_route(RouteEntry(
            "K2YE-5", "COOL", "K2YE-5", 150, "N6ZX-5", "WBAY", time.time()))
        frame = decode_nodes_broadcast("W6ELA-1", r.build_nodes_payload())
        assert frame is not None
        assert frame.entries[0].quality == 150            # advertised as-is

    def test_payload_obs_gate_skips_stale_routes(self):
        # A route below obs_min_to_broadcast is too stale to propagate (p.66).
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False,
                         obs_min_to_broadcast=5)
        now = time.time()
        r._upsert_route(RouteEntry("K2YE-5", "COOL", "K2YE-5", 150,
                                   "N6ZX-5", "WBAY", now, obs_count=6))   # fresh → advertised
        r._upsert_route(RouteEntry("KK6XY-9", "GOOD", "KK6XY-9", 160,
                                   "N6ZX-5", "WBAY", now, obs_count=2))   # stale → skipped
        frame = decode_nodes_broadcast("W6ELA-1", r.build_nodes_payload())
        assert frame is not None
        assert {e.dest_call for e in frame.entries} == {"K2YE-5"}

    def test_payload_locked_route_always_advertised(self):
        # obs_count == 0 marks a locked route — advertised regardless of the gate.
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False,
                         obs_min_to_broadcast=5)
        r._upsert_route(RouteEntry("K2YE-5", "COOL", "K2YE-5", 150,
                                   "N6ZX-5", "WBAY", time.time(), obs_count=0))
        frame = decode_nodes_broadcast("W6ELA-1", r.build_nodes_payload())
        assert {e.dest_call for e in frame.entries} == {"K2YE-5"}

    def test_payload_returns_none_when_all_stale(self):
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False,
                         obs_min_to_broadcast=5)
        r._upsert_route(RouteEntry("K2YE-5", "COOL", "K2YE-5", 150,
                                   "N6ZX-5", "WBAY", time.time(), obs_count=1))
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
        # Best route is via K6FB-5; quality composed 220 × 192 ÷ 256 = 165.
        assert k2ye.neighbor_call == "K6FB-5"
        assert k2ye.quality == 165


# ── NODES broadcast fragmentation (N6a — fidelity Gap 1) ────────────────────

class TestNetromNodesFragmentation:
    """A NODES broadcast is a series of AX.25 UI frames, each ≤11 entries; a
    larger routing table fragments across frames, each re-stamped with the
    7-byte header.  build_nodes_payloads() returns one payload per frame."""

    def _transit_router_with(self, n: int) -> NetromRouter:
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False,
                         obs_min_to_broadcast=5)
        now = time.time()
        for i in range(n):
            call = f"W6A{chr(65 + i)}-5"    # W6AA-5 .. W6AY-5 (distinct dests)
            r._upsert_route(RouteEntry(
                call, f"D{i:02d}", call, 200, "K6FB-5", "ROCK", now))
        return r

    def test_small_table_is_one_frame(self):
        payloads = self._transit_router_with(5).build_nodes_payloads()
        assert len(payloads) == 1
        assert len(decode_nodes_broadcast("W6ELA-1", payloads[0]).entries) == 5

    def test_large_table_fragments_at_11_entries(self):
        payloads = self._transit_router_with(25).build_nodes_payloads()  # 11+11+3
        counts = [len(decode_nodes_broadcast("W6ELA-1", p).entries) for p in payloads]
        assert counts == [11, 11, 3]

    def test_every_fragment_carries_header_and_loses_nothing(self):
        payloads = self._transit_router_with(25).build_nodes_payloads()
        seen: set[str] = set()
        for p in payloads:
            assert len(p) <= 256                  # fits one AX.25 info field
            frame = decode_nodes_broadcast("W6ELA-1", p)
            assert frame is not None              # each frame decodes on its own
            assert frame.source_alias == "PALO"   # header re-stamped every frame
            seen.update(e.dest_call for e in frame.entries)
        assert len(seen) == 25                     # union == whole table

    def test_self_only_returns_single_header_frame(self):
        payloads = NetromRouter("W6ELA-1", "PALO").build_nodes_payloads()
        assert len(payloads) == 1 and len(payloads[0]) == 7

    def test_nothing_to_advertise_is_empty_list(self):
        r = NetromRouter("W6ELA-1", "PALO", advertise_self_only=False)
        assert r.build_nodes_payloads() == []


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
            NodeEntry("KK6XY-9",  "GOOD", "KK6XY-9",  220),
        ])
        asyncio.run(r.on_netrom_frame("N6ZX-5", "NODES", learned))
        # K2YE-5 + KK6XY-9 (entries) + N6ZX-5 (auto-added source) = 3.
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


# ── N0a: bogus-destination filtering (routing hygiene) ────────────────────────

class TestCallsignValidation:
    def test_real_callsigns_pass(self):
        for c in ("W6ELA-1", "K2YE-5", "KF6ANX-4", "N6ZX-5", "WA6KQB-5",
                  "K6FB", "2E0ABC", "G8BPQ-2", "KN6PE-7"):
            assert _is_routable_callsign(c), c

    def test_junk_rejected(self):
        # URONode pseudo-entries, garbage, out-of-range SSIDs.
        for c in ("ENABLE", "ENABLE-0", "OFF", "OFF-0", "SFRC", "#DIGI",
                  "", "NODES", "-5", "12345", "K2YE-16"):
            assert not _is_routable_callsign(c), c


class TestNodesSanitization:
    def test_bogus_dests_never_enter_table(self):
        r = NetromRouter("W6ELA-1", "PALO")
        payload = _nodes_payload("MONTC", [
            NodeEntry("N6ZX-5", "WBAY", "N6ZX-5", 180),      # real
            NodeEntry("ENABLE-0", "##TEMP", "K2YE-5", 100),  # URONode pseudo-entry
            NodeEntry("OFF-0", "SFRC", "K2YE-5", 100),       # URONode pseudo-entry
        ])
        asyncio.run(r.on_netrom_frame("K2YE-5", "NODES", payload))
        dests = {rt.dest_call.upper() for rt in r.routing_table}
        assert "N6ZX-5" in dests
        assert "K2YE-5" in dests     # broadcast source auto-added as direct neighbor
        assert not any("ENABLE" in d or "OFF" in d for d in dests)

    def test_observer_also_sees_sanitized_entries(self):
        # The heard plugin consumes the same frame via the observer — it must
        # not record the junk destinations either.
        r = NetromRouter("W6ELA-1", "PALO")
        seen: list[list[str]] = []

        async def _obs(frame):
            seen.append([e.dest_call for e in frame.entries])

        r.set_nodes_observer(_obs)
        payload = _nodes_payload("MONTC", [
            NodeEntry("N6ZX-5", "WBAY", "N6ZX-5", 180),
            NodeEntry("OFF-0", "SFRC", "K2YE-5", 100),
        ])
        asyncio.run(r.on_netrom_frame("K2YE-5", "NODES", payload))
        assert seen == [["N6ZX-5"]]

    async def test_seed_skips_bogus_rows(self, tmp_path):
        now = int(time.time())
        db = await _make_heard_db(tmp_path, [
            ("N6ZX-5",   "WBAY",   "N6ZX-5", 180, "N6ZX-5", "WBAY",  now - 60),
            ("ENABLE-0", "##TEMP", "K2YE-5", 100, "K2YE-5", "MONTC", now - 60),
        ])
        r = NetromRouter("W6ELA-1", "PALO")
        await r.seed_from_db(db)
        dests = {rt.dest_call.upper() for rt in r.routing_table}
        assert "N6ZX-5" in dests
        assert "ENABLE-0" not in dests and "ENABLE" not in dests


# ── N0b: outbound next-hop API (trustworthy crosslink target) ─────────────────

class TestOutboundNeighbor:
    def _direct_and_transit(self):
        """N6ZX-5 heard direct (q192); also advertised as a transit route by
        K6FB-5 at higher quality (q220); K2YE-5 reachable only via N6ZX-5."""
        r = NetromRouter("W6ELA-1", "PALO")
        asyncio.run(r.on_netrom_frame(
            "N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 180)])))
        asyncio.run(r.on_netrom_frame(
            "K6FB-5", "NODES",
            _nodes_payload("ROCK", [NodeEntry("N6ZX-5", "WBAY", "K6FB-5", 220)])))
        return r

    def test_route_is_direct_flag(self):
        r = self._direct_and_transit()
        by_via = {rt.via_call: rt for rt in r.get_routes("N6ZX-5")}
        assert by_via["N6ZX-5"].is_direct is True     # direct link to itself
        assert by_via["K6FB-5"].is_direct is False    # transit via K6FB-5

    def test_is_direct_neighbor(self):
        r = self._direct_and_transit()
        assert r.is_direct_neighbor("N6ZX-5") is True
        assert r.is_direct_neighbor("K6FB-5") is True
        assert r.is_direct_neighbor("K2YE-5") is False   # only reachable via N6ZX-5

    def test_best_neighbor_prefers_direct_over_transit(self):
        r = self._direct_and_transit()
        # N5: with link-adjusted quality a 1-hop direct route (= path quality)
        # always outranks a 2-hop transit route through an equal-quality link,
        # so get_route already returns the direct route…
        assert r.get_route("N6ZX-5").via_call == "N6ZX-5"
        # …and best_neighbor_for prefers the direct link to N6ZX-5 itself.
        assert r.best_neighbor_for("N6ZX-5") == "N6ZX-5"

    def test_best_neighbor_transit(self):
        r = self._direct_and_transit()
        assert r.best_neighbor_for("K2YE-5") == "N6ZX-5"   # crosslink to the neighbor
        assert r.best_neighbor_for("MONTC") == "N6ZX-5"    # by alias

    def test_best_neighbor_unknown_is_none(self):
        r = self._direct_and_transit()
        assert r.best_neighbor_for("W1AW-3") is None

    def test_best_neighbor_min_quality_floor(self):
        r = NetromRouter("W6ELA-1", "PALO")
        asyncio.run(r.on_netrom_frame(
            "N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 60)])))
        # 60 advertised × 192 ÷ 256 = 45 composed.
        assert r.best_neighbor_for("K2YE-5", min_quality=100) is None
        assert r.best_neighbor_for("K2YE-5", min_quality=40) == "N6ZX-5"

    def test_best_neighbor_prefers_direct_heard_over_transit(self):
        """A known dest reachable only via transit is crosslinked to DIRECTLY
        once we've heard it directly on the air (note_heard_direct)."""
        r = self._direct_and_transit()
        assert r.best_neighbor_for("K2YE-5") == "N6ZX-5"      # transit-only in table
        r.note_heard_direct("K2YE-5")                          # now heard on the air
        assert r.best_neighbor_for("K2YE-5") == "K2YE-5"       # → direct
        assert r.best_neighbor_for("MONTC") == "K2YE-5"        # via alias too

    def test_crosslink_makes_direct_even_without_route(self):
        """A live crosslink is proof of adjacency, even with no NODES route."""
        r = NetromRouter("W6ELA-1", "PALO")
        assert r.best_neighbor_for("KF6ANX-4") is None         # unknown, no crosslink
        r.note_crosslink("KF6ANX-4", up=True)
        assert r.is_direct_neighbor("KF6ANX-4") is True
        assert r.best_neighbor_for("KF6ANX-4") == "KF6ANX-4"
        r.note_crosslink("KF6ANX-4", up=False)
        assert r.is_direct_neighbor("KF6ANX-4") is False       # link gone

    def test_heard_direct_ignored_for_non_node(self):
        """Hearing a random (non-node) station directly must NOT make it a
        crosslink neighbor — known-node gating."""
        r = NetromRouter("W6ELA-1", "PALO")
        r.note_heard_direct("W1AW-9")          # a ham we heard, no NET/ROM presence
        assert r.is_direct_neighbor("W1AW-9") is False
        assert r.best_neighbor_for("W1AW-9") is None



class TestAdjacency:
    """N0.5 — the router as the single adjacency authority, combining NODES
    routes + RF direct-heard (note_heard_direct) + live crosslinks
    (note_crosslink)."""

    def _known(self):
        """N6ZX-5 (WBAY) as a NODES source (→ direct route to itself), which
        also advertises K2YE-5 (MONTC) as a transit destination."""
        r = NetromRouter("W6ELA-1", "PALO")
        asyncio.run(r.on_netrom_frame(
            "N6ZX-5", "NODES",
            _nodes_payload("WBAY", [NodeEntry("K2YE-5", "MONTC", "K2YE-5", 180)])))
        return r

    def test_nodes_source_is_direct(self):
        r = self._known()
        assert r.is_direct_neighbor("N6ZX-5") is True    # source → direct route
        assert r.is_direct_neighbor("K2YE-5") is False   # transit dest only

    def test_crosslink_is_proof(self):
        r = self._known()
        r.note_crosslink("K2YE-5", up=True)
        assert r.is_direct_neighbor("K2YE-5") is True    # live link overrides
        r.note_crosslink("K2YE-5", up=False)
        assert r.is_direct_neighbor("K2YE-5") is False

    def test_heard_direct_within_ttl(self):
        r = self._known()
        r.note_heard_direct("K2YE-5")                    # heard now
        assert r.is_direct_neighbor("K2YE-5") is True

    def test_heard_direct_ttl_expiry(self):
        r = self._known()
        r.note_heard_direct("K2YE-5", when=time.time() - 7200)  # 2h ago
        assert r.is_direct_neighbor("K2YE-5") is False   # default TTL 60m

    def test_graceful_without_heard_or_crosslink(self):
        """No heard/crosslink pushes → adjacency still works from NODES alone."""
        r = self._known()
        assert r.is_direct_neighbor("N6ZX-5") is True    # source direct route
        assert r.is_direct_neighbor("K2YE-5") is False   # transit only

    def test_adjacent_neighbors_is_coherent_set(self):
        r = self._known()
        assert r.adjacent_neighbors == {"N6ZX-5"}        # only the direct one
        r.note_crosslink("K2YE-5", up=True)
        assert r.adjacent_neighbors == {"N6ZX-5", "K2YE-5"}

    def test_best_neighbor_first_hop_hardening(self):
        """Prefer a transit route whose first hop we can reach directly over a
        higher-quality route whose first hop we cannot."""
        r = NetromRouter("W6ELA-1", "PALO")
        now = time.time()
        # WA6D-5 reachable two ways (post-restart seed style — no source-direct
        # routes present): higher-q via KO6UN-5 (unreachable), lower-q via N6ZX-5.
        r._upsert_route(RouteEntry("WA6D-5", "HOGAN", "WA6D-5", 200, "KO6UN-5", "UNRCH", now))
        r._upsert_route(RouteEntry("WA6D-5", "HOGAN", "WA6D-5", 150, "N6ZX-5", "WBAY", now))
        r.note_heard_direct("N6ZX-5")                    # only N6ZX-5 is reachable
        assert r.is_direct_neighbor("KO6UN-5") is False
        assert r.is_direct_neighbor("N6ZX-5") is True
        assert r.best_neighbor_for("WA6D-5") == "N6ZX-5"  # skips unreachable KO6UN-5

    def test_best_neighbor_best_effort_when_no_hop_confirmed(self):
        """If no first hop is confirmed reachable, fall back to the best route
        rather than refusing it outright."""
        r = NetromRouter("W6ELA-1", "PALO")
        now = time.time()
        r._upsert_route(RouteEntry("WA6D-5", "HOGAN", "WA6D-5", 200, "KO6UN-5", "UNRCH", now))
        assert r.best_neighbor_for("WA6D-5") == "KO6UN-5"  # best-effort
