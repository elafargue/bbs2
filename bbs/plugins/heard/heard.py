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
"""
from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

import aiosqlite

from bbs.core.auth import AuthLevel
from bbs.core.plugin_registry import BBSPlugin

if TYPE_CHECKING:
    from bbs.core.session import BBSSession
    from bbs.core.terminal import Terminal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS heard_stations (
    callsign    TEXT    NOT NULL COLLATE NOCASE,
    dest        TEXT    NOT NULL DEFAULT '' COLLATE NOCASE,
    transport   TEXT    NOT NULL DEFAULT '',
    via         TEXT    NOT NULL DEFAULT '',
    first_heard INTEGER NOT NULL,
    last_heard  INTEGER NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
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
"""

_DEFAULT_MAX_AGE_HOURS = 24


def _fmt_ts(ts: int) -> str:
    """Format a Unix timestamp as a compact local datetime string."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


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


# ── ASCII network map helpers ─────────────────────────────────────────────────

def _map_confirmed_edges(src: str, via: str, bbs_call: str) -> list[tuple[str, str]]:
    """
    Extract confirmed (source → dest) hop pairs from a via path string.

    Mirrors the same logic in server/routes/heard.py to avoid a circular
    import.  A digi sets the H-bit (*) only after it has relayed the frame,
    so all hops up to and including the last '*' are confirmed; everything
    after the last '*' is speculative and discarded.

    Empty via → direct reception → single edge (src, bbs_call).
    No '*' in via → we cannot confirm any relay → empty list.
    """
    if not via:
        return [(src, bbs_call)]
    hops = [h.strip() for h in via.split(",") if h.strip()]
    last_star = max(
        (i for i, h in enumerate(hops) if h.endswith("*")),
        default=-1,
    )
    if last_star < 0:
        return []
    confirmed = [h.rstrip("*") for h in hops[: last_star + 1]]
    chain = [src] + confirmed + [bbs_call]
    return [(chain[i], chain[i + 1]) for i in range(len(chain) - 1)]


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
    min_auth_level_name = "IDENTIFIED"

    def __init__(self) -> None:
        super().__init__()
        # In-memory cache; refreshed from DB on each session start.
        self._max_age_hours: int = _DEFAULT_MAX_AGE_HOURS

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)
        async with aiosqlite.connect(db_path, timeout=30) as db:
            await db.executescript(_SCHEMA)
            # Migrate heard_stations: add via column if absent.
            try:
                await db.execute(
                    "ALTER TABLE heard_stations ADD COLUMN via TEXT NOT NULL DEFAULT ''"
                )
            except Exception:
                pass  # column already exists
            # Migrate heard_paths: if via_base column is absent the table uses
            # the old schema (unique on raw via string).  Drop and recreate —
            # this is ephemeral data and correctness matters more than history.
            try:
                await db.execute("SELECT via_base FROM heard_paths LIMIT 1")
            except Exception:
                await db.execute("DROP TABLE heard_paths")
                await db.execute("""
                    CREATE TABLE heard_paths (
                        callsign  TEXT    NOT NULL COLLATE NOCASE,
                        transport TEXT    NOT NULL DEFAULT '',
                        via_base  TEXT    NOT NULL DEFAULT '',
                        via       TEXT    NOT NULL DEFAULT '',
                        first_seen INTEGER NOT NULL,
                        last_seen  INTEGER NOT NULL,
                        count      INTEGER NOT NULL DEFAULT 1,
                        UNIQUE (callsign, transport, via_base)
                    )
                """)
            # Seed max_age_hours from YAML config only if not already stored.
            default = int(cfg.get("max_age_hours", _DEFAULT_MAX_AGE_HOURS))
            await db.execute(
                "INSERT OR IGNORE INTO heard_settings (key, value) VALUES ('max_age_hours', ?)",
                (str(default),),
            )
            await db.commit()
        self._max_age_hours = await self._load_max_age()

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

    async def _prune(self) -> int:
        """Delete entries older than max_age_hours.  Returns the number removed."""
        if self._max_age_hours <= 0:
            return 0
        cutoff = int(time.time()) - self._max_age_hours * 3600
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            cur = await db.execute(
                "DELETE FROM heard_stations WHERE last_heard < ?", (cutoff,)
            )
            await db.execute(
                "DELETE FROM heard_paths WHERE last_seen < ?", (cutoff,)
            )
            await db.commit()
            return cur.rowcount

    # ── Transport observer ────────────────────────────────────────────────────

    async def on_heard(
        self, src: str, dest: str, via: list[str], ts: int, transport: str
    ) -> None:
        """
        Called by RF transports when a frame is received that is NOT addressed
        to the BBS.  Records/updates the heard-stations and heard_paths tables.
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

        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            # ── heard_stations: OR the * flags for the most-recent path ──────
            row = await (
                await db.execute(
                    "SELECT via FROM heard_stations WHERE callsign=? AND transport=?",
                    (src_up, transport),
                )
            ).fetchone()
            merged_via = _merge_via(row[0] if row else "", via_str)
            await db.execute(
                """
                INSERT INTO heard_stations
                    (callsign, dest, transport, via, first_heard, last_heard, count)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(callsign, transport) DO UPDATE SET
                    last_heard = excluded.last_heard,
                    count      = count + 1,
                    dest       = excluded.dest,
                    via        = ?
                """,
                (src_up, dest_up, transport, via_str, ts, ts, merged_via),
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
                    (src_up, transport, via_base, merged_path_via, ts, ts, merged_path_via),
                )
            await db.commit()

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
            for edge in _map_confirmed_edges(src, via, bbs_call):
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

    async def _station_count(self) -> int:
        """Return the number of rows currently in heard_stations."""
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                async with db.execute("SELECT COUNT(*) FROM heard_stations") as cur:
                    row = await cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    async def handle_session(self, session: "BBSSession") -> None:
        term = session.term
        self._max_age_hours = await self._load_max_age()
        await self._prune()

        limit    = int(self._cfg.get("limit", 200))
        is_sysop = session.auth.is_sysop

        # Action requested this iteration; None = show menu only.
        action: str | None = None

        while True:
            # ── Count for menu label ─────────────────────────────────────────
            count     = await self._station_count()
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

            if action is None:
                # First iteration or post-map: just show the menu.
                await term.send_menu("HEARD STATIONS", menu)
                action = (await term.readline(max_len=4, timeout=120)).strip().upper()

            # ── Dispatch ────────────────────────────────────────────────────
            if action == "Q" or not action:
                break

            if action == "C" and is_sysop:
                await self._configure(session)
                action = None
                continue

            if action in ("M", "MS"):
                bbs_call = session.cfg.callsign.upper()
                data     = await self._build_map_data(bbs_call)
                map_lines = _render_ascii_map(
                    bbs_call, data,
                    digis_only=(action == "M"),
                    term=term,
                )
                await term.paginate(map_lines)
                action = None
                continue

            if action in ("H", "HS"):
                async with aiosqlite.connect(self._db_path, timeout=30) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute(
                        """
                        SELECT hs.callsign, hs.dest, hs.transport, hs.via,
                               hs.first_heard, hs.last_heard, hs.count,
                               (SELECT COUNT(*) FROM heard_paths hp
                                 WHERE hp.callsign = hs.callsign
                                   AND hp.transport = hs.transport
                                   AND hp.via_base = '') AS direct_count,
                               (SELECT COUNT(*) FROM heard_paths hp
                                 WHERE hp.callsign = hs.callsign
                                   AND hp.transport = hs.transport
                                   AND hp.via_base != '') AS digi_count,
                               (SELECT hp.via FROM heard_paths hp
                                 WHERE hp.callsign = hs.callsign
                                   AND hp.transport = hs.transport
                                   AND hp.via_base != ''
                                 ORDER BY hp.last_seen DESC LIMIT 1) AS best_digi_via
                        FROM heard_stations hs
                        ORDER BY hs.last_heard DESC
                        LIMIT ?
                        """,
                        (limit,),
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
                    await term.paginate(lines)
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
                    await term.paginate(lines)

            # After any listing/map, go back to menu-only next iteration
            action = None

        await term.sendln()

    async def _clear(self) -> int:
        """Delete all rows from heard_stations and heard_paths.  Returns rows removed."""
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            cur = await db.execute("DELETE FROM heard_stations")
            removed = cur.rowcount
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
            await term.send_menu(
                "CONFIGURE",
                [("A", f"Set max age (current: {self._max_age_hours}h)"),
                 ("X", "Clear all heard entries"),
                 ("Q", "Back")],
            )
            choice = (await term.readline(max_len=4, timeout=60)).strip().upper()
            if choice == "Q" or not choice:
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
