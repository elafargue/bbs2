"""
server/routes/heard.py — Heard Stations REST API.

All endpoints require sysop login.

GET  /api/heard           — paginated list of heard stations
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
            SELECT callsign, dest, transport, via, first_heard, last_heard, count,
                   lat, lon, comment, source, nodename, position_source
            FROM heard_stations
            ORDER BY last_heard DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
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
    """Delete all entries from heard_stations and heard_paths."""
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute("DELETE FROM heard_stations")
        removed = cur.rowcount
        db.execute("DELETE FROM heard_paths")
        db.commit()
        return jsonify({"removed": removed})
    finally:
        db.close()


@app.route("/api/heard/<callsign>", methods=["PUT"])
def heard_update(callsign: str):
    """Update lat, lon, nodename, and comment for a heard station (matched by callsign + transport)."""
    err = _require_sysop()
    if err:
        return err
    callsign = callsign.strip().upper()
    data      = request.get_json(silent=True) or {}
    transport = str(data.get("transport", "")).strip()
    lat       = data.get("lat")
    lon       = data.get("lon")
    comment   = str(data.get("comment", ""))
    nodename  = str(data.get("nodename", "")).strip().upper()

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
    try:
        # Manual edits set position_source='manual' so the UI can distinguish
        # sysop-entered coordinates from those self-reported by a <MAP:...> beacon.
        cur = db.execute(
            """
            UPDATE heard_stations
               SET lat = ?, lon = ?, comment = ?, nodename = ?,
                   position_source = CASE
                       WHEN ? IS NOT NULL OR ? IS NOT NULL THEN 'manual'
                       ELSE position_source
                   END
             WHERE callsign = ? AND transport = ?
            """,
            (lat, lon, comment, nodename, lat, lon, callsign, transport),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Station not found"}), 404
        db.commit()
        return jsonify({"ok": True})
    except sqlite3.OperationalError as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        db.close()


@app.route("/api/heard/<callsign>", methods=["DELETE"])
def heard_delete_station(callsign: str):
    """Delete all heard_stations and heard_paths rows for a callsign (sysop only)."""
    err = _require_sysop()
    if err:
        return err
    callsign = callsign.strip().upper()
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute("DELETE FROM heard_stations WHERE callsign = ?", (callsign,))
        removed = cur.rowcount
        db.execute("DELETE FROM heard_paths WHERE callsign = ?", (callsign,))
        db.commit()
        if removed == 0:
            return jsonify({"error": "Station not found"}), 404
        return jsonify({"ok": True, "removed": removed})
    except sqlite3.OperationalError as exc:
        return jsonify({"error": str(exc)}), 500
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
        # All heard_paths rows (one per callsign+transport+via_base combo).
        # via_base='' means direct reception; we still want to create an edge.
        rows = db.execute(
            """
            SELECT hp.callsign, hp.via, hp.via_base
            FROM heard_paths hp
            JOIN heard_stations hs
              ON hs.callsign = hp.callsign
             AND hs.transport = hp.transport
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

    return jsonify({"bbs": bbs_call, "nodes": nodes, "edges": edges})
