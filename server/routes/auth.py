"""
server/routes/auth.py — Sysop web login / logout.

POST /api/admin/login   { "password": "…" }  → sets session cookie
POST /api/admin/logout  → clears session
GET  /api/admin/me      → current sysop identity
"""
from __future__ import annotations

import bcrypt
from flask import jsonify, request, session

from server.app import app


def _check_password(plain: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), stored_hash.encode())
    except Exception:
        return False


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    from server.app import bbs_engine

    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))

    if not password:
        return jsonify({"error": "Password required"}), 400

    if bbs_engine is None:
        return jsonify({"error": "BBS engine not running"}), 503

    stored_hash = bbs_engine.cfg.sysop_password_hash
    if not _check_password(password, stored_hash):
        return jsonify({"error": "Invalid password"}), 401

    session["sysop"] = True
    return jsonify({"ok": True})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/admin/me", methods=["GET"])
def admin_me():
    if session.get("sysop"):
        from server.app import bbs_engine
        callsign = bbs_engine.cfg.callsign if bbs_engine else "unknown"
        return jsonify({"sysop": True, "callsign": callsign})
    return jsonify({"sysop": False}), 401
