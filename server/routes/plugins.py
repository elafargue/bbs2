"""
server/routes/plugins.py — Plugin management REST API.

GET  /api/plugins              — list all plugins with stats
POST /api/plugins/<name>/toggle — enable or disable a plugin
     body: {"enabled": true|false}
"""
from __future__ import annotations

from flask import jsonify, request, session

from server.app import app


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/api/plugins", methods=["GET"])
def list_plugins():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify({"error": "BBS engine not running"}), 503
    return jsonify(bbs_engine.plugin_stats_snapshot())


@app.route("/api/plugins/<name>/toggle", methods=["POST"])
def toggle_plugin(name):
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify({"error": "BBS engine not running"}), 503

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))

    ok = bbs_engine.plugin_registry.toggle(name, enabled)
    if not ok:
        return jsonify({"error": f"Plugin '{name}' not found"}), 404
    return jsonify({"ok": True, "name": name, "enabled": enabled})
