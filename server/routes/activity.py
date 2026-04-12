"""
server/routes/activity.py — Activity log REST endpoint.

GET /api/activity?n=100   — return last N log lines (default 100, max 500)
GET /api/activity/users   — current connected users snapshot
GET /api/activity/stats   — plugin stats snapshot
"""
from __future__ import annotations

from flask import jsonify, request, session

from server.app import app


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
    n = min(int(request.args.get("n", 100)), 500)
    return jsonify({"lines": bbs_engine.recent_log_lines(n)})


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
