"""
bbs/netrom/router.py — NETROM routing table.

Receives raw NETROM frames from a transport observer, decodes NODES
broadcasts, and maintains a live view of the reachable network.

Spec-compliance notes
─────────────────────
- Up to ``_MAX_ROUTES_PER_DEST`` alternates kept per destination, sorted by
  quality (highest first).  Same neighbor refreshes its own entry.
- Routes expire after ``route_ttl_seconds`` of silence from the advertiser
  (default 3× the typical broadcast interval).  Pruning is done by
  ``prune_stale_routes()`` which the engine calls on a periodic task.
- When building our own NODES broadcast we degrade quality by ``hop_cost``
  and substitute our own adjacent neighbor (``via_call``) for the
  ``neighbor_call`` field — the upstream's neighbor is meaningless to our
  peers.  Routes whose degraded quality falls below
  ``min_advert_quality`` are not advertised.

Usage
─────
    router = NetromRouter(node_call="W6ELA-1", node_alias="PALO")
    transport.set_netrom_observer(router.on_netrom_frame)
    for route in router.routing_table:
        print(route)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from bbs.ax25.netrom_frame import (
    NodeEntry,
    NodesFrame,
    decode_nodes_broadcast,
    encode_nodes_broadcast,
)

logger = logging.getLogger(__name__)

NodesFrameCallback = Callable[["NodesFrame"], Awaitable[None]]

# Defaults — overridable through the NetromRouter constructor.
_ROUTE_TTL_SECONDS    = 3 * 3600   # 3 h; 3× the default 30-min broadcast cadence
_HOP_COST             = 25         # quality decrement applied on re-broadcast
_MIN_ADVERT_QUALITY   = 10         # don't propagate routes below this after decrement
_MAX_ROUTES_PER_DEST  = 3          # per NET/ROM spec
# NORCAL convention for direct-RF neighbors.  Used when auto-adding the
# source of a received NODES broadcast as a direct route to itself.
_DIRECT_NEIGHBOR_QUALITY = 192


@dataclass
class RouteEntry:
    """One entry in the local routing table."""
    dest_call: str        # destination node callsign
    alias: str            # destination node alias (may be empty)
    neighbor_call: str    # neighbor_call as advertised by the upstream
    quality: int          # quality as advertised by the upstream
    via_call: str         # OUR adjacent neighbor (the broadcaster of this entry)
    via_alias: str        # alias of that adjacent neighbor
    last_seen: float      # unix timestamp of last NODES broadcast containing this entry

    def __str__(self) -> str:
        age = int(time.time() - self.last_seen)
        alias = f"({self.alias})" if self.alias else ""
        return (
            f"{self.dest_call:<10} {alias:<8} nbr={self.neighbor_call:<10} "
            f"q={self.quality:3d}  via {self.via_call} ({self.via_alias})  "
            f"{age}s ago"
        )


class NetromRouter:
    """
    Maintains a NETROM routing table built from received NODES broadcasts.

    Thread-safety: designed for use inside a single asyncio event loop.
    All methods are synchronous except on_netrom_frame().
    """

    def __init__(
        self,
        node_call: str,
        node_alias: str,
        *,
        route_ttl_seconds:    int  = _ROUTE_TTL_SECONDS,
        hop_cost:             int  = _HOP_COST,
        min_advert_quality:   int  = _MIN_ADVERT_QUALITY,
        advertise_self_only:  bool = True,
    ) -> None:
        self._call  = node_call.upper()
        self._alias = node_alias.upper()[:6]
        # dest_call (upper) → list of alternate routes, sorted by quality desc,
        # truncated to _MAX_ROUTES_PER_DEST.  Keyed for fast lookup.
        self._routes: dict[str, list[RouteEntry]] = {}
        self._nodes_observer: Optional[NodesFrameCallback] = None
        self._route_ttl_seconds  = route_ttl_seconds
        self._hop_cost           = hop_cost
        self._min_advert_quality = min_advert_quality
        # Polite-client mode: when True, build_nodes_payload() emits only the
        # 7-byte header (discriminator + alias) instead of re-advertising
        # the full routing table.  This matches the NORCAL convention that
        # client/endpoint nodes (BBSes, user stations) announce only their
        # own presence, leaving full-table re-advertisement to high-level
        # transit nodes.  Header-only is what KI6ZHD-5 and similar client
        # stations transmit on the live 145.05 network — peers add the
        # source as a direct neighbor on RX regardless of entry count.
        self._advertise_self_only = advertise_self_only

    def set_nodes_observer(self, cb: NodesFrameCallback) -> None:
        """Register *cb* to be called after each NODES broadcast is processed."""
        self._nodes_observer = cb

    # ── Transport observer ────────────────────────────────────────────────────

    async def on_netrom_frame(
        self, src_call: str, dest_call: str, payload: bytes
    ) -> None:
        """
        Called by the transport for every received NETROM UI frame.
        src_call / dest_call come from the AX.25 envelope.
        payload is the raw binary AX.25 info field (PID byte already stripped).
        """
        if dest_call.upper() == "NODES":
            frame = decode_nodes_broadcast(src_call, payload)
            if frame is not None:
                self._process_nodes(frame)
                if self._nodes_observer is not None:
                    try:
                        await self._nodes_observer(frame)
                    except Exception:
                        logger.exception(
                            "netrom nodes observer error for frame from %s", src_call
                        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _upsert_route(self, entry: RouteEntry) -> bool:
        """Insert or refresh *entry* keyed on (dest_call, via_call).

        Returns True if the entry was newly added, False if it replaced an
        existing one.  Bucket is kept sorted by quality desc and truncated
        to ``_MAX_ROUTES_PER_DEST`` alternates per spec.
        """
        key = entry.dest_call.upper()
        via_call_up = entry.via_call.upper()
        bucket = self._routes.setdefault(key, [])
        existing_idx = next(
            (i for i, r in enumerate(bucket)
             if r.via_call.upper() == via_call_up),
            None,
        )
        is_new = existing_idx is None
        if is_new:
            bucket.append(entry)
        else:
            bucket[existing_idx] = entry
        bucket.sort(key=lambda r: r.quality, reverse=True)
        if len(bucket) > _MAX_ROUTES_PER_DEST:
            del bucket[_MAX_ROUTES_PER_DEST:]
        return is_new

    def _process_nodes(self, frame: NodesFrame) -> None:
        now = time.time()
        new_count = 0
        updated_count = 0
        via_call_up  = frame.source_call.upper()

        # Auto-add the broadcast SOURCE as a direct neighbor.  Receiving a
        # NODES broadcast implies the source is directly RF-reachable — we
        # just heard them.  Without this, polite-client nodes that only
        # send Len=7 header-only NODES (like KI6ZHD-5 / SCLARA on 145.05)
        # would never appear in our routing table because they're not
        # included as entries in their own broadcasts.  Quality 192 matches
        # the NORCAL convention for direct-RF NETROM neighbors.
        source_route = RouteEntry(
            dest_call     = frame.source_call,
            alias         = frame.source_alias,
            neighbor_call = frame.source_call,   # direct: neighbor == dest
            quality       = _DIRECT_NEIGHBOR_QUALITY,
            via_call      = frame.source_call,   # we hear them on our radio
            via_alias     = frame.source_alias,
            last_seen     = now,
        )
        if self._upsert_route(source_route):
            new_count += 1
        else:
            updated_count += 1

        for entry in frame.entries:
            re = RouteEntry(
                dest_call=entry.dest_call,
                alias=entry.alias,
                neighbor_call=entry.neighbor_call,
                quality=entry.quality,
                via_call=frame.source_call,
                via_alias=frame.source_alias,
                last_seen=now,
            )
            if self._upsert_route(re):
                new_count += 1
            else:
                updated_count += 1

        logger.info(
            "netrom NODES from %s (%s): %d entries (%d new, %d updated) — "
            "routing table now has %d destinations",
            frame.source_call, frame.source_alias,
            len(frame.entries), new_count, updated_count,
            len(self._routes),
        )
        if logger.isEnabledFor(logging.DEBUG):
            for e in frame.entries:
                logger.debug(
                    "  %-10s %-8s nbr=%-10s q=%3d",
                    e.dest_call, f"({e.alias})", e.neighbor_call, e.quality,
                )

    # ── Persistence ───────────────────────────────────────────────────────────

    async def seed_from_db(self, db_path: str) -> int:
        """Restore in-memory routes from the heard plugin's netrom_routes table.

        Called once at engine startup so the router does not begin every
        session with an empty table — otherwise build_nodes_payload() would
        return None for the first ~30 minutes after each restart and we'd be
        invisible on the air during that window.

        Only rows whose last_seen is within self._route_ttl_seconds of now
        are loaded.  The nodes_observer is NOT fired for seeded entries —
        they are historical rather than freshly-received broadcasts, and
        re-emitting them through the heard pipeline would cause spurious
        update churn against rows that are already there.

        Missing table (heard plugin disabled or fresh deployment) is treated
        as "nothing to seed" — returns 0, no exception.

        Returns the number of routes loaded.
        """
        import aiosqlite
        cutoff = int(time.time() - self._route_ttl_seconds)
        loaded = 0
        try:
            async with aiosqlite.connect(db_path, timeout=30) as db:
                async with db.execute(
                    "SELECT dest_call, alias, neighbor_call, quality, "
                    "       via_call, via_alias, last_seen "
                    "FROM netrom_routes WHERE last_seen >= ? "
                    "ORDER BY quality DESC",
                    (cutoff,),
                ) as cursor:
                    async for row in cursor:
                        entry = RouteEntry(
                            dest_call     = row[0],
                            alias         = row[1] or "",
                            neighbor_call = row[2],
                            quality       = int(row[3]),
                            via_call      = row[4],
                            via_alias     = row[5] or "",
                            last_seen     = float(row[6]),
                        )
                        key = entry.dest_call.upper()
                        bucket = self._routes.setdefault(key, [])
                        # Skip duplicates (defensive — table PK should
                        # prevent them, but seeding may run after a
                        # partial in-memory population in odd cases).
                        if any(r.via_call.upper() == entry.via_call.upper()
                               for r in bucket):
                            continue
                        bucket.append(entry)
                        bucket.sort(key=lambda r: r.quality, reverse=True)
                        if len(bucket) > _MAX_ROUTES_PER_DEST:
                            del bucket[_MAX_ROUTES_PER_DEST:]
                        loaded += 1
        except aiosqlite.OperationalError as exc:
            # "no such table: netrom_routes" — heard plugin never ran, or
            # this is a fresh deployment.  Treat as empty seed.
            logger.debug("netrom router seed: %s", exc)
            return 0
        if loaded:
            logger.info(
                "netrom router seeded %d route(s) from heard DB "
                "(%d destinations)",
                loaded, len(self._routes),
            )
        return loaded

    # ── Maintenance ───────────────────────────────────────────────────────────

    def prune_stale_routes(self, now: float | None = None) -> int:
        """
        Remove RouteEntry records that haven't been refreshed within the TTL.
        If a destination loses all its routes, the destination key is removed.
        Returns the count of pruned entries.
        """
        cutoff = (now if now is not None else time.time()) - self._route_ttl_seconds
        pruned = 0
        empty_keys: list[str] = []
        for key, bucket in self._routes.items():
            kept = [r for r in bucket if r.last_seen >= cutoff]
            pruned += len(bucket) - len(kept)
            if kept:
                bucket[:] = kept
            else:
                empty_keys.append(key)
        for k in empty_keys:
            del self._routes[k]
        return pruned

    # ── Public accessors ──────────────────────────────────────────────────────

    @property
    def routing_table(self) -> list[RouteEntry]:
        """
        All known routes (including alternates), sorted by alias then dest
        callsign for display.  Multiple entries with the same dest_call are
        the alternate routes for that destination.
        """
        flat: list[RouteEntry] = []
        for bucket in self._routes.values():
            flat.extend(bucket)
        flat.sort(key=lambda r: (
            r.alias.upper() or r.dest_call.upper(),
            r.dest_call.upper(),
            -r.quality,
        ))
        return flat

    @property
    def node_count(self) -> int:
        """Distinct destination callsigns in the table."""
        return len(self._routes)

    @property
    def adjacent_neighbors(self) -> set[str]:
        """Uppercased callsigns of all our direct NETROM neighbors.

        A direct neighbor is any node from which we've received a NODES
        broadcast — that's recorded as the ``via_call`` on every route
        we've learned from them.  Used by the AGWPE transport to classify
        incoming AX.25 connections on 'C': a connection from a known
        neighbor is treated as a NETROM crosslink (no BBS banner sent,
        wait for the L3 CONNECT REQ), one from an unknown caller is
        treated as a direct BBS user.

        The set is computed on demand from current router state; cheap
        for typical NORCAL-scale tables (under 100 destinations).
        """
        return {
            r.via_call.upper()
            for routes in self._routes.values()
            for r in routes
        }

    def get_route(self, dest: str) -> RouteEntry | None:
        """
        Look up the BEST route to *dest* (callsign or alias, case-insensitive).
        Returns None if no route is known.
        """
        dest_up = dest.upper()
        bucket = self._routes.get(dest_up)
        if bucket:
            return bucket[0]
        # Fall back to alias search
        for routes in self._routes.values():
            if routes and routes[0].alias.upper() == dest_up:
                return routes[0]
        return None

    def get_routes(self, dest: str) -> list[RouteEntry]:
        """
        Return all known routes to *dest* (callsign or alias, case-insensitive)
        in quality order (best first).  Empty list when nothing is known.
        """
        dest_up = dest.upper()
        bucket = self._routes.get(dest_up)
        if bucket:
            return list(bucket)
        for routes in self._routes.values():
            if routes and routes[0].alias.upper() == dest_up:
                return list(routes)
        return []

    def build_nodes_payload(self) -> bytes | None:
        """
        Build the NETROM NODES broadcast payload to advertise to our neighbors.

        Two modes:

        **Polite-client mode (``advertise_self_only=True``, the default)**
        Emit only the 7-byte header (discriminator + alias). Peers add the
        broadcast source as a direct neighbor on RX regardless of entry
        count — this is how live-network client stations like KI6ZHD-5
        (SCLARA) announce themselves on 145.05. The payload is independent
        of routing-table state, so we always have something to broadcast,
        including at cold start when ``_routes`` is empty.

        **Transit-node mode (``advertise_self_only=False``)**
        For each destination, advertise the BEST known route with:
          - ``neighbor_call`` = our OWN adjacent neighbor (the route's via_call),
            so peers know which AX.25 link to use when they forward through us.
          - ``quality`` decremented by hop_cost; routes that fall below
            min_advert_quality are skipped.
        Returns None when nothing would be advertised.

        Only enable transit-node mode if the network admins have explicitly
        agreed — re-broadcasting a learned routing table is reserved for
        high-reachability transit nodes by NORCAL convention.
        """
        if self._advertise_self_only:
            return encode_nodes_broadcast(self._alias, [])

        if not self._routes:
            return None
        entries: list[NodeEntry] = []
        for key, bucket in self._routes.items():
            if not bucket:
                continue
            best = bucket[0]
            degraded = max(0, best.quality - self._hop_cost)
            if degraded < self._min_advert_quality:
                continue
            entries.append(NodeEntry(
                dest_call=best.dest_call,
                alias=best.alias,
                neighbor_call=best.via_call,  # OUR next hop, not upstream's
                quality=degraded,
            ))
        if not entries:
            return None
        # Sort for stable wire output (helps tests + monitoring diff).
        entries.sort(key=lambda e: e.dest_call.upper())
        return encode_nodes_broadcast(self._alias, entries)
