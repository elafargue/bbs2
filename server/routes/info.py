"""
server/routes/info.py — BBS Info message REST API.

All endpoints require sysop login.

GET /api/info  — return the current info message
PUT /api/info  — replace the info message
               body: {"message": "<text>"}
"""
from __future__ import annotations

import sqlite3

from flask import jsonify, request, session

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


@app.route("/api/info", methods=["GET"])
def get_bbs_info():
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.execute("SELECT message FROM bbs_info WHERE id = 1")
        row = cur.fetchone()
        return jsonify({"message": row[0] if row else ""})
    except sqlite3.OperationalError:
        # Table not yet created (info plugin disabled or never started)
        return jsonify({"message": ""})
    finally:
        db.close()


@app.route("/api/info", methods=["PUT"])
def update_bbs_info():
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", ""))
    try:
        db.execute("UPDATE bbs_info SET message = ? WHERE id = 1", (message,))
        db.commit()
        return jsonify({"ok": True})
    except sqlite3.OperationalError as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()
