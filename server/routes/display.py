"""
server/routes/display.py — REST API for the framebuffer display plugin.

All endpoints require sysop login.

GET  /api/display/settings    — return current settings dict
PUT  /api/display/settings    — update settings; body: {key: value, ...}
GET  /api/display/status      — current runtime state (last_conns, bulletins, etc.)
POST /api/display/wake         — reset idle timer / wake from dim or off
"""
from __future__ import annotations

import sqlite3

from flask import jsonify, request, session

from server.app import app


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _get_plugin():
    from server.app import bbs_engine
    if bbs_engine is None:
        return None, (jsonify({"error": "BBS engine not running"}), 503)
    plugin = bbs_engine.plugin_registry.get("display")
    if plugin is None:
        return None, (jsonify({"error": "Display plugin not loaded"}), 404)
    return plugin, None


@app.route("/api/display/settings", methods=["GET"])
def display_get_settings():
    err = _require_sysop()
    if err:
        return err
    plugin, perr = _get_plugin()
    if perr:
        return perr
    return jsonify(dict(plugin._settings))


@app.route("/api/display/settings", methods=["PUT"])
def display_put_settings():
    err = _require_sysop()
    if err:
        return err
    plugin, perr = _get_plugin()
    if perr:
        return perr

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Empty body"}), 400

    # Validate keys
    from bbs.plugins.display.display import _DEFAULTS
    bad_keys = [k for k in data if k not in _DEFAULTS]
    if bad_keys:
        return jsonify({"error": f"Unknown settings keys: {bad_keys}"}), 400

    # Update in memory immediately (hot-reload for next render cycle)
    plugin.update_settings({k: str(v) for k, v in data.items()})

    # Persist to DB asynchronously via the engine's loop
    from server.app import bbs_engine
    if bbs_engine and bbs_engine._loop:
        import asyncio
        asyncio.run_coroutine_threadsafe(
            plugin.async_save_settings({k: str(v) for k, v in data.items()}),
            bbs_engine._loop,
        )

    return jsonify({"ok": True, "settings": dict(plugin._settings)})


@app.route("/api/display/status", methods=["GET"])
def display_get_status():
    err = _require_sysop()
    if err:
        return err
    plugin, perr = _get_plugin()
    if perr:
        return perr
    return jsonify(plugin.get_stats())


@app.route("/api/display/wake", methods=["POST"])
def display_wake():
    err = _require_sysop()
    if err:
        return err
    plugin, perr = _get_plugin()
    if perr:
        return perr
    plugin.wake()
    return jsonify({"ok": True})
