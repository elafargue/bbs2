"""
bbs/plugins/heard/heard.py — Heard Stations plugin.

Maintains a log of AX.25 stations heard by the BBS on RF transports (KISS,
AGWPE).  Any station that transmits a UI frame — beacon, APRS packet, etc. —
while the BBS is monitoring is recorded here.

The on_heard() method is called directly by the transport layer when a frame
is received that is NOT addressed to the BBS callsign.  The engine wires this
up automatically at startup when the plugin is enabled.

Access: IDENTIFIED — any station with a callsign can view the list.
Sysop:  can configure max_age_hours interactively or via the web UI.

Schema (v2)
-----------
  stations      — one row per callsign; station identity, position, aliases.
  heard_events  — one row per (callsign, transport); per-transport observation data.
  heard_paths   — one row per (callsign, transport, via_base); path history.
  netrom_routes — NET/ROM routing table; PK (dest_call, neighbor_call).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional, TYPE_CHECKING

from bbs.ax25.netrom_frame import NodesFrame

import aiosqlite

logger = logging.getLogger(__name__)

from bbs.core.auth import AuthLevel
from bbs.core.plugin_registry import BBSPlugin
from bbs.plugins.heard.graph import confirmed_edges

if TYPE_CHECKING:
    from bbs.core.session import BBSSession
    from bbs.core.terminal import Terminal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    callsign         TEXT    PRIMARY KEY NOT NULL COLLATE NOCASE,
    base_call        TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
    lat              REAL,
    lon              REAL,
    comment          TEXT    NOT NULL DEFAULT '',
    position_source  TEXT    NOT NULL DEFAULT '',
    position_ts      INTEGER NOT NULL DEFAULT 0,
    netrom_alias     TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
    beacon_alias     TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
    kanode_alias     TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
    service          TEXT    NOT NULL DEFAULT '',
    last_beacon_text TEXT    NOT NULL DEFAULT '',
    last_beacon_ts   INTEGER NOT NULL DEFAULT 0,
    first_seen       INTEGER NOT NULL DEFAULT 0,
    last_seen        INTEGER NOT NULL DEFAULT 0
);
-- A physical station: groups the callsign-SSIDs (and, via the Ka-Node merge,
-- tactical digi aliases) that belong to one operator/site, keyed on the
-- SSID-stripped base callsign.  Canonical fields are sysop-set; positions and
-- aliases otherwise roll up from member `stations` rows at read time.
CREATE TABLE IF NOT EXISTS station_entities (
    base_call          TEXT    PRIMARY KEY NOT NULL COLLATE NOCASE,
    canonical_nodename TEXT    NOT NULL DEFAULT '',
    notes              TEXT    NOT NULL DEFAULT '',
    lat                REAL,
    lon                REAL,
    position_source    TEXT    NOT NULL DEFAULT '',
    first_seen         INTEGER NOT NULL DEFAULT 0,
    last_seen          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stations_base_call ON stations (base_call);
CREATE TABLE IF NOT EXISTS heard_events (
    callsign          TEXT    NOT NULL COLLATE NOCASE,
    transport         TEXT    NOT NULL DEFAULT '',
    source            TEXT    NOT NULL DEFAULT 'heard',
    first_heard       INTEGER NOT NULL,
    last_heard        INTEGER NOT NULL,
    count             INTEGER NOT NULL DEFAULT 0,
    last_direct_heard INTEGER NOT NULL DEFAULT 0,
    dest              TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
    via               TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (callsign, transport)
);
CREATE TABLE IF NOT EXISTS heard_paths (
    callsign    TEXT    NOT NULL COLLATE NOCASE,
    transport   TEXT    NOT NULL DEFAULT '',
    via_base    TEXT    NOT NULL DEFAULT '',
    via         TEXT    NOT NULL DEFAULT '',
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (callsign, transport, via_base)
);
CREATE TABLE IF NOT EXISTS heard_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS netrom_routes (
    dest_call     TEXT NOT NULL COLLATE NOCASE,
    neighbor_call TEXT NOT NULL COLLATE NOCASE,
    alias         TEXT NOT NULL DEFAULT '',
    quality       INTEGER NOT NULL DEFAULT 0,
    via_call      TEXT NOT NULL COLLATE NOCASE,
    via_alias     TEXT NOT NULL DEFAULT '',
    last_seen     INTEGER NOT NULL,
    PRIMARY KEY (dest_call, neighbor_call)
);
"""

_DEFAULT_MAX_AGE_HOURS = 24
_DIRECT_GRACE_SECONDS = 120
# Cap on the stored last-beacon/ID text so a long payload can't bloat a row.
_MAX_BEACON_TEXT = 256

# Heard-schema version, tracked in heard_settings['heard_schema_version'].
# v1 = consolidation of the pre-versioning legacy migrations (each idempotent
#      and guarded) plus dropping the orphaned heard_stations table.
# v2 = add stations.service + last_beacon_text + last_beacon_ts.
# v3 = add station_entities + stations.base_call; group SSIDs into physical
#      stations by base callsign.
# v4 = add stations.position_ts (when a position was last written) so the
#      physical-station reference position is the freshest beacon across SSIDs.
_HEARD_SCHEMA_VERSION = 4


def _merge_via(stored: str, incoming: str) -> str:
    """
    Merge two via path strings by OR-ing the has-been-repeated (*) flags.

    The same beacon is often received multiple times — once per digipeater
    that re-transmits it.  Each copy has the H-bit set only for the digis
    that have already forwarded it at the time of that particular reception.
    By OR-ing the flags we accumulate all heard repeaters:

        stored   = "KJOHN*,KBULN,WOODY,KBETH"
        incoming = "KJOHN*,KBULN,WOODY*,KBETH"
        merged   = "KJOHN*,KBULN,WOODY*,KBETH"

    If the path structures differ (different callsigns or different length)
    the incoming string is returned as-is.
    """
    if not stored:
        return incoming
    if not incoming:
        return stored
    stored_parts   = [v.strip() for v in stored.split(",")   if v.strip()]
    incoming_parts = [v.strip() for v in incoming.split(",") if v.strip()]
    if len(stored_parts) != len(incoming_parts):
        return incoming

    def _base(entry: str) -> str:
        return entry.rstrip("*")

    if [_base(p) for p in stored_parts] != [_base(p) for p in incoming_parts]:
        return incoming  # different digipeater chains

    return ",".join(
        _base(s) + ("*" if s.endswith("*") or n.endswith("*") else "")
        for s, n in zip(stored_parts, incoming_parts)
    )


# ── <MAP:lat,lon,call[,nodename]> location-beacon tag ─────────────────────────

_MAP_TAG_RE = re.compile(
    r"<MAP:\s*"
    r"([+-]?\d+(?:\.\d+)?)\s*,"            # lat
    r"\s*([+-]?\d+(?:\.\d+)?)\s*,"         # lon
    r"\s*([A-Z0-9\-]+)"                    # callsign
    r"(?:\s*,\s*([A-Z0-9\-/]+))?"           # optional nodename (may use XXXX/YYYY for dual-alias nodes)
    r"\s*>",
    re.IGNORECASE,
)


def _parse_map_tag(info: str) -> tuple[float, float, str, str] | None:
    """
    Parse a ``<MAP:lat,lon,callsign[,nodename]>`` tag from a packet's info field.

    Returns ``(lat, lon, callsign_upper, nodename_upper)`` for the first valid
    tag found, or ``None`` if no tag is present or the coordinates are out of
    range.  ``nodename`` is the empty string when the 3-argument form is used.
    """
    if not info:
        return None
    m = _MAP_TAG_RE.search(info)
    if not m:
        return None
    try:
        lat = float(m.group(1))
        lon = float(m.group(2))
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    callsign = m.group(3).upper()
    nodename = (m.group(4) or "").upper()
    return lat, lon, callsign, nodename


# ── ASCII network map helpers ─────────────────────────────────────────────────

def _render_ascii_map(
    bbs_call: str,
    data: dict,
    digis_only: bool,
    term: Terminal,
) -> list[str]:
    """
    Render the network topology as a plain ASCII tree.

    digis_only=True  (M command)::

        NETMAP W6ELA
        +--WOODY [2]
        |  \\--KPHXOR [1]
        \\--[direct: 4 stn]

    digis_only=False (MS command)::

        NETMAP W6ELA
        +--WOODY [2]
        |  +--W6OAK N6YP
        |  \\--KPHXOR [1]
        |     \\--KC7HEX
        \\--[direct]
           KF6ANX WB6YYY

    Node labels:
        WOODY [N]  — digi with N stations whose *immediate* parent is WOODY
        WOODY      — digi with no direct-child stations (only sub-digis)
    """
    children     = data["children"]
    digis        = data["digis"]
    stn_count    = data["stn_count"]
    stn_calls    = data["stn_calls"]
    direct_count = data["direct_count"]
    direct_calls = data["direct_calls"]
    width = term.width

    def _pack_calls(calls: list[str], pfx: str, conn: str) -> list[str]:
        """Pack callsigns onto wrapped lines of at most `width` characters."""
        out: list[str] = []
        avail = max(width - len(pfx) - len(conn), 6)
        buf: list[str] = []
        buf_len = 0
        cont_conn = " " * len(conn)
        for call in calls:
            need = (1 + len(call)) if buf else len(call)
            if buf and buf_len + need > avail:
                out.append(pfx + conn + term.label(" ".join(buf),"orange"))
                buf = [call]
                buf_len = len(call)
                conn = cont_conn
            else:
                buf.append(call)
                buf_len += need
        if buf:
            out.append(pfx + conn + term.label(" ".join(buf),"orange"))
        return out

    def _render_node(node: str, pfx: str, is_last: bool) -> list[str]:
        conn      = "\\--" if is_last else "+--"
        child_pfx = pfx + ("   " if is_last else "|  ")
        result: list[str] = []

        if node in digis:
            cnt   = stn_count.get(node, 0)
            label = f"{node} [{cnt}]" if cnt else node
            result.append(pfx + conn + label)

            digi_ch = [c for c in children.get(node, []) if c in digis]
            stn_ch  = stn_calls.get(node, [])

            if digis_only:
                sub_items: list = digi_ch
            else:
                sub_items = digi_ch + (["__stns__"] if stn_ch else [])

            for j, sub in enumerate(sub_items):
                sub_last = j == len(sub_items) - 1
                if sub == "__stns__":
                    sub_conn = "\\--" if sub_last else "+--"
                    result.extend(_pack_calls(stn_ch, child_pfx, sub_conn))
                else:
                    result.extend(_render_node(sub, child_pfx, sub_last))
        else:
            # Pure station leaf (only appears in full mode)
            result.append(pfx + conn + term.label(node, "warning"))
        return result

    bbs_digi_ch = [c for c in children.get(bbs_call, []) if c in digis]
    has_direct  = direct_count > 0
    all_items: list = bbs_digi_ch + (["__direct__"] if has_direct else [])

    if not all_items:
        return [f"NETMAP {bbs_call}", "(no confirmed RF paths yet)"]

    lines: list[str] = [f"NETMAP {bbs_call}"]
    for i, item in enumerate(all_items):
        is_last   = i == len(all_items) - 1
        conn      = "\\--" if is_last else "+--"
        child_pfx = "   " if is_last else "|  "

        if item == "__direct__":
            if digis_only:
                lines.append(conn + f"[direct: {direct_count} stn]")
            else:
                lines.append(conn + "[direct]")
                lines.extend(_pack_calls(direct_calls, child_pfx, ""))
        else:
            lines.extend(_render_node(item, "", is_last))

    return lines


class HeardPlugin(BBSPlugin):
    name = "heard"
    display_name = "Heard Stations"
    menu_key = "H"
    help_text = "Log of recently heard AX.25 stations with path and signal info."
    min_auth_level_name = "IDENTIFIED"

    def __init__(self) -> None:
        super().__init__()
        # In-memory cache; refreshed from DB on each session start.
        self._max_age_hours: int = _DEFAULT_MAX_AGE_HOURS
        # Ka-Node alias → owner callsign (e.g. 'KROCK' → 'K6FB-5').
        # Loaded at startup, refreshed when sysop saves a kanode_alias.
        self._kanode_map: dict[str, str] = {}
        # callsign → unix ts we last heard it *directly* (no digipeater H-bit).
        # Seeded from the DB at startup and updated on every direct on_heard.
        # This is the RF direct-heard source of truth; it is *pushed* into the
        # NET/ROM router (which owns adjacency) via _direct_heard_observer, so
        # the router can treat a node we hear on the air as a 1-hop neighbor.
        self._direct_heard: dict[str, int] = {}
        # Optional push sink: cb(call, ts) called for each seeded station when
        # wired, and on every subsequent direct hearing.  The router registers
        # its note_heard_direct here.  None when nothing consumes it.
        self._direct_heard_observer: Optional[Callable[[str, float], None]] = None

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)

        # Versioned schema migrations run BEFORE _SCHEMA is applied, so the v2
        # split (heard_stations → stations) sees the old table before an empty
        # `stations` would be created.
        async with aiosqlite.connect(db_path, timeout=30) as db:
            await self._migrate_heard_schema(db)
            await db.commit()

        # ── Apply schema (idempotent; creates missing tables on fresh install) ─
        async with aiosqlite.connect(db_path, timeout=30) as db:
            await db.executescript(_SCHEMA)
            default = int(cfg.get("max_age_hours", _DEFAULT_MAX_AGE_HOURS))
            await db.execute(
                "INSERT OR IGNORE INTO heard_settings (key, value) VALUES ('max_age_hours', ?)",
                (str(default),),
            )
            # Mark the one-shot netrom_alias→beacon_alias migration as done on
            # fresh installs so it never runs (idempotent; existing DBs may
            # already have a real or empty value, which is preserved).
            await db.execute(
                "INSERT OR IGNORE INTO heard_settings (key, value)"
                " VALUES ('migration_alias_fix_done', '1')"
            )
            await db.commit()

        self._max_age_hours = await self._load_max_age()
        await self._refresh_kanode_map()
        await self._load_direct_heard()

    async def _load_direct_heard(self) -> None:
        """Seed the in-memory direct-heard cache from the DB so "heard directly"
        survives a restart — otherwise the NET/ROM router would fall back to slow
        transit routing on every cold start until each neighbor re-beacons."""
        cache: dict[str, int] = {}
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                async with db.execute(
                    "SELECT callsign, MAX(last_direct_heard) FROM heard_events "
                    "WHERE last_direct_heard > 0 GROUP BY callsign"
                ) as cur:
                    async for call, ts in cur:
                        if call and ts:
                            cache[str(call).upper()] = int(ts)
        except Exception:
            logger.debug("heard: direct-heard cache seed skipped", exc_info=True)
        self._direct_heard = cache
        logger.info("heard: direct-heard cache seeded with %d station(s)", len(cache))

    def heard_direct_within(self, call: str, seconds: int) -> bool:
        """True iff *call* was heard directly (no digipeater) within the last
        *seconds*.  Synchronous (backed by the in-memory cache)."""
        ts = self._direct_heard.get(call.upper(), 0)
        return ts > 0 and (int(time.time()) - ts) <= max(0, seconds)

    def set_direct_heard_observer(
        self, cb: "Callable[[str, float], None]"
    ) -> None:
        """Register a push sink for RF direct hearings (the NET/ROM router's
        ``note_heard_direct``).  Immediately replays the currently-seeded cache
        so adjacency is populated right after startup, then fires on every
        subsequent direct ``on_heard``.  Optional — nothing breaks if unset."""
        self._direct_heard_observer = cb
        for call, ts in list(self._direct_heard.items()):
            try:
                cb(call, float(ts))
            except Exception:
                logger.debug("direct-heard observer replay failed for %s", call,
                             exc_info=True)

    # ── Schema migrations ─────────────────────────────────────────────────────

    async def _migrate_heard_schema(self, db) -> None:
        """Apply ordered, version-gated heard-schema migrations.

        The applied version is stored in heard_settings['heard_schema_version'].
        Every migration is idempotent and internally guarded, so it is safe
        against any starting DB shape; the version stamp adopts an already-migrated
        DB (e.g. a live production DB) at the current version and skips the work
        on every subsequent start.
        """
        # heard_settings carries the version marker; ensure it exists first.
        await db.execute(
            "CREATE TABLE IF NOT EXISTS heard_settings ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        async with db.execute(
            "SELECT value FROM heard_settings WHERE key = 'heard_schema_version'"
        ) as _cur:
            row = await _cur.fetchone()
        version = int(row[0]) if row and str(row[0]).isdigit() else 0
        if version >= _HEARD_SCHEMA_VERSION:
            return

        migrations = [
            (1, self._heard_migration_v1),
            (2, self._heard_migration_v2),
            (3, self._heard_migration_v3),
            (4, self._heard_migration_v4),
        ]
        for target, migrate in migrations:
            if target > version:
                await migrate(db)
                await db.execute(
                    "INSERT OR REPLACE INTO heard_settings (key, value)"
                    " VALUES ('heard_schema_version', ?)",
                    (str(target),),
                )
                logger.info("heard plugin: schema migrated to v%d", target)

    async def _heard_migration_v1(self, db) -> None:
        """v1 — consolidate the pre-versioning legacy migrations (unchanged; each
        idempotent + guarded) and drop the orphaned heard_stations table."""
        # ── Detect existing tables ────────────────────────────────────────
        # Close the cursor (async with) before any later DROP TABLE: an open
        # cursor on sqlite_master blocks schema modifications ("database table
        # is locked").
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as tables_cur:
            table_names = {r[0].lower() for r in await tables_cur.fetchall()}

        # ── Migrate heard_paths if it predates the via_base column ────────
        if "heard_paths" in table_names:
            try:
                async with db.execute("SELECT via_base FROM heard_paths LIMIT 1") as _c:
                    await _c.fetchall()
            except Exception:
                # Very old schema: drop and recreate (ephemeral path data).
                await db.execute("DROP TABLE heard_paths")
                table_names.discard("heard_paths")

        # ── Migrate heard_stations → stations + heard_events (schema v2) ──
        if "heard_stations" in table_names and "stations" not in table_names:
            logger.info(
                "heard plugin: migrating heard_stations → stations + heard_events …"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS stations (
                    callsign        TEXT    PRIMARY KEY NOT NULL COLLATE NOCASE,
                    lat             REAL,
                    lon             REAL,
                    comment         TEXT    NOT NULL DEFAULT '',
                    position_source TEXT    NOT NULL DEFAULT '',
                    netrom_alias    TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
                    beacon_alias    TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
                    first_seen      INTEGER NOT NULL DEFAULT 0,
                    last_seen       INTEGER NOT NULL DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS heard_events (
                    callsign          TEXT    NOT NULL COLLATE NOCASE,
                    transport         TEXT    NOT NULL DEFAULT '',
                    source            TEXT    NOT NULL DEFAULT 'heard',
                    first_heard       INTEGER NOT NULL,
                    last_heard        INTEGER NOT NULL,
                    count             INTEGER NOT NULL DEFAULT 0,
                    last_direct_heard INTEGER NOT NULL DEFAULT 0,
                    dest              TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
                    via               TEXT    NOT NULL DEFAULT '',
                    PRIMARY KEY (callsign, transport)
                )
            """)
            # Station identity: one row per callsign, RF-heard metadata wins
            # over NETROM-only rows for position / comment.  The old nodename
            # column was a catch-all (beacons, manual edits, NETROM NODES) so
            # it goes into beacon_alias; netrom_alias will repopulate from the
            # next NODES broadcast.
            await db.execute("""
                INSERT INTO stations
                    (callsign, lat, lon, comment, position_source,
                     beacon_alias, first_seen, last_seen)
                SELECT
                    callsign,
                    MAX(CASE WHEN transport != 'netrom' AND lat IS NOT NULL
                             THEN lat END),
                    MAX(CASE WHEN transport != 'netrom' AND lon IS NOT NULL
                             THEN lon END),
                    COALESCE(MAX(CASE WHEN transport != 'netrom'
                                     AND comment != ''
                                     THEN comment END), ''),
                    COALESCE(MAX(CASE WHEN transport != 'netrom'
                                     AND position_source != ''
                                     THEN position_source END), ''),
                    COALESCE(MAX(CASE WHEN nodename != '' THEN nodename END), ''),
                    MIN(first_heard),
                    MAX(last_heard)
                FROM heard_stations
                GROUP BY callsign
            """)
            # Per-transport observations: all rows, all transports, verbatim.
            await db.execute("""
                INSERT OR IGNORE INTO heard_events
                    (callsign, transport, source, first_heard, last_heard,
                     count, last_direct_heard, dest, via)
                SELECT callsign, transport, source, first_heard, last_heard,
                       count, last_direct_heard, dest, via
                FROM heard_stations
            """)
            await db.execute("DROP TABLE heard_stations")
            table_names.discard("heard_stations")
            table_names.update({"stations", "heard_events"})
            logger.info("heard plugin: heard_stations migration complete")

        # ── One-shot fix for an early migration bug ───────────────────────
        # The first version of the schema-v2 migration wrote
        # heard_stations.nodename → netrom_alias.  It should have gone to
        # beacon_alias (the original source was ambiguous).  Detect by:
        # netrom_alias set but no heard_events row with transport='netrom'.
        # Gated by a heard_settings sentinel so this only runs on databases
        # that pre-date the fix; otherwise a legit netrom_alias whose
        # heard_events 'netrom' row gets pruned in the future could be
        # erroneously rewritten on the next startup.
        if (
            "stations" in table_names
            and "heard_events" in table_names
            and "heard_settings" in table_names
        ):
            async with db.execute(
                "SELECT value FROM heard_settings"
                " WHERE key = 'migration_alias_fix_done'"
            ) as _c:
                done_row = await _c.fetchone()
            if not done_row:
                fixed = await db.execute(
                    """
                    UPDATE stations
                       SET beacon_alias = CASE WHEN beacon_alias = ''
                                               THEN netrom_alias
                                               ELSE beacon_alias END,
                           netrom_alias = ''
                     WHERE netrom_alias != ''
                       AND NOT EXISTS (
                               SELECT 1 FROM heard_events e
                                WHERE e.callsign = stations.callsign
                                  AND e.transport = 'netrom'
                           )
                    """
                )
                if fixed.rowcount:
                    logger.info(
                        "heard plugin: corrected netrom_alias → beacon_alias"
                        " for %d station(s)", fixed.rowcount
                    )
                await db.execute(
                    "INSERT OR REPLACE INTO heard_settings (key, value)"
                    " VALUES ('migration_alias_fix_done', '1')"
                )

        # ── Add kanode_alias column if not present ────────────────────────
        if "stations" in table_names:
            try:
                async with db.execute("SELECT kanode_alias FROM stations LIMIT 1") as _c:
                    await _c.fetchall()
            except Exception:
                await db.execute(
                    "ALTER TABLE stations ADD COLUMN"
                    " kanode_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE"
                )
                logger.info("heard plugin: added kanode_alias column to stations")

        # ── Migrate netrom_routes to composite PK (dest_call, neighbor_call)
        if "netrom_routes" in table_names:
            async with db.execute("PRAGMA table_info(netrom_routes)") as _c:
                pk_rows = await _c.fetchall()
            pk_count = sum(1 for r in pk_rows if r[5] > 0)
            if pk_count == 1:
                logger.info(
                    "heard plugin: migrating netrom_routes to composite PK …"
                )
                await db.execute("""
                    CREATE TABLE netrom_routes_new (
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
                await db.execute("""
                    INSERT OR IGNORE INTO netrom_routes_new
                        (dest_call, neighbor_call, alias, quality,
                         via_call, via_alias, last_seen)
                    SELECT dest_call, neighbor_call, alias, quality,
                           via_call, via_alias, last_seen
                    FROM netrom_routes
                """)
                await db.execute("DROP TABLE netrom_routes")
                await db.execute(
                    "ALTER TABLE netrom_routes_new RENAME TO netrom_routes"
                )
                logger.info("heard plugin: netrom_routes migration complete")

        # ── Drop the orphaned heard_stations table ────────────────────────
        # The schema-v2 split above only fires when `stations` does NOT yet
        # exist, so a DB where the split already ran keeps its old heard_stations
        # rows forever (they are superseded by `stations`).  Drop the stale
        # table when `stations` is present so no live data is lost.
        if "heard_stations" in table_names and "stations" in table_names:
            await db.execute("DROP TABLE heard_stations")
            table_names.discard("heard_stations")
            logger.info("heard plugin: dropped orphaned heard_stations table")

    async def _heard_migration_v2(self, db) -> None:
        """v2 — add stations.service + last_beacon_text + last_beacon_ts.

        Only ALTERs an *existing* stations table; on a fresh install `stations`
        is created (with these columns) by _SCHEMA after the migrator runs.
        """
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stations'"
        ) as cur:
            if await cur.fetchone() is None:
                return
        for col, ddl in (
            ("service",
             "ALTER TABLE stations ADD COLUMN service TEXT NOT NULL DEFAULT ''"),
            ("last_beacon_text",
             "ALTER TABLE stations ADD COLUMN last_beacon_text TEXT NOT NULL DEFAULT ''"),
            ("last_beacon_ts",
             "ALTER TABLE stations ADD COLUMN last_beacon_ts INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                async with db.execute(f"SELECT {col} FROM stations LIMIT 1") as _c:
                    await _c.fetchall()
            except Exception:
                await db.execute(ddl)
                logger.info("heard plugin: added stations.%s (v2)", col)

    async def _heard_migration_v3(self, db) -> None:
        """v3 — add station_entities + stations.base_call, grouping the
        callsign-SSIDs of one physical station under its base callsign."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS station_entities (
                base_call          TEXT    PRIMARY KEY NOT NULL COLLATE NOCASE,
                canonical_nodename TEXT    NOT NULL DEFAULT '',
                notes              TEXT    NOT NULL DEFAULT '',
                lat                REAL,
                lon                REAL,
                position_source    TEXT    NOT NULL DEFAULT '',
                first_seen         INTEGER NOT NULL DEFAULT 0,
                last_seen          INTEGER NOT NULL DEFAULT 0
            )
        """)
        # The rest applies only to an existing stations table; a fresh install
        # gets base_call from _SCHEMA and has no rows to backfill.
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stations'"
        ) as cur:
            if await cur.fetchone() is None:
                return
        try:
            async with db.execute("SELECT base_call FROM stations LIMIT 1") as _c:
                await _c.fetchall()
        except Exception:
            await db.execute(
                "ALTER TABLE stations ADD COLUMN"
                " base_call TEXT NOT NULL DEFAULT '' COLLATE NOCASE"
            )
            logger.info("heard plugin: added stations.base_call (v3)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stations_base_call ON stations (base_call)"
        )
        # Backfill base_call = callsign with any -SSID suffix stripped.
        await db.execute("""
            UPDATE stations
               SET base_call = CASE WHEN instr(callsign,'-') > 0
                                    THEN substr(callsign, 1, instr(callsign,'-') - 1)
                                    ELSE callsign END
             WHERE base_call = ''
        """)
        # One entity per base callsign, seeded from member first/last_seen.
        await db.execute("""
            INSERT OR IGNORE INTO station_entities (base_call, first_seen, last_seen)
            SELECT base_call, MIN(first_seen), MAX(last_seen)
              FROM stations WHERE base_call != '' GROUP BY base_call
        """)
        logger.info("heard plugin: created station_entities + base_call (v3)")

    async def _heard_migration_v4(self, db) -> None:
        """v4 — add stations.position_ts (unix time a position was last written).

        Used to pick the freshest beacon position when rolling SSIDs up to a
        physical station.  Existing positions are backfilled to last_seen: a
        best-effort "when we last heard this station" stamp, so pre-v4 fixes
        still order sensibly against new beacons.
        """
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stations'"
        ) as cur:
            if await cur.fetchone() is None:
                return
        try:
            async with db.execute("SELECT position_ts FROM stations LIMIT 1") as _c:
                await _c.fetchall()
        except Exception:
            await db.execute(
                "ALTER TABLE stations ADD COLUMN position_ts INTEGER NOT NULL DEFAULT 0"
            )
            logger.info("heard plugin: added stations.position_ts (v4)")
        # Seed a timestamp for rows that already carry a fix so freshest-wins
        # ordering has something to compare against.
        await db.execute(
            "UPDATE stations SET position_ts = last_seen"
            " WHERE lat IS NOT NULL AND position_ts = 0"
        )

    async def _reconcile_entities(self, db) -> None:
        """Assign base_call to any newly-inserted stations and ensure a
        station_entities row exists for each base callsign.  Called at the end
        of an ingest write; cheap because it only touches rows whose base_call
        is still blank (the ones just inserted) — base_call is indexed.
        """
        await db.execute("""
            UPDATE stations
               SET base_call = CASE WHEN instr(callsign,'-') > 0
                                    THEN substr(callsign, 1, instr(callsign,'-') - 1)
                                    ELSE callsign END
             WHERE base_call = ''
        """)
        await db.execute("""
            INSERT OR IGNORE INTO station_entities (base_call, first_seen, last_seen)
            SELECT base_call, MIN(first_seen), MAX(last_seen)
              FROM stations
             WHERE base_call != ''
               AND base_call NOT IN (SELECT base_call FROM station_entities)
             GROUP BY base_call
        """)

    # ── Settings helpers ──────────────────────────────────────────────────────

    async def _load_max_age(self) -> int:
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            async with db.execute(
                "SELECT value FROM heard_settings WHERE key = 'max_age_hours'"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else _DEFAULT_MAX_AGE_HOURS

    async def _save_max_age(self, hours: int) -> None:
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            await db.execute(
                "INSERT OR REPLACE INTO heard_settings (key, value) VALUES ('max_age_hours', ?)",
                (str(hours),),
            )
            await db.commit()
        self._max_age_hours = hours

    async def _refresh_kanode_map(self) -> None:
        """Reload kanode_alias → owner callsign map from the DB.

        Called at startup (after schema is applied) and after each sysop edit
        via PUT /api/heard/<callsign>.  Building a fresh dict from scratch
        means clearing a Ka-Node alias also evicts the stale mapping.
        """
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                async with db.execute(
                    "SELECT callsign, kanode_alias FROM stations WHERE kanode_alias != ''"
                ) as cur:
                    rows = await cur.fetchall()
            self._kanode_map = {alias.upper(): call.upper() for call, alias in rows}
        except Exception:
            logger.warning(
                "heard plugin: _refresh_kanode_map failed; "
                "Ka-Node alias resolution may be stale", exc_info=True,
            )

    async def _prune(self) -> int:
        """Delete path entries older than max_age_hours.
        Station identity records (stations table) are retained indefinitely.
        Returns the number of path entries removed."""
        if self._max_age_hours <= 0:
            return 0
        cutoff = int(time.time()) - self._max_age_hours * 3600
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            cur = await db.execute(
                "DELETE FROM heard_paths WHERE last_seen < ?", (cutoff,)
            )
            await db.commit()
            return cur.rowcount

    # ── Transport observer ────────────────────────────────────────────────────

    async def on_heard(
        self, src: str, dest: str, via: list[str], ts: int, transport: str,
        info: str = "",
    ) -> None:
        """
        Called by RF transports when a frame is received that is NOT addressed
        to the BBS.  Records/updates the stations, heard_events and heard_paths
        tables.
        """
        src_up   = src.upper()
        dest_up  = dest.upper()
        via_str  = ",".join(via)
        # A frame is heard *direct* when no digipeater has set the H-bit yet
        # (including the case of a completely empty via list).  A frame with
        # e.g. "Via WOODY" (no *) was received before WOODY relayed it, so the
        # BBS heard it straight from the source station.
        is_direct = not any(v.endswith("*") for v in via)
        via_base  = ",".join(v.rstrip("*") for v in via)  # normalised for digi rows
        if is_direct:
            # Keep the direct-heard cache current and push to the NET/ROM router.
            self._direct_heard[src_up] = ts
            if self._direct_heard_observer is not None:
                try:
                    self._direct_heard_observer(src_up, float(ts))
                except Exception:
                    logger.debug("direct-heard observer push failed for %s",
                                 src_up, exc_info=True)

        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            # ── stations: ensure identity row exists ──────────────────────────
            await db.execute(
                "INSERT OR IGNORE INTO stations (callsign, first_seen, last_seen)"
                " VALUES (?, ?, ?)",
                (src_up, ts, ts),
            )
            await db.execute(
                "UPDATE stations SET last_seen = MAX(last_seen, ?) WHERE callsign = ?",
                (ts, src_up),
            )
            # ── latest beacon/ID/status text this station transmitted ─────────
            # on_heard only sees monitored UI/unproto frames (beacons, IDs,
            # APRS, status), so the info field is the station's own broadcast
            # text.  Keep the most recent non-empty one (capped) for display.
            _beacon_text = info.strip()
            if _beacon_text:
                await db.execute(
                    "UPDATE stations SET last_beacon_text = ?, last_beacon_ts = ?"
                    " WHERE callsign = ?",
                    (_beacon_text[:_MAX_BEACON_TEXT], ts, src_up),
                )

            # ── heard_events: upsert per-transport observation ────────────────
            await db.execute(
                """
                INSERT INTO heard_events
                    (callsign, transport, source, first_heard, last_heard,
                     count, last_direct_heard, dest, via)
                VALUES (?, ?, 'heard', ?, ?, 1, ?, ?, ?)
                ON CONFLICT(callsign, transport) DO UPDATE SET
                    last_heard        = excluded.last_heard,
                    count             = count + 1,
                    dest              = excluded.dest,
                    via               = excluded.via,
                    source            = 'heard',
                    last_direct_heard = CASE
                        WHEN ? THEN excluded.last_direct_heard
                        ELSE last_direct_heard
                    END
                """,
                (src_up, transport, ts, ts,
                 ts if is_direct else 0, dest_up, via_str,
                 1 if is_direct else 0),
            )

            # ── Auto-seed relay digipeaters from the via path ─────────────────
            # Three tiers based on position relative to the last H-bit (*):
            #   i == last_star_idx: BBS received RF directly from this digi.
            #                       Update last_heard, mark source='heard'.
            #   i <  last_star_idx: If this hop has '*', treat it as direct.
            #                       Otherwise keep 'heard' only within a short
            #                       grace window after the last direct hear.
            #   i >  last_star_idx: Speculative hop (H-bit not yet set).  Only
            #                       seed the row; do not update last_heard.
            _last_star = max(
                (j for j, v in enumerate(via) if v.endswith("*")),
                default=-1,
            )
            for _i, _part in enumerate(via):
                _digi = _part.rstrip("*").strip().upper()
                if not _digi or _digi == src_up:
                    continue
                # Resolve Ka-Node alias → owner callsign so digi activity is
                # attributed to the owning station (e.g. KROCK → K6FB-5).
                _effective_digi = self._kanode_map.get(_digi, _digi)

                # Always ensure station identity row exists.
                await db.execute(
                    "INSERT OR IGNORE INTO stations (callsign, first_seen, last_seen)"
                    " VALUES (?, ?, ?)",
                    (_effective_digi, ts, ts),
                )

                if _i > _last_star:
                    # Speculative — only seed the heard_events row, no time update.
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO heard_events
                            (callsign, transport, source,
                             first_heard, last_heard, count, last_direct_heard,
                             dest, via)
                        VALUES (?, '', 'via', ?, ?, 0, 0, '', '')
                        """,
                        (_effective_digi, ts, ts),
                    )
                elif _i == _last_star:
                    # Directly heard: BBS received the RF signal from this digi.
                    await db.execute(
                        "UPDATE stations SET last_seen = MAX(last_seen, ?) WHERE callsign = ?",
                        (ts, _effective_digi),
                    )
                    await db.execute(
                        """
                        INSERT INTO heard_events
                            (callsign, transport, source,
                             first_heard, last_heard, count, last_direct_heard,
                             dest, via)
                        VALUES (?, '', 'heard', ?, ?, 0, ?, '', '')
                        ON CONFLICT(callsign, transport) DO UPDATE SET
                            last_heard        = MAX(last_heard, excluded.last_heard),
                            last_direct_heard = MAX(last_direct_heard,
                                                    excluded.last_direct_heard),
                            source            = 'heard'
                        """,
                        (_effective_digi, ts, ts, ts),
                    )
                else:
                    if _part.endswith("*"):
                        # Some transports mark intermediate digis with '*'.
                        await db.execute(
                            """
                            INSERT INTO heard_events
                                (callsign, transport, source,
                                 first_heard, last_heard, count, last_direct_heard,
                                 dest, via)
                            VALUES (?, '', 'heard', ?, ?, 0, ?, '', '')
                            ON CONFLICT(callsign, transport) DO UPDATE SET
                                last_heard        = MAX(last_heard,
                                                        excluded.last_heard),
                                last_direct_heard = MAX(last_direct_heard,
                                                        excluded.last_direct_heard),
                                source            = 'heard'
                            """,
                            (_effective_digi, ts, ts, ts),
                        )
                    else:
                        # No star on this hop: keep 'heard' only briefly after
                        # a recent direct hear; otherwise downgrade to 'via'.
                        cutoff = ts - _DIRECT_GRACE_SECONDS
                        await db.execute(
                            """
                            INSERT INTO heard_events
                                (callsign, transport, source,
                                 first_heard, last_heard, count, last_direct_heard,
                                 dest, via)
                            VALUES (?, '', 'via', ?, ?, 0, 0, '', '')
                            ON CONFLICT(callsign, transport) DO UPDATE SET
                                last_heard = MAX(last_heard, excluded.last_heard),
                                source     = CASE
                                    WHEN COALESCE(heard_events.last_direct_heard, 0) >= ?
                                    THEN 'heard'
                                    ELSE 'via'
                                END
                            """,
                            (_effective_digi, ts, ts, cutoff),
                        )

            # ── heard_paths: direct receptions → via_base=""; relayed → base ─
            if is_direct:
                # Record as a direct-path row (via_base="") so the display can
                # show "Direct" or "Direct, <digi>" when the same station is
                # also heard via a digipeater.
                await db.execute(
                    """
                    INSERT INTO heard_paths
                        (callsign, transport, via_base, via, first_seen, last_seen, count)
                    VALUES (?, ?, '', '', ?, ?, 1)
                    ON CONFLICT(callsign, transport, via_base) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        count     = count + 1
                    """,
                    (src_up, transport, ts, ts),
                )
            elif via_base:
                # Relayed: at least one digi has the H-bit set.
                path_row = await (
                    await db.execute(
                        "SELECT via FROM heard_paths"
                        " WHERE callsign=? AND transport=? AND via_base=?",
                        (src_up, transport, via_base),
                    )
                ).fetchone()
                merged_path_via = _merge_via(path_row[0] if path_row else "", via_str)
                await db.execute(
                    """
                    INSERT INTO heard_paths
                        (callsign, transport, via_base, via, first_seen, last_seen, count)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(callsign, transport, via_base) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        count     = count + 1,
                        via       = ?
                    """,
                    (src_up, transport, via_base, merged_path_via, ts, ts,
                     merged_path_via),
                )

            # ── <MAP:lat,lon,call[,nodename]> location-beacon tag ────────────
            # Honour the tag only when its callsign matches the frame source —
            # the protocol says "Only use your own callsign and your own node
            # name", and matching prevents trivial spoofing.  We compare on the
            # base callsign (stripping any -SSID suffix) so that e.g. a frame
            # from KC7HEX-10 may legitimately carry "<MAP:...,KC7HEX,NODE>".
            # When honoured, beacon coordinates override any prior value
            # (manual or beacon): a station's own broadcast is the freshest
            # authoritative source.
            parsed_map = _parse_map_tag(info)
            if parsed_map is not None:
                lat, lon, map_call, beacon_alias = parsed_map
                if map_call.split("-", 1)[0] == src_up.split("-", 1)[0]:
                    await db.execute(
                        """
                        UPDATE stations
                           SET lat             = ?,
                               lon             = ?,
                               beacon_alias    = ?,
                               position_source = 'beacon',
                               position_ts     = ?
                         WHERE callsign = ?
                        """,
                        (lat, lon, beacon_alias, ts, src_up),
                    )
                    logger.info(
                        "MAP beacon: %s%s @ %.4f,%.4f via %s",
                        src_up,
                        f" ({beacon_alias})" if beacon_alias else "",
                        lat, lon, transport,
                    )
                else:
                    logger.warning(
                        "MAP beacon ignored: src %s does not match tag callsign %s"
                        " — info: %r",
                        src_up, map_call, info,
                    )
            await self._reconcile_entities(db)
            await db.commit()

        # Notify subscribers after the DB write so the data is consistent.
        if self._bus is not None:
            await self._bus.publish("heard.station", {
                "callsign":  src_up,
                "dest":      dest_up,
                "transport": transport,
                "via":       via_str,
                "timestamp": ts,
                "info":      info,
            })

    # ── NETROM NODES observer ─────────────────────────────────────────────────

    async def on_netrom_nodes(self, frame: NodesFrame) -> None:
        """
        Called by NetromRouter after decoding a received NODES broadcast.

        Updates netrom_routes and upserts stations/heard_events rows so NETROM
        nodes appear in the map and graph views.  Also backfills netrom_alias on
        any existing station whose alias we now know for the first time.
        """
        ts        = int(time.time())
        src_up    = frame.source_call.upper()
        src_alias = frame.source_alias.upper()

        # Build a callsign → alias map for everything we learn from this broadcast.
        alias_map: dict[str, str] = {}
        if src_alias:
            alias_map[src_up] = src_alias
        for e in frame.entries:
            if e.alias:
                alias_map[e.dest_call.upper()] = e.alias.upper()

        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            # Upsert the advertising node into stations + heard_events.
            if src_alias:
                await db.execute(
                    "INSERT OR IGNORE INTO stations"
                    " (callsign, first_seen, last_seen, netrom_alias)"
                    " VALUES (?, ?, ?, ?)",
                    (src_up, ts, ts, src_alias),
                )
                await db.execute(
                    """
                    UPDATE stations SET
                        last_seen    = MAX(last_seen, ?),
                        netrom_alias = CASE WHEN netrom_alias = ''
                                           THEN ? ELSE netrom_alias END
                    WHERE callsign = ?
                    """,
                    (ts, src_alias, src_up),
                )
                await db.execute(
                    """
                    INSERT INTO heard_events
                        (callsign, transport, source,
                         first_heard, last_heard, count, last_direct_heard,
                         dest, via)
                    VALUES (?, 'netrom', 'netrom', ?, ?, 1, 0, '', '')
                    ON CONFLICT(callsign, transport) DO UPDATE SET
                        last_heard = excluded.last_heard,
                        count      = count + 1
                    """,
                    (src_up, ts, ts),
                )

            # Upsert each route entry into netrom_routes and stations/heard_events.
            for e in frame.entries:
                dest_up  = e.dest_call.upper()
                alias_up = e.alias.upper() if e.alias else ''
                nbr_up   = e.neighbor_call.upper()

                await db.execute(
                    """
                    INSERT INTO netrom_routes
                        (dest_call, neighbor_call, alias, quality,
                         via_call, via_alias, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dest_call, neighbor_call) DO UPDATE SET
                        alias     = excluded.alias,
                        quality   = excluded.quality,
                        via_call  = excluded.via_call,
                        via_alias = excluded.via_alias,
                        last_seen = excluded.last_seen
                    """,
                    (dest_up, nbr_up, alias_up, e.quality, src_up, src_alias, ts),
                )

                if alias_up:
                    await db.execute(
                        "INSERT OR IGNORE INTO stations"
                        " (callsign, first_seen, last_seen, netrom_alias)"
                        " VALUES (?, ?, ?, ?)",
                        (dest_up, ts, ts, alias_up),
                    )
                    await db.execute(
                        """
                        UPDATE stations SET
                            last_seen    = MAX(last_seen, ?),
                            netrom_alias = CASE WHEN netrom_alias = ''
                                               THEN ? ELSE netrom_alias END
                        WHERE callsign = ?
                        """,
                        (ts, alias_up, dest_up),
                    )
                    await db.execute(
                        """
                        INSERT INTO heard_events
                            (callsign, transport, source,
                             first_heard, last_heard, count, last_direct_heard,
                             dest, via)
                        VALUES (?, 'netrom', 'netrom', ?, ?, 1, 0, '', '')
                        ON CONFLICT(callsign, transport) DO UPDATE SET
                            last_heard = excluded.last_heard,
                            count      = count + 1
                        """,
                        (dest_up, ts, ts),
                    )

            # Backfill netrom_alias on every existing station row for callsigns
            # we just learned an alias for — only where it is still blank.
            for call, alias in alias_map.items():
                await db.execute(
                    "UPDATE stations SET netrom_alias = ?"
                    " WHERE callsign = ? AND netrom_alias = ''",
                    (alias, call),
                )

            await self._reconcile_entities(db)
            await db.commit()

        logger.info(
            "netrom NODES from %s (%s): %d routes, aliases backfilled for %d callsign(s)",
            src_up, src_alias, len(frame.entries), len(alias_map),
        )

    # ── ASCII network map ─────────────────────────────────────────────────────

    async def _build_map_data(self, bbs_call: str) -> dict:
        """
        Build the topology tree for the ASCII network map.

        Returns a dict consumed by _render_ascii_map():
            children    — {parent: [child, ...]} tree; bbs_call is the root
            digis       — set of relay-node names
            stn_count   — {digi: N} direct-child station count per digi
            stn_calls   — {digi: [callsign, ...]} for MAP ALL mode
            direct_count — stations heard with no digipeater
            direct_calls — sorted callsign list for MAP ALL mode

        Edge ambiguity (a node reachable via two different parent digis) is
        resolved by choosing the parent with the highest edge count.
        """
        from collections import defaultdict

        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            async with db.execute(
                "SELECT callsign, via FROM heard_paths WHERE via_base != ''"
            ) as cur:
                relayed_rows = await cur.fetchall()
            async with db.execute(
                "SELECT callsign FROM heard_paths WHERE via_base = '' ORDER BY callsign"
            ) as cur:
                direct_calls: list[str] = [r[0].upper() for r in await cur.fetchall()]

        # Count occurrences of each confirmed hop
        edge_count: dict[tuple[str, str], int] = defaultdict(int)
        source_nodes: set[str] = set()
        for callsign, via in relayed_rows:
            src = callsign.upper()
            source_nodes.add(src)
            for edge in confirmed_edges(src, via, bbs_call):
                edge_count[edge] += 1

        # Digis = non-BBS nodes that appear as a hop target in any confirmed path
        digis: set[str] = {b for (_, b) in edge_count if b != bbs_call}

        # For each non-BBS node, pick its best parent (highest edge count toward BBS)
        all_nodes = {n for pair in edge_count for n in pair} - {bbs_call}
        parent: dict[str, str] = {}
        for node in all_nodes:
            best_b, best_cnt = None, 0
            for (a, b), cnt in edge_count.items():
                if a == node and cnt > best_cnt:
                    best_b, best_cnt = b, cnt
            if best_b:
                parent[node] = best_b

        # Build children lists; sort digis before stations, then alpha
        children: dict[str, list[str]] = defaultdict(list)
        for node, par in parent.items():
            children[par].append(node)
        for par in children:
            children[par].sort(key=lambda n: (n not in digis, n))

        # Station counts / callsigns per digi (direct-child stations only)
        stn_count: dict[str, int] = defaultdict(int)
        stn_calls: dict[str, list[str]] = defaultdict(list)
        for node in source_nodes:
            if node not in digis and node in parent:
                par = parent[node]
                stn_count[par] += 1
                stn_calls[par].append(node)
        for lst in stn_calls.values():
            lst.sort()

        return {
            "children":     dict(children),
            "digis":        digis,
            "stn_count":    dict(stn_count),
            "stn_calls":    dict(stn_calls),
            "direct_count": len(direct_calls),
            "direct_calls": direct_calls,
        }

    # ── BBS session handler ───────────────────────────────────────────────────

    async def _station_count(self, cutoff: int = 0) -> int:
        """Return the number of active (non-expired) directly-heard stations."""
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                async with db.execute(
                    "SELECT COUNT(DISTINCT callsign) FROM heard_events"
                    " WHERE source = 'heard' AND transport != '' AND last_heard >= ?",
                    (cutoff,),
                ) as cur:
                    row = await cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    async def handle_session(self, session: "BBSSession") -> None:
        term = session.term
        self._max_age_hours = await self._load_max_age()
        await self._prune()
        cutoff = (
            int(time.time()) - self._max_age_hours * 3600
            if self._max_age_hours > 0
            else 0
        )

        limit    = int(self._cfg.get("limit", 200))
        is_sysop = session.auth.is_sysop

        while True:
            # ── Count for menu label ─────────────────────────────────────────
            count     = await self._station_count(cutoff)
            age_label = (
                f"{self._max_age_hours}h window"
                if self._max_age_hours > 0
                else "all time"
            )
            h_label  = f"List ({count} stations, {age_label})"
            hs_label = f"List short ({count} stations)"

            menu: list[tuple[str, str]] = [
                ("H",  h_label),
                ("HS", hs_label),
                ("M",  "Map (digis only)"),
                ("MS", "Map (with stations)"),
                ("Q",  "Quit"),
            ]
            if is_sysop:
                menu.insert(0, ("C", f"Configure (max age: {self._max_age_hours}h)"))

            action = await term.prompt_menu("HEARD STATIONS", menu, max_len=4, timeout=120)
            session.touch()
            session.log_command("heard", action)

            # ── Dispatch ────────────────────────────────────────────────────
            if action == "Q":
                break

            if action == "C" and is_sysop:
                await self._configure(session)
                continue

            if action in ("M", "MS"):
                bbs_call = session.cfg.callsign.upper()
                data     = await self._build_map_data(bbs_call)
                map_lines = _render_ascii_map(
                    bbs_call, data,
                    digis_only=(action == "M"),
                    term=term,
                )
                await term.paginate(map_lines, timeout=float(session.cfg.idle_timeout) or None)
                continue

            if action in ("H", "HS"):
                async with aiosqlite.connect(self._db_path, timeout=30) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute(
                        """
                        SELECT
                            s.callsign,
                            COALESCE((SELECT e.dest FROM heard_events e
                                       WHERE e.callsign = s.callsign
                                         AND e.source = 'heard'
                                         AND e.transport != ''
                                       ORDER BY e.last_heard DESC LIMIT 1), '') AS dest,
                            COALESCE((SELECT e.transport FROM heard_events e
                                       WHERE e.callsign = s.callsign
                                         AND e.source = 'heard'
                                         AND e.transport != ''
                                       ORDER BY e.last_heard DESC LIMIT 1), '') AS transport,
                            COALESCE((SELECT e.via FROM heard_events e
                                       WHERE e.callsign = s.callsign
                                         AND e.source = 'heard'
                                         AND e.transport != ''
                                       ORDER BY e.last_heard DESC LIMIT 1), '') AS via,
                            s.first_seen  AS first_heard,
                            s.last_seen   AS last_heard,
                            COALESCE((SELECT SUM(e.count) FROM heard_events e
                                       WHERE e.callsign = s.callsign
                                         AND e.source = 'heard'
                                         AND e.transport != ''), 0) AS count,
                            (SELECT COUNT(*) FROM heard_paths hp
                              WHERE hp.callsign = s.callsign
                                AND hp.via_base = '') AS direct_count,
                            (SELECT COUNT(*) FROM heard_paths hp
                              WHERE hp.callsign = s.callsign
                                AND hp.via_base != '') AS digi_count,
                            (SELECT hp.via FROM heard_paths hp
                              WHERE hp.callsign = s.callsign
                                AND hp.via_base != ''
                              ORDER BY hp.last_seen DESC LIMIT 1) AS best_digi_via
                        FROM stations s
                        WHERE EXISTS (
                            SELECT 1 FROM heard_events e
                             WHERE e.callsign = s.callsign
                               AND e.source = 'heard'
                               AND e.transport != ''
                               AND e.last_heard >= ?
                        )
                        ORDER BY s.last_seen DESC
                        LIMIT ?
                        """,
                        (cutoff, limit),
                    ) as cur:
                        rows = await cur.fetchall()

                if not rows:
                    await term.sendln(term.note(f"No stations heard yet ({age_label})."))
                elif action == "HS":
                    # Short listing: CALLSIGN  MM/DD HH:MM  (two columns wide)
                    col_call = 9
                    col_ts   = 11   # "MM/DD HH:MM"
                    per_row  = max(1, (term.width + 2) // (col_call + col_ts + 2))
                    header   = f"HEARD  ({len(rows)} stations, {age_label})"
                    await term.sendln(term.label(header, "meta"))
                    await term.sendln(term.note("-" * min(len(header), term.width)))
                    await term.flush()
                    lines = []
                    buf: list[str] = []
                    for row in rows:
                        call = str(row["callsign"]).upper().ljust(col_call)[:col_call]
                        ts   = time.strftime("%m/%d %H:%M", time.localtime(row["last_heard"]))
                        buf.append(f"{term.style(call, 'accent')} {term.note(ts)}")
                        if len(buf) >= per_row:
                            lines.append("  ".join(buf))
                            buf = []
                    if buf:
                        lines.append("  ".join(buf))
                    await term.paginate(lines, timeout=float(session.cfg.idle_timeout) or None)
                else:
                    # Full listing
                    header  = f"HEARD STATIONS  ({len(rows)} entries, {age_label})"
                    await term.sendln(term.label(header, "meta"))
                    await term.sendln(term.note("-" * min(len(header), term.width)))
                    col_call = 9
                    col_ts   = 14   # "YY-MM-DD HH:MM"
                    col_trn  = 12
                    col_hdr  = (
                        f"{'CALLSIGN':<{col_call}} "
                        f"{'LAST HEARD':<{col_ts}} "
                        f"{'TRANSPORT':<{col_trn}} VIA"
                    )
                    await term.sendln(term.label(col_hdr, "meta"))
                    await term.sendln(term.note("-" * min(len(col_hdr), term.width)))
                    await term.flush()
                    lines = []
                    for row in rows:
                        call         = str(row["callsign"]).upper().ljust(col_call)[:col_call]
                        last         = time.strftime("%y-%m-%d %H:%M", time.localtime(row["last_heard"])).ljust(col_ts)[:col_ts]
                        trn          = str(row["transport"]).ljust(col_trn)[:col_trn]
                        heard_direct = bool(row["direct_count"])
                        digi_count   = row["digi_count"] or 0
                        best_digi    = row["best_digi_via"]
                        if heard_direct and digi_count > 0:
                            extra    = (1 + digi_count) - 2
                            via_text = f"direct, {best_digi}"
                            if extra > 0:
                                via_text += term.note(f" (+{extra} more)")
                        elif heard_direct:
                            via_text = term.note("direct")
                        elif digi_count > 0:
                            extra    = digi_count - 1
                            via_text = best_digi or str(row["via"])
                            if extra > 0:
                                via_text += term.note(f" (+{extra} more)")
                        else:
                            via_text = str(row["via"]) if row["via"] else term.note("direct")
                        lines.append(
                            f"{term.style(call, 'accent')} {term.note(last)} "
                            f"{trn} {via_text}"
                        )
                    await term.paginate(lines, timeout=float(session.cfg.idle_timeout) or None)

        await term.sendln()

    async def _clear(self) -> int:
        """Delete all rows from stations, heard_events and heard_paths.
        Returns the number of station records removed."""
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            cur = await db.execute("DELETE FROM stations")
            removed = cur.rowcount
            await db.execute("DELETE FROM heard_events")
            await db.execute("DELETE FROM heard_paths")
            await db.commit()
        return removed

    async def _configure(self, session: "BBSSession") -> None:
        term = session.term
        while True:
            await term.sendln(term.label("CONFIGURE HEARD STATIONS", "meta"))
            await term.sendln(
                f"Current max age: {term.style(str(self._max_age_hours), 'accent')} hours  "
                f"(0 = keep forever)"
            )
            choice = await term.prompt_menu(
                "CONFIGURE",
                [("A", f"Set max age (current: {self._max_age_hours}h)"),
                 ("X", "Clear all heard entries"),
                 ("Q", "Back")],
                max_len=4, timeout=60,
            )
            session.touch()
            session.log_command("heard:configure", choice)
            if choice == "Q":
                break
            elif choice == "A":
                await term.send("New max age in hours (Enter to cancel): ")
                raw = (await term.readline(max_len=6, timeout=60)).strip()
                if not raw:
                    await term.sendln(term.note("Cancelled."))
                    continue
                try:
                    hours = int(raw)
                    if hours < 0:
                        raise ValueError
                except ValueError:
                    await term.sendln(term.warn("Invalid value — must be a non-negative integer."))
                    continue
                await self._save_max_age(hours)
                await self._prune()
                label = f"{hours}h" if hours > 0 else "forever"
                await term.sendln(term.ok(f"Max age set to {label}."))
            elif choice == "X":
                await term.send(term.warn("Clear ALL heard entries? [y/N]: "))
                confirm = (await term.readline(max_len=2, timeout=30)).strip().upper()
                if confirm == "Y":
                    removed = await self._clear()
                    await term.sendln(term.ok(f"Cleared {removed} entries."))
                else:
                    await term.sendln(term.note("Cancelled."))

    async def shutdown(self) -> None:
        pass

    def get_stats(self) -> dict[str, Any]:
        base = super().get_stats()
        base["display_name"] = self.display_name
        base["max_age_hours"] = self._max_age_hours
        return base
