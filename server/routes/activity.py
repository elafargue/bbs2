"""
server/routes/activity.py — Activity log REST endpoints.

GET /api/activity?n=500        — return last N log lines from DB (default 500)
GET /api/activity/users        — current connected users snapshot
GET /api/activity/stats        — plugin stats snapshot
GET /api/activity/connections  — connection journal (last N days)
"""
from __future__ import annotations

from flask import jsonify, request, session

from server.app import app
from server.websocket.handlers import _read_activity_log


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/api/activity", methods=["GET"])
def get_activity():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify({"lines": []})
    n = int(request.args.get("n", 500))
    lines = _read_activity_log(str(bbs_engine.cfg.db_path), n)
    # Fall back to in-memory buffer if DB has nothing yet (fresh start)
    if not lines:
        lines = bbs_engine.recent_log_lines(n)
    return jsonify({"lines": lines})


@app.route("/api/activity/users", methods=["GET"])
def get_connected_users():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify([])
    return jsonify(bbs_engine.connected_users_snapshot())


@app.route("/api/activity/stats", methods=["GET"])
def get_plugin_stats():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify([])
    return jsonify(bbs_engine.plugin_stats_snapshot())


@app.route("/api/activity/connections", methods=["GET"])
def get_connection_log():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify([])
    import sqlite3
    days = bbs_engine.cfg.connection_log_days or 30
    cutoff = int(__import__("time").time()) - days * 86400
    try:
        db = sqlite3.connect(str(bbs_engine.cfg.db_path))
        db.row_factory = sqlite3.Row
        cur = db.execute(
            """
            SELECT callsign, transport, first_seen, last_seen, auth_level
            FROM connection_log
            WHERE last_seen >= ?
            ORDER BY last_seen DESC
            LIMIT 500
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        db.close()
    except Exception:
        rows = []
    return jsonify(rows)
