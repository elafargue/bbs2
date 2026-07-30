"""
server/routes/heard.py — Heard Stations REST API.

All endpoints require sysop login.

GET  /api/heard           — paginated list of heard stations (one row per callsign)
GET  /api/heard/settings  — current settings (max_age_hours)
PUT  /api/heard/settings  — update settings; body: {"max_age_hours": N}
"""
from __future__ import annotations

import sqlite3

from flask import jsonify, request, session

from bbs.plugins.heard.graph import confirmed_edges
from server.app import app


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _sync_db() -> sqlite3.Connection | None:
    from server.app import bbs_engine
    if bbs_engine is None:
        return None
    return sqlite3.connect(str(bbs_engine.cfg.db_path))


@app.route("/api/heard", methods=["GET"])
def heard_list():
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        import time as _time
        limit = min(int(request.args.get("limit", 500)), 2000)
        try:
            row = db.execute(
                "SELECT value FROM heard_settings WHERE key = 'max_age_hours'"
            ).fetchone()
            max_age_hours = int(row[0]) if row else 24
        except sqlite3.OperationalError:
            max_age_hours = 24
        cutoff = (
            int(_time.time()) - max_age_hours * 3600
            if max_age_hours > 0
            else 0
        )
        cur = db.execute(
            """
            SELECT
                s.callsign,
                s.lat, s.lon, s.comment, s.position_source,
                s.netrom_alias, s.beacon_alias, s.kanode_alias,
                COALESCE(NULLIF(s.kanode_alias,''), NULLIF(s.beacon_alias,''), NULLIF(s.netrom_alias,''), '') AS nodename,
                s.first_seen  AS first_heard,
                s.last_seen   AS last_heard,
                COALESCE(SUM(CASE WHEN e.transport != '' THEN e.count ELSE 0 END), 0) AS count,
                GROUP_CONCAT(DISTINCT CASE WHEN e.transport != '' THEN e.transport END)
                    AS transports_csv,
                CASE
                    WHEN MAX(CASE WHEN e.source = 'heard'  THEN 1 ELSE 0 END) = 1 THEN 'heard'
                    WHEN MAX(CASE WHEN e.source = 'netrom' THEN 1 ELSE 0 END) = 1 THEN 'netrom'
                    ELSE 'via'
                END AS source,
                (SELECT e2.via FROM heard_events e2
                  WHERE e2.callsign = s.callsign AND e2.via != ''
                  ORDER BY e2.last_heard DESC LIMIT 1) AS via,
                (SELECT e2.dest FROM heard_events e2
                  WHERE e2.callsign = s.callsign AND e2.dest != ''
                  ORDER BY e2.last_heard DESC LIMIT 1) AS dest,
                (SELECT e2.transport FROM heard_events e2
                  WHERE e2.callsign = s.callsign
                  ORDER BY
                      CASE WHEN e2.transport NOT IN ('', 'netrom') THEN 0
                           WHEN e2.transport = 'netrom' THEN 1
                           ELSE 2 END,
                      e2.last_heard DESC
                  LIMIT 1) AS transport
            FROM stations s
            LEFT JOIN heard_events e ON e.callsign = s.callsign
            GROUP BY s.callsign
            ORDER BY s.last_seen DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["transports"] = [t for t in (d.pop("transports_csv") or "").split(",") if t]
            d["expired"] = bool(d["source"] == "heard" and d["last_heard"] < cutoff)
            rows.append(d)
        return jsonify(rows)
    except sqlite3.OperationalError:
        return jsonify([])
    finally:
        db.close()


@app.route("/api/heard/paths", methods=["GET"])
def heard_paths():
    """Return the per-path breakdown for a given callsign."""
    err = _require_sysop()
    if err:
        return err
    callsign = request.args.get("callsign", "").strip().upper()
    if not callsign:
        return jsonify({"error": "callsign parameter required"}), 400
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute(
            """
            SELECT via, transport, first_seen, last_seen, count
            FROM heard_paths
            WHERE callsign = ?
            ORDER BY last_seen DESC
            """,
            (callsign,),
        )
        cols = [d[0] for d in cur.description]
        return jsonify([dict(zip(cols, r)) for r in cur.fetchall()])
    except sqlite3.OperationalError:
        return jsonify([])
    finally:
        db.close()


@app.route("/api/heard", methods=["DELETE"])
def heard_clear():
    """Delete all entries from stations, heard_events and heard_paths."""
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute("DELETE FROM stations")
        removed = cur.rowcount
        db.execute("DELETE FROM heard_events")
        db.execute("DELETE FROM heard_paths")
        db.commit()
        return jsonify({"removed": removed})
    finally:
        db.close()


@app.route("/api/heard/<callsign>", methods=["PUT"])
def heard_update(callsign: str):
    """Update lat, lon, beacon_alias (nodename), kanode_alias, and comment for a station.

    When kanode_alias is set and a stations row with that callsign already exists,
    the old row is merged into this one: position data is transferred if this station
    lacks it, heard_events and heard_paths are re-attributed, then the old row is
    deleted.
    """
    err = _require_sysop()
    if err:
        return err
    callsign     = callsign.strip().upper()
    data         = request.get_json(silent=True) or {}
    lat          = data.get("lat")
    lon          = data.get("lon")
    comment      = str(data.get("comment", ""))
    nodename     = str(data.get("nodename", "")).strip().upper()
    kanode_alias = str(data.get("kanode_alias", "")).strip().upper()

    if lat is not None:
        try:
            lat = float(lat)
            if not (-90.0 <= lat <= 90.0):
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "lat must be a number between -90 and 90"}), 400
    if lon is not None:
        try:
            lon = float(lon)
            if not (-180.0 <= lon <= 180.0):
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "lon must be a number between -180 and 180"}), 400

    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    # Manage the transaction explicitly so the entire UPDATE+merge+DELETE
    # block runs under a single write lock; without this an on_heard()
    # write can land in the to-be-merged kanode_alias row mid-merge and
    # split heard_events between the old and new callsigns.
    db.isolation_level = None  # autocommit mode — we BEGIN/COMMIT manually
    try:
        db.execute("BEGIN IMMEDIATE")

        cur = db.execute(
            """
            UPDATE stations
               SET lat = ?, lon = ?, comment = ?, beacon_alias = ?, kanode_alias = ?,
                   position_source = CASE
                       WHEN ? IS NOT NULL OR ? IS NOT NULL THEN 'manual'
                       ELSE position_source
                   END
             WHERE callsign = ?
            """,
            (lat, lon, comment, nodename, kanode_alias, lat, lon, callsign),
        )
        if cur.rowcount == 0:
            db.execute("ROLLBACK")
            return jsonify({"error": "Station not found"}), 404

        merge_info: dict = {}

        # ── Ka-Node merge: absorb the old standalone digi row if it exists ──
        if kanode_alias:
            old = db.execute(
                "SELECT lat, lon, position_source FROM stations WHERE callsign = ?",
                (kanode_alias,),
            ).fetchone()
            if old:
                old_lat, old_lon, old_pos_src = old

                # Transfer position to owner if owner has none
                if lat is None and old_lat is not None:
                    db.execute(
                        "UPDATE stations SET lat=?, lon=?, position_source=? WHERE callsign=?",
                        (old_lat, old_lon, old_pos_src or "manual", callsign),
                    )
                    merge_info["position_transferred"] = True

                events_count = (db.execute(
                    "SELECT COUNT(*) FROM heard_events WHERE callsign = ?", (kanode_alias,)
                ).fetchone() or (0,))[0]

                # Re-attribute heard_events, merging counts on conflict
                db.execute(
                    """
                    INSERT INTO heard_events
                        (callsign, transport, source, first_heard, last_heard,
                         count, last_direct_heard, dest, via)
                    SELECT ?, transport, source, first_heard, last_heard,
                           count, last_direct_heard, dest, via
                    FROM heard_events WHERE callsign = ?
                    ON CONFLICT(callsign, transport) DO UPDATE SET
                        count             = count             + excluded.count,
                        first_heard       = MIN(first_heard,  excluded.first_heard),
                        last_heard        = MAX(last_heard,   excluded.last_heard),
                        last_direct_heard = MAX(last_direct_heard, excluded.last_direct_heard)
                    """,
                    (callsign, kanode_alias),
                )

                # Re-attribute heard_paths, merging counts on conflict
                db.execute(
                    """
                    INSERT INTO heard_paths
                        (callsign, transport, via_base, via, first_seen, last_seen, count)
                    SELECT ?, transport, via_base, via, first_seen, last_seen, count
                    FROM heard_paths WHERE callsign = ?
                    ON CONFLICT(callsign, transport, via_base) DO UPDATE SET
                        count      = count      + excluded.count,
                        first_seen = MIN(first_seen, excluded.first_seen),
                        last_seen  = MAX(last_seen,  excluded.last_seen)
                    """,
                    (callsign, kanode_alias),
                )

                # Delete the now-empty old row
                db.execute("DELETE FROM heard_events WHERE callsign = ?", (kanode_alias,))
                db.execute("DELETE FROM heard_paths  WHERE callsign = ?", (kanode_alias,))
                db.execute("DELETE FROM stations     WHERE callsign = ?", (kanode_alias,))

                merge_info["merged"]        = kanode_alias
                merge_info["events_merged"] = events_count

        db.execute("COMMIT")

        # Refresh the plugin's in-memory Ka-Node map after EVERY PUT, not just
        # when a non-empty alias was set — clearing an alias must also evict
        # the stale mapping from the cache.
        import asyncio as _asyncio
        from server.app import bbs_engine as _engine
        if _engine is not None:
            _plugin = _engine.plugin_registry.get("heard")
            _loop   = _engine._loop
            if _plugin is not None and _loop is not None and _loop.is_running():
                _asyncio.run_coroutine_threadsafe(
                    _plugin._refresh_kanode_map(),  # type: ignore[attr-defined]
                    _loop,
                )

        return jsonify({"ok": True, **merge_info})
    except sqlite3.OperationalError as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return jsonify({"error": str(exc)}), 500
    finally:
        db.close()


@app.route("/api/heard/<callsign>", methods=["DELETE"])
def heard_delete_station(callsign: str):
    """Delete all rows for a callsign from stations, heard_events, and heard_paths."""
    err = _require_sysop()
    if err:
        return err
    callsign = callsign.strip().upper()
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute("DELETE FROM stations WHERE callsign = ?", (callsign,))
        removed = cur.rowcount
        db.execute("DELETE FROM heard_events WHERE callsign = ?", (callsign,))
        db.execute("DELETE FROM heard_paths WHERE callsign = ?", (callsign,))
        db.commit()
        if removed == 0:
            return jsonify({"error": "Station not found"}), 404
        return jsonify({"ok": True, "removed": removed})
    except sqlite3.OperationalError as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        db.close()


@app.route("/api/heard/netrom-routes", methods=["GET"])
def netrom_routes_list():
    """Return the full NET/ROM routing table (sysop only)."""
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute(
            """
            SELECT dest_call, alias, neighbor_call, quality, via_call, via_alias, last_seen
            FROM netrom_routes
            ORDER BY dest_call ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return jsonify([dict(zip(cols, r)) for r in cur.fetchall()])
    except sqlite3.OperationalError:
        return jsonify([])
    finally:
        db.close()


@app.route("/api/heard/settings", methods=["GET"])
def heard_settings_get():
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute(
            "SELECT value FROM heard_settings WHERE key = 'max_age_hours'"
        )
        row = cur.fetchone()
        return jsonify({"max_age_hours": int(row[0]) if row else 24})
    except sqlite3.OperationalError:
        return jsonify({"max_age_hours": 24})
    finally:
        db.close()


@app.route("/api/heard/settings", methods=["PUT"])
def heard_settings_put():
    err = _require_sysop()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        hours = int(data.get("max_age_hours", 24))
        if hours < 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "max_age_hours must be a non-negative integer"}), 400

    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        db.execute(
            "INSERT OR REPLACE INTO heard_settings (key, value) VALUES ('max_age_hours', ?)",
            (str(hours),),
        )
        db.commit()
    except sqlite3.OperationalError as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        db.close()

    # Update the in-memory plugin state immediately (if the plugin is loaded).
    from server.app import bbs_engine
    if bbs_engine is not None:
        plugin = bbs_engine.plugin_registry.get("heard")
        if plugin is not None:
            import asyncio
            loop = bbs_engine._loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    plugin._save_max_age(hours),  # type: ignore[attr-defined]
                    loop,
                )

    return jsonify({"ok": True, "max_age_hours": hours})


# ── Network graph ─────────────────────────────────────────────────────────────


@app.route("/api/heard/graph", methods=["GET"])
def heard_graph():
    """
    Build a confirmed-path network graph from heard_paths.

    Response:
      {
        "bbs": "W6ELA",
        "nodes": { "CALLSIGN": {"type": "bbs"|"station"|"digi"|"both"}, ... },
        "edges": [{"source": "A", "target": "B", "count": N}, ...]
      }
    """
    err = _require_sysop()
    if err:
        return err

    from server.app import bbs_engine
    bbs_call = (
        bbs_engine.cfg.callsign.upper()
        if bbs_engine is not None
        else "BBS"
    )

    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503

    try:
        rows = db.execute(
            """
            SELECT hp.callsign, hp.via, hp.via_base
            FROM heard_paths hp
            JOIN stations s ON s.callsign = hp.callsign
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        db.close()

    # Aggregate confirmed edges across all paths.
    edge_counts: dict[tuple[str, str], int] = {}
    # Track node roles.
    stations: set[str] = set()   # appeared as a source
    digis: set[str] = set()      # appeared inside a via path

    for row in rows:
        src      = row[0].upper()
        via_str  = row[1] or ""   # OR'd starred path
        via_base = row[2] or ""

        stations.add(src)

        edges = confirmed_edges(src, via_str, bbs_call)
        for a, b in edges:
            # All intermediate nodes (not the source, not the BBS) are digis.
            if a not in (src, bbs_call):
                digis.add(a)
            if b not in (src, bbs_call):
                digis.add(b)
            key = (a, b)
            edge_counts[key] = edge_counts.get(key, 0) + 1

    # Build node map with types.
    all_nodes = stations | digis | {bbs_call}
    nodes: dict[str, dict] = {}
    for call in all_nodes:
        if call == bbs_call:
            ntype = "bbs"
        elif call in stations and call in digis:
            ntype = "both"
        elif call in digis:
            ntype = "digi"
        else:
            ntype = "station"
        nodes[call] = {"type": ntype}

    edges = [
        {"source": a, "target": b, "count": c}
        for (a, b), c in edge_counts.items()
    ]

    # ── Enrich with NETROM data ──────────────────────────────────────────────
    # Re-open DB for NETROM tables (closed above in finally).
    db2 = _sync_db()
    netrom_edges: list[dict] = []
    if db2:
        try:
            # Backfill nodenames onto all RF nodes that have one.
            try:
                nodename_rows = db2.execute(
                    "SELECT callsign,"
                    " COALESCE(NULLIF(kanode_alias,''), NULLIF(beacon_alias,''),"
                    "          NULLIF(netrom_alias,''), '') AS nodename"
                    " FROM stations"
                    " WHERE kanode_alias != '' OR beacon_alias != '' OR netrom_alias != ''"
                ).fetchall()
                for call, name in nodename_rows:
                    call_up = call.upper()
                    if call_up in nodes and name:
                        nodes[call_up]["nodename"] = name
            except sqlite3.OperationalError:
                pass

            # Add NETROM-known nodes and routing edges.
            # Each row: via_call advertised dest_call reachable via neighbor_call.
            # Edges we can derive:
            #   via_call → neighbor_call  (direct AX.25 link; NETROM only routes to
            #                              a direct neighbor as next hop)
            #   neighbor_call → dest_call (onward hop, only when dest != neighbor)
            # The BBS is NOT added as a routing node unless it appears as via_call
            # in someone else's routing table (i.e. it's actively advertising).
            try:
                nr_rows = db2.execute(
                    "SELECT dest_call, alias, neighbor_call, quality, via_call, via_alias"
                    " FROM netrom_routes"
                ).fetchall()
                seen_direct: set[tuple[str, str]] = set()
                for dest_call, alias, neighbor_call, quality, via_call, via_alias in nr_rows:
                    dest_up = dest_call.upper()
                    nbr_up  = neighbor_call.upper()
                    via_up  = via_call.upper()
                    # Ensure all three nodes exist in the node map.
                    if dest_up not in nodes:
                        nodes[dest_up] = {"type": "netrom", "nodename": alias}
                    elif alias and not nodes[dest_up].get("nodename"):
                        nodes[dest_up]["nodename"] = alias
                    if nbr_up not in nodes:
                        nodes[nbr_up] = {"type": "netrom", "nodename": ""}
                    if via_up not in nodes:
                        nodes[via_up] = {"type": "netrom", "nodename": via_alias}
                    elif via_alias and not nodes[via_up].get("nodename"):
                        nodes[via_up]["nodename"] = via_alias
                    # via_call → neighbor_call: direct AX.25 link (emit once per pair).
                    direct_key = (via_up, nbr_up)
                    if direct_key not in seen_direct:
                        seen_direct.add(direct_key)
                        netrom_edges.append(
                            {"source": via_up, "target": nbr_up, "quality": quality}
                        )
                    # neighbor_call → dest_call: onward hop (skip when dest IS neighbor).
                    if nbr_up != dest_up:
                        netrom_edges.append(
                            {"source": nbr_up, "target": dest_up, "quality": quality}
                        )
            except sqlite3.OperationalError:
                pass
        finally:
            db2.close()

    return jsonify({
        "bbs": bbs_call,
        "nodes": nodes,
        "edges": edges,
        "netrom_edges": netrom_edges,
    })
