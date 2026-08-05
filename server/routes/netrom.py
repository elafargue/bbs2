"""
server/routes/netrom.py — NET/ROM node live-activity REST API.

GET /api/netrom/activity — live node sessions (who's at => or bridged onward)
                           plus gateway-safety state (caps, live count, recent
                           refusals). Read-only, sysop-only. Returns
                           {"enabled": false} when the node isn't running.
"""
from __future__ import annotations

from flask import jsonify, session

from server.app import app


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/api/netrom/activity", methods=["GET"])
def get_netrom_activity():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify({"enabled": False})
    return jsonify(bbs_engine.netrom_snapshot())
