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
import re
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
# How long an RF direct-hearing (note_heard_direct) counts as "still directly
# reachable" for adjacency.  Overridable via the constructor (engine reads
# netrom.direct_heard_ttl_minutes, default 60).
_DIRECT_HEARD_TTL_SECONDS = 60 * 60

# ── NET/ROM 1.3 routing PARMS (manual pp.65-67), mapped to config (N5) ─────────
# Default per-neighbour link ("path") quality for the RF channel — PARMS #3,
# manual default 192 (a 1200-baud user-accessed frequency).  Used to compose
# advertised route qualities on receive; see _route_quality().
_CHANNEL_QUALITY_DEFAULT  = 192
# PARMS #2: routes learned below this quality are ignored; 0 disables auto-
# routing entirely (ignore all NODES).  Manual default 1.
_WORST_QUALITY_DEFAULT    = 1
# PARMS #1: cap on the destination list (manual default 50, max 400); raised for
# a modern mesh.
_MAX_DESTINATIONS_DEFAULT = 200
# PARMS #5: obsolescence-count initialiser (manual default 6).  A route's obs
# count is (re)set to this on add/update, decremented each broadcast cycle, and
# the route deleted at 0.  0 disables decay (routes permanent).  obs_count == 0
# on a manually-added route marks it locked (never auto-decremented/deleted).
_OBS_INITIALIZER_DEFAULT = 6
# PARMS #6: only re-advertise routes with obs >= this (manual default 5).  Used
# by transit re-advertisement (N5c).
_OBS_MIN_TO_BROADCAST_DEFAULT = 5

# NODES broadcast framing (N6a — fidelity Gap 1).  A NODES broadcast is a series
# of AX.25 UI frames, each a 7-byte header (0xFF + 6-byte source alias) followed
# by 21-byte destination entries.  Real NET/ROM nodes cap each frame to fit one
# AX.25 info field and send multiple frames when the table is larger:
# (256 - 7) // 21 = 11 entries per frame.  Larger tables fragment across frames,
# each re-stamped with the header.
_NODES_HEADER_LEN            = 7
_NODES_ENTRY_LEN            = 21
_NODES_FRAME_BUDGET         = 256
_NODES_MAX_ENTRIES_PER_FRAME = (_NODES_FRAME_BUDGET - _NODES_HEADER_LEN) // _NODES_ENTRY_LEN  # 11


def _route_quality(advertised: int, path_quality: int) -> int:
    """NET/ROM receive-side route quality (1987 manual p.65, rule 5):

        routequality = ((advertised × path_quality) + 128) // 256

    Compose the neighbour's *advertised* route quality with OUR *link* (path)
    quality to that neighbour — both 0-255 fractions of 256 — rounded to the
    nearest 256th.  This is the step the pre-N5 router skipped (it stored the
    advertised quality verbatim), so transit-route qualities were never
    link-adjusted and mis-ranked next-hops."""
    return ((int(advertised) * int(path_quality)) + 128) >> 8

# A plausible AX.25 callsign: 1-2 char prefix, a call-area digit, a 1-4 letter
# suffix, optional -SSID (0-15).  Used to filter NODES junk — URONode emits
# pseudo-entries like ``##TEMP:ENABLE-0`` and ``SFRC:OFF-0`` for disabled /
# temporary node-table slots, and garbled callsigns arrive via the mesh — so
# that non-callsign destinations never enter the routing table, the heard DB,
# the map or the graph.  (Bit-flipped-but-valid callsigns, e.g. the ELSO
# WA6KQZ-5/WA6KWB-5 variants, pass this and need the separate alias-collision
# pass; this only catches things that are not callsigns at all.)
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,2}[0-9][A-Z]{1,4}(?:-(?:[0-9]|1[0-5]))?$")


def _is_routable_callsign(call: str) -> bool:
    """True iff *call* is a syntactically valid AX.25 callsign(-SSID)."""
    return bool(_CALLSIGN_RE.match((call or "").upper().strip()))


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
    obs_count: int = _OBS_INITIALIZER_DEFAULT  # obsolescence count (N5b); 0 = locked

    @property
    def is_direct(self) -> bool:
        """True when this is a direct link to the destination itself (we hear
        the dest on our own radio), rather than a transit route via another
        node.  Direct routes are auto-added with ``via_call == dest_call``."""
        return self.via_call.upper() == self.dest_call.upper()

    def __str__(self) -> str:
        age = int(time.time() - self.last_seen)
        alias = f"({self.alias})" if self.alias else ""
        return (
            f"{self.dest_call:<10} {alias:<8} nbr={self.neighbor_call:<10} "
            f"q={self.quality:3d} obs={self.obs_count}  "
            f"via {self.via_call} ({self.via_alias})  {age}s ago"
        )


@dataclass
class NeighbourEntry:
    """One entry in the neighbour list (1987 manual p.63): an adjacent node we
    can reach in one hop, with OUR link (``path``) quality to it.

    ``path_quality`` composes the advertised quality of every route learned via
    this neighbour (see :func:`_route_quality`).  N5a populates it; the full
    use-count lifecycle + obsolescence persistence land in N5b."""
    call:         str
    port:         str   = ""                        # transport/channel (single RF port for now)
    path_quality: int   = _CHANNEL_QUALITY_DEFAULT  # 0-255; 0 = ignore this neighbour
    use_count:    int   = 0                          # routes currently via it (N5b lifecycle)
    locked:       bool  = False                      # operator-set quality, never auto-updated
    crosslink:    bool  = False                      # a live AX.25 crosslink exists now (enrichment)
    last_heard:   float = 0.0                        # unix ts of last NODES / RF-direct hearing


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
        route_ttl_seconds:        int   = _ROUTE_TTL_SECONDS,
        hop_cost:                 int   = _HOP_COST,
        min_advert_quality:       int   = _MIN_ADVERT_QUALITY,
        advertise_self_only:      bool  = True,
        direct_heard_ttl_seconds: float = _DIRECT_HEARD_TTL_SECONDS,
        channel_quality:          int   = _CHANNEL_QUALITY_DEFAULT,
        neighbour_quality:        Optional[dict[str, int]] = None,
        worst_quality:            int   = _WORST_QUALITY_DEFAULT,
        max_destinations:         int   = _MAX_DESTINATIONS_DEFAULT,
        obs_initializer:          int   = _OBS_INITIALIZER_DEFAULT,
        obs_min_to_broadcast:     int   = _OBS_MIN_TO_BROADCAST_DEFAULT,
    ) -> None:
        self._call  = node_call.upper()
        self._alias = node_alias.upper()[:6]
        # dest_call (upper) → list of alternate routes, sorted by quality desc,
        # truncated to _MAX_ROUTES_PER_DEST.  Keyed for fast lookup.
        self._routes: dict[str, list[RouteEntry]] = {}
        # ── Neighbour list (N5, 1987 manual p.63) ────────────────────────────
        # Adjacent nodes + OUR link (path) quality to each — the vehicle for the
        # receive-side route-quality formula.  Keyed by callsign (upper).
        self._neighbours: dict[str, NeighbourEntry] = {}
        # PARMS-mapped routing knobs (see _route_quality / _process_nodes).
        self._channel_quality = max(0, min(255, int(channel_quality)))
        self._neighbour_quality = {
            str(k).upper(): max(0, min(255, int(v)))
            for k, v in (neighbour_quality or {}).items()
        }
        self._worst_quality  = max(0, min(255, int(worst_quality)))
        self._max_destinations = max(1, int(max_destinations))
        # Obsolescence-count lifecycle (N5b, PARMS #5/#6): routes decay a count
        # each broadcast cycle and are deleted at 0, instead of a hard TTL.
        self._obs_initializer = max(0, min(255, int(obs_initializer)))
        self._obs_min_to_broadcast = max(1, min(255, int(obs_min_to_broadcast)))
        # Persistence (N5b/b2): when set, the router snapshots its composed
        # routing + neighbour tables here so they survive a restart.  None ⇒ no
        # persistence (tests / heard disabled).
        self._db_path: Optional[str] = None
        self._nodes_observer: Optional[NodesFrameCallback] = None
        # ── Adjacency enrichment (N0.5) ──────────────────────────────────────
        # The router is the single authority for "is X a directly-reachable
        # NET/ROM neighbor?", combining three sources: NODES routes (above),
        # RF direct-heard (fed by the optional heard plugin via
        # note_heard_direct), and live crosslinks (fed by the transport via
        # note_crosslink).  Both maps are enrichment — absent sources just mean
        # less signal, never an error.
        self._heard_direct: dict[str, float] = {}   # call → unix ts heard DIRECT (RF)
        self._crosslinks:   set[str]          = set()  # calls with a live AX.25 crosslink
        self._direct_heard_ttl = float(direct_heard_ttl_seconds)
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

    # ── Adjacency enrichment (push API) ────────────────────────────────────────

    def note_heard_direct(self, call: str, when: float | None = None) -> None:
        """Enrich adjacency: we heard *call* DIRECTLY on the air (no digipeater).

        Pushed by the (optional) heard plugin on every direct hearing and once
        per seeded station at startup.  Enrichment only — if no source ever
        calls this, adjacency simply relies on live crosslinks + NODES routes.
        """
        self._heard_direct[call.upper()] = float(when) if when is not None else time.time()

    def note_crosslink(self, call: str, up: bool) -> None:
        """Enrich adjacency: a live AX.25 crosslink to *call* opened or closed.

        A live crosslink is definitive proof of one-hop reachability; pushed by
        the transport at crosslink establish / teardown.
        """
        c = call.upper()
        if up:
            self._crosslinks.add(c)
        else:
            self._crosslinks.discard(c)
            # A neighbour kept alive only by the (now-gone) crosslink and with no
            # routes should age out.
            self._reconcile_neighbours()

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
                # Sanitize BEFORE both consumers so the routing table AND the
                # heard plugin (via the observer) only ever see real callsigns.
                self._sanitize_entries(frame)
                self._process_nodes(frame)
                if self._nodes_observer is not None:
                    try:
                        await self._nodes_observer(frame)
                    except Exception:
                        logger.exception(
                            "netrom nodes observer error for frame from %s", src_call
                        )
                # Snapshot the (composed) tables so they survive a restart.
                await self.persist()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _sanitize_entries(self, frame: NodesFrame) -> int:
        """Drop NODES entries whose destination is not a real callsign.

        Filters URONode pseudo-entries (``##TEMP:ENABLE-0``, ``SFRC:OFF-0``) and
        garbled destinations in place, so neither the routing table nor the
        heard observer records them.  Returns the count dropped.
        """
        clean = [e for e in frame.entries if _is_routable_callsign(e.dest_call)]
        dropped = len(frame.entries) - len(clean)
        if dropped:
            if logger.isEnabledFor(logging.DEBUG):
                for e in frame.entries:
                    if not _is_routable_callsign(e.dest_call):
                        logger.debug(
                            "netrom: dropping non-callsign NODES dest %r (alias %r) from %s",
                            e.dest_call, e.alias, frame.source_call,
                        )
            frame.entries = clean
        return dropped

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

    # ── Neighbour list (N5) ─────────────────────────────────────────────────

    def _default_path_quality(self, call: str) -> int:
        """OUR link (path) quality to *call*: an operator override
        (``neighbour_quality`` config) if set, else the channel default
        (1987 manual p.65 rule 3)."""
        return self._neighbour_quality.get(call.upper(), self._channel_quality)

    def _get_or_create_neighbour(self, call: str, when: float) -> NeighbourEntry:
        """Ensure a neighbour-list entry for *call* (the source of a NODES
        broadcast is a one-hop neighbour), refreshing ``last_heard`` and, unless
        operator-locked, its channel-default path quality."""
        c = call.upper()
        nbr = self._neighbours.get(c)
        if nbr is None:
            nbr = NeighbourEntry(
                call=c, path_quality=self._default_path_quality(c), last_heard=when,
            )
            self._neighbours[c] = nbr
        else:
            nbr.last_heard = when
            if not nbr.locked:
                nbr.path_quality = self._default_path_quality(c)
        return nbr

    def _process_nodes(self, frame: NodesFrame) -> None:
        now = time.time()
        # Rule 1 (p.65): worst_quality == 0 disables auto-routing entirely.
        if self._worst_quality == 0:
            return
        new_count = 0
        updated_count = 0

        # The broadcast SOURCE is a one-hop neighbour (NODES are link-local):
        # ensure a neighbour-list entry with OUR link quality to it (rule 3).
        # path_quality == 0 means the operator has told us to ignore this
        # neighbour, including disregarding its broadcasts (manual p.23).
        nbr = self._get_or_create_neighbour(frame.source_call, now)
        path_q = nbr.path_quality
        if path_q == 0:
            return

        # Rule 4: a DIRECT route to the source exists at our link (path) quality.
        # (Also covers polite-client nodes whose header-only NODES carry no
        # entries — they still enter the table as a direct neighbour.)
        source_route = RouteEntry(
            dest_call     = frame.source_call,
            alias         = frame.source_alias,
            neighbor_call = frame.source_call,   # direct: neighbor == dest
            quality       = path_q,
            via_call      = frame.source_call,
            via_alias     = frame.source_alias,
            last_seen     = now,
            obs_count     = self._obs_initializer,   # (re)init on hearing (p.66)
        )
        if self._upsert_route(source_route):
            new_count += 1
        else:
            updated_count += 1

        # Rules 5-9: an INDIRECT route to each advertised destination via the
        # source, with the quality composed through our link to the source.
        for entry in frame.entries:
            dest_up = entry.dest_call.upper()
            if entry.neighbor_call.upper() == self._call:
                rq = 0                               # rule 6: trivial loop → q0
            else:
                rq = _route_quality(entry.quality, path_q)   # rule 5
            if rq < self._worst_quality:             # rule 8 (also drops q0 loops)
                continue
            if dest_up not in self._routes and \
                    len(self._routes) >= self._max_destinations:
                continue                             # rule 9: destination cap
            re = RouteEntry(
                dest_call=entry.dest_call,
                alias=entry.alias,
                neighbor_call=entry.neighbor_call,
                quality=rq,
                via_call=frame.source_call,
                via_alias=frame.source_alias,
                last_seen=now,
                obs_count=self._obs_initializer,     # (re)init on hearing (p.66)
            )
            if self._upsert_route(re):
                new_count += 1
            else:
                updated_count += 1

        logger.info(
            "netrom NODES from %s (%s): %d entries (%d new, %d updated) — "
            "path_q=%d, routing table now has %d destinations",
            frame.source_call, frame.source_alias,
            len(frame.entries), new_count, updated_count,
            path_q, len(self._routes),
        )
        if logger.isEnabledFor(logging.DEBUG):
            for e in frame.entries:
                logger.debug(
                    "  %-10s %-8s nbr=%-10s q=%3d",
                    e.dest_call, f"({e.alias})", e.neighbor_call, e.quality,
                )

    # ── Persistence (N5b/b2) ────────────────────────────────────────────────

    def set_db_path(self, db_path: str) -> None:
        """Enable persistence: the router snapshots its (composed) routing +
        neighbour tables to this DB after each NODES update and on the decay
        scan, so adjacency + link qualities survive a restart.  Unset ⇒ no
        persistence."""
        self._db_path = db_path or None

    async def _ensure_netrom_schema(self, db) -> None:
        """Create/upgrade the routing-table schema the router owns.  Additive
        only (no PK migration): create the tables if absent and add the N5
        ``obs_count`` column to a pre-N5 ``netrom_routes``."""
        await db.execute(
            "CREATE TABLE IF NOT EXISTS netrom_routes ("
            " dest_call TEXT NOT NULL COLLATE NOCASE,"
            " neighbor_call TEXT NOT NULL COLLATE NOCASE,"
            " alias TEXT NOT NULL DEFAULT '',"
            " quality INTEGER NOT NULL DEFAULT 0,"
            " via_call TEXT NOT NULL COLLATE NOCASE,"
            " via_alias TEXT NOT NULL DEFAULT '',"
            " last_seen INTEGER NOT NULL,"
            " obs_count INTEGER NOT NULL DEFAULT 6,"
            " PRIMARY KEY (dest_call, neighbor_call))"
        )
        async with db.execute("PRAGMA table_info(netrom_routes)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "obs_count" not in cols:
            await db.execute(
                "ALTER TABLE netrom_routes ADD COLUMN "
                "obs_count INTEGER NOT NULL DEFAULT 6"
            )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS netrom_neighbours ("
            " call TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,"
            " port TEXT NOT NULL DEFAULT '',"
            " path_quality INTEGER NOT NULL DEFAULT 192,"
            " use_count INTEGER NOT NULL DEFAULT 0,"
            " locked INTEGER NOT NULL DEFAULT 0,"
            " last_heard INTEGER NOT NULL DEFAULT 0)"
        )

    async def persist(self) -> None:
        """Snapshot the current routing + neighbour tables to the DB (full
        replace → the DB mirrors memory and stale rows are pruned for free).
        Composed qualities + obs counts are stored, closing the persistence
        seam.  Best-effort; no-op when persistence is disabled."""
        if not self._db_path:
            return
        import aiosqlite
        # De-dup routes by the DB key (dest_call, neighbor_call), keeping the
        # best-quality one — matches the table PK (rare same-neighbour alternates
        # collapse, as they did under the heard writer).
        best: dict[tuple[str, str], RouteEntry] = {}
        for bucket in self._routes.values():
            for r in bucket:
                k = (r.dest_call.upper(), r.neighbor_call.upper())
                if k not in best or r.quality > best[k].quality:
                    best[k] = r
        route_rows = [
            (r.dest_call.upper(), r.neighbor_call.upper(), r.alias, r.quality,
             r.via_call.upper(), r.via_alias, int(r.last_seen), r.obs_count)
            for r in best.values()
        ]
        nbr_rows = [
            (n.call.upper(), n.port, n.path_quality, n.use_count,
             1 if n.locked else 0, int(n.last_heard))
            for n in self._neighbours.values()
        ]
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                await self._ensure_netrom_schema(db)
                await db.execute("DELETE FROM netrom_routes")
                await db.executemany(
                    "INSERT INTO netrom_routes (dest_call, neighbor_call, alias,"
                    " quality, via_call, via_alias, last_seen, obs_count)"
                    " VALUES (?,?,?,?,?,?,?,?)", route_rows,
                )
                await db.execute("DELETE FROM netrom_neighbours")
                await db.executemany(
                    "INSERT INTO netrom_neighbours (call, port, path_quality,"
                    " use_count, locked, last_heard) VALUES (?,?,?,?,?,?)", nbr_rows,
                )
                await db.commit()
        except Exception:
            logger.warning("netrom router persist failed", exc_info=True)

    async def seed_from_db(self, db_path: str) -> int:
        """Restore the in-memory routing + neighbour tables from the DB at
        startup, so adjacency + composed link qualities survive a restart
        instead of rebuilding blind (and NODES TX has something to advertise
        immediately).  Only rows within the route TTL are loaded.  Missing
        tables (fresh deployment) ⇒ nothing to seed; returns the routes loaded.

        The nodes_observer is NOT fired for seeded entries — they are historical
        rather than freshly-received broadcasts."""
        import aiosqlite
        cutoff = int(time.time() - self._route_ttl_seconds)
        loaded = 0
        try:
            async with aiosqlite.connect(db_path, timeout=30) as db:
                await self._ensure_netrom_schema(db)
                # Neighbour list first, so adjacency is authoritative on restart.
                async with db.execute(
                    "SELECT call, port, path_quality, use_count, locked, last_heard "
                    "FROM netrom_neighbours WHERE last_heard >= ?", (cutoff,),
                ) as cur:
                    async for row in cur:
                        if not _is_routable_callsign(row[0]):
                            continue
                        c = row[0].upper()
                        self._neighbours[c] = NeighbourEntry(
                            call=c, port=row[1] or "",
                            path_quality=int(row[2]), use_count=int(row[3]),
                            locked=bool(row[4]), last_heard=float(row[5]),
                        )
                async with db.execute(
                    "SELECT dest_call, alias, neighbor_call, quality, "
                    "       via_call, via_alias, last_seen, obs_count "
                    "FROM netrom_routes WHERE last_seen >= ? "
                    "ORDER BY quality DESC", (cutoff,),
                ) as cursor:
                    async for row in cursor:
                        if not _is_routable_callsign(row[0]):
                            continue
                        entry = RouteEntry(
                            dest_call     = row[0],
                            alias         = row[1] or "",
                            neighbor_call = row[2],
                            quality       = int(row[3]),
                            via_call      = row[4],
                            via_alias     = row[5] or "",
                            last_seen     = float(row[6]),
                            obs_count     = int(row[7]),
                        )
                        bucket = self._routes.setdefault(entry.dest_call.upper(), [])
                        if any(r.via_call.upper() == entry.via_call.upper()
                               for r in bucket):
                            continue
                        bucket.append(entry)
                        bucket.sort(key=lambda r: r.quality, reverse=True)
                        if len(bucket) > _MAX_ROUTES_PER_DEST:
                            del bucket[_MAX_ROUTES_PER_DEST:]
                        loaded += 1
        except aiosqlite.OperationalError as exc:
            logger.debug("netrom router seed: %s", exc)
            return 0
        if loaded or self._neighbours:
            logger.info(
                "netrom router seeded %d route(s), %d neighbour(s) from DB "
                "(%d destinations)",
                loaded, len(self._neighbours), len(self._routes),
            )
        return loaded

    # ── Maintenance ───────────────────────────────────────────────────────────

    def prune_stale_routes(self, now: float | None = None) -> int:
        """
        Remove RouteEntry records that haven't been refreshed within the TTL.
        If a destination loses all its routes, the destination key is removed.
        Returns the count of pruned entries.

        Superseded by :meth:`decay_obsolescence` under N5b (the engine now runs
        the obsolescence-count scan instead of this TTL prune); kept for the
        ``seed_from_db`` cutoff and back-compat.
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

    def decay_obsolescence(self) -> int:
        """Decrement every route's obsolescence count and delete routes that
        reach 0 (1987 manual p.66) — the periodic scan the engine runs at the
        NODES broadcast cadence, replacing the hard-TTL prune.

        A route's ``obs_count`` is (re)set to ``obs_initializer`` whenever it is
        heard/updated, so an actively-broadcast route never ages out, while one
        that goes quiet is deleted after ``obs_initializer`` missed cycles —
        giving the stable, ROCK-like table the TTL model lacked.  ``obs_count ==
        0`` marks a **locked** (manually-added) route: never decremented or
        deleted.  ``obs_initializer == 0`` disables decay entirely (permanent
        routes).  Returns the number of routes deleted."""
        if self._obs_initializer == 0:
            return 0
        deleted = 0
        empty_keys: list[str] = []
        for key, bucket in self._routes.items():
            kept: list[RouteEntry] = []
            for r in bucket:
                if r.obs_count == 0:          # locked — never auto-touched
                    kept.append(r)
                    continue
                r.obs_count -= 1
                if r.obs_count > 0:
                    kept.append(r)
                else:
                    deleted += 1              # reached 0 → delete (never sits at 0)
            if kept:
                bucket[:] = kept
            else:
                empty_keys.append(key)
        for k in empty_keys:
            del self._routes[k]
        # Age out neighbour-list entries whose routes all decayed away.
        self._reconcile_neighbours()
        return deleted

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
        """Uppercased callsigns of all nodes we can currently reach in ONE hop.

        N5b: the neighbour list (entries with a non-zero path quality), plus any
        live crosslink, plus any dest reachable by a fresh direct route (the last
        covers routes seeded from the DB before the neighbour list is persisted).
        Filtered through :meth:`is_direct_neighbor` so the same authority governs
        both the set and single-call classification.
        """
        candidates = set(self._neighbours) | set(self._crosslinks)
        candidates |= {
            r.dest_call.upper()
            for bucket in self._routes.values()
            for r in bucket if r.is_direct
        }
        return {c for c in candidates if self.is_direct_neighbor(c)}

    @property
    def neighbour_list(self) -> list["NeighbourEntry"]:
        """The neighbour table (N5), sorted by callsign — the classic NET/ROM
        ``ROUTES`` view (call, port, path quality, use-count, locked)."""
        return sorted(self._neighbours.values(), key=lambda n: n.call)

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

    def _is_known_node(self, call: str) -> bool:
        """True iff *call* is a NET/ROM node we know about — a destination in the
        routing table (NODES auto-adds every broadcast source as a dest), a
        neighbor that advertised routes to us (a route ``via_call``), or a node
        we currently have a crosslink to.  Gates :meth:`is_direct_neighbor` so
        that hearing a random (non-node) station's beacon directly does not make
        it look like a crosslink peer."""
        c = call.upper()
        if c in self._routes or c in self._crosslinks:
            return True
        return any(
            r.via_call.upper() == c
            for routes in self._routes.values()
            for r in routes
        )

    def _has_fresh_direct_route(self, call: str) -> bool:
        """True iff we hold a *direct* NODES route to *call* (``via_call ==
        dest_call``) still within the route TTL."""
        cutoff = time.time() - self._route_ttl_seconds
        return any(
            r.is_direct and r.last_seen >= cutoff
            for r in self._routes.get(call.upper(), ())
        )

    def is_direct_neighbor(self, call: str) -> bool:
        """The ONE adjacency authority: True iff *call* is a directly-reachable
        NET/ROM node — reachable in a single AX.25 hop right now.

        N5b: the **neighbour list** is the authority (a node is a neighbour once
        it broadcasts NODES, and is aged out by the obsolescence lifecycle when
        it goes quiet).  Layered with bbs2's enrichments:
          - a **live crosslink** to it (definitive proof), OR
          - it is in the **neighbour list** with a non-zero path quality, OR
          - it is a **known node** heard **directly** on the air within the
            direct-heard TTL (covers a node we hear as a beacon but that does
            not send us NODES).

        Used for inbound classification (crosslink vs BBS user), outbound
        next-hop selection, and INTERLOCK.
        """
        c = call.upper()
        if c in self._crosslinks:
            return True
        nbr = self._neighbours.get(c)
        if nbr is not None and nbr.path_quality > 0:
            return True
        if self._is_known_node(c) and \
                time.time() - self._heard_direct.get(c, 0.0) <= self._direct_heard_ttl:
            return True
        # Enrichment: a fresh *direct* route (via == dest) also implies one-hop
        # reachability — covers routes seeded from the DB after a restart, before
        # the neighbour list is persisted (N5b/b), and legacy direct routes.
        return self._has_fresh_direct_route(c)

    def _reconcile_neighbours(self) -> None:
        """Recompute each neighbour's use-count (routes forwarding via it) and
        delete an entry that carries no routes and has no live crosslink, unless
        operator-locked (manual p.23: an unlocked neighbour whose use-count
        reaches 0 is removed).  Called after the obsolescence scan."""
        use_count: dict[str, int] = {}
        for bucket in self._routes.values():
            for r in bucket:
                v = r.via_call.upper()
                use_count[v] = use_count.get(v, 0) + 1
        for c in list(self._neighbours):
            nbr = self._neighbours[c]
            nbr.use_count = use_count.get(c, 0)
            if nbr.use_count == 0 and not nbr.locked and c not in self._crosslinks:
                del self._neighbours[c]

    def best_neighbor_for(self, dest: str, *, min_quality: int = 1) -> str | None:
        """The neighbor to open a crosslink to in order to reach *dest* (callsign
        or alias) — the outbound next-hop for originating a circuit.

        1. If *dest* itself is directly reachable, crosslink straight to it (a
           fast 1-hop SABM) — even with only transit routes to it, or none.
        2. Otherwise the highest-quality transit route whose **first hop
           (via_call) we can also reach directly** (first-hop hardening — avoids
           a RETRYOUT on a via_call we only hear through a digipeater).
        3. Best-effort fallback: the highest-quality route even if we can't
           confirm the first hop, rather than refusing a known route.

        Returns ``None`` if *dest* is unknown or below *min_quality*.
        """
        routes = self.get_routes(dest)
        dest_call = routes[0].dest_call.upper() if routes else dest.upper()
        # 1. Direct to the destination itself.
        if _is_routable_callsign(dest_call) and self.is_direct_neighbor(dest_call):
            return dest_call
        if not routes:
            return None
        usable = [r for r in routes if r.quality >= min_quality]
        if not usable:
            return None
        # 2. Prefer a transit route whose first hop is directly reachable.
        reachable = next(
            (r for r in usable if self.is_direct_neighbor(r.via_call)), None
        )
        chosen = reachable or usable[0]   # 3. else best-effort (quality-sorted)
        return chosen.via_call.upper()

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
          - ``quality`` = our already-composed route quality; the receiver
            re-composes it through *their* link to us, so multi-hop degradation
            is inherent (N5 — ``hop_cost`` / ``min_advert_quality`` are retired).
          - gated on ``obs_min_to_broadcast`` (manual p.66 PARMS #6): stale
            routes are not propagated; ``obs_count == 0`` (locked) always is.
        Returns None when nothing would be advertised.

        Only enable transit-node mode if the network admins have explicitly
        agreed — re-broadcasting a learned routing table is reserved for
        high-reachability transit nodes by NORCAL convention.

        This is the SINGLE-frame form (back-compat; used by tests and small
        tables).  The broadcast path uses :meth:`build_nodes_payloads`, which
        fragments a large table into multiple frames (N6a — fidelity Gap 1).
        """
        if self._advertise_self_only:
            return encode_nodes_broadcast(self._alias, [])
        entries = self._nodes_entries()
        if not entries:
            return None
        return encode_nodes_broadcast(self._alias, entries)

    def build_nodes_payloads(self) -> list[bytes]:
        """NODES broadcast payload(s), one per AX.25 UI frame (N6a).

        A NODES broadcast is a *series* of UI frames, each a 7-byte header
        (0xFF + alias) plus up to ``_NODES_MAX_ENTRIES_PER_FRAME`` (11) 21-byte
        destination entries — a bigger routing table is fragmented across frames,
        each re-stamped with the header (manual §"Node Broadcasts").  The AGWPE
        NODES loop sends one UI frame per returned payload.

        Returns ``[]`` when nothing would be advertised.  Polite-client mode
        (``advertise_self_only``) always returns exactly one header-only frame.
        """
        if self._advertise_self_only:
            return [encode_nodes_broadcast(self._alias, [])]
        entries = self._nodes_entries()
        if not entries:
            return []
        step = _NODES_MAX_ENTRIES_PER_FRAME
        return [
            encode_nodes_broadcast(self._alias, entries[i:i + step])
            for i in range(0, len(entries), step)
        ]

    def _nodes_entries(self) -> list[NodeEntry]:
        """Transit-mode advertisement entries: the best route per destination,
        obsolescence-gated, sorted for stable wire output.  Empty in
        polite-client mode's callers (they never call this) or when nothing is
        advertisable.  Shared by both build_nodes_payload(s)."""
        entries: list[NodeEntry] = []
        for bucket in self._routes.values():
            if not bucket:
                continue
            best = bucket[0]
            # Obsolescence gate (p.66): don't propagate stale routes.  A locked
            # route (obs 0) is permanent and always advertised.
            if best.obs_count != 0 and best.obs_count < self._obs_min_to_broadcast:
                continue
            entries.append(NodeEntry(
                dest_call=best.dest_call,
                alias=best.alias,
                neighbor_call=best.via_call,  # OUR next hop, not upstream's
                quality=best.quality,          # composed; receiver re-composes
            ))
        # Sort for stable wire output (helps tests + monitoring diff).
        entries.sort(key=lambda e: e.dest_call.upper())
        return entries
