"""
server/routes/users.py — User management REST API.

All endpoints require sysop login.

GET    /api/users              — list all users (or ?pending=1 for pending only)
POST   /api/users              — create a user record
GET    /api/users/<id>         — get one user
PATCH  /api/users/<id>         — update: approved, banned, name, qth
DELETE /api/users/<id>         — delete user
POST   /api/users/<id>/secret  — provision OTP secret (body: {"type": "totp|hotp", "secret": "<base32_optional>"})
DELETE /api/users/<id>/secret  — clear OTP secret
"""
from __future__ import annotations

import base64
import os
import sqlite3

from flask import jsonify, request, session

from bbs.core.auth import otp_provisioning_uri
from server.app import app


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _sync_db():
    """Return a synchronous sqlite3 connection to the BBS database."""
    from server.app import bbs_engine
    if bbs_engine is None:
        return None
    return sqlite3.connect(str(bbs_engine.cfg.db_path))


def _user_row_to_dict(row, cursor) -> dict:
    cols = [d[0] for d in cursor.description]
    d = dict(zip(cols, row))
    # Never expose raw secret bytes over REST
    d.pop("totp_secret", None)
    d["has_secret"] = bool(d.get("totp_secret_flag"))
    d.pop("totp_secret_flag", None)
    return d


@app.route("/api/users", methods=["POST"])
def create_user():
    """Manually create a user account."""
    err = _require_sysop()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    callsign = str(data.get("callsign", "")).upper().strip()
    if not callsign:
        return jsonify({"error": "callsign is required"}), 400
    # Basic callsign sanity: alphanumeric + hyphen, 3-10 chars
    import re as _re
    if not _re.match(r"^[A-Z0-9]{1,6}(-\d{1,2})?$", callsign):
        return jsonify({"error": "invalid callsign format"}), 400

    name = str(data.get("name", "")).strip()[:64]
    qth = str(data.get("qth", "")).strip()[:64]
    approved = 1 if data.get("approved") else 0

    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM users WHERE callsign=? COLLATE NOCASE", (callsign,))
        if cur.fetchone():
            return jsonify({"error": "Callsign already exists"}), 409
        cur.execute(
            "INSERT INTO users (callsign, name, qth, approved) VALUES (?, ?, ?, ?)",
            (callsign, name, qth, approved),
        )
        db.commit()
        user_id = cur.lastrowid
        cur.execute(
            "SELECT *, (totp_secret IS NOT NULL AND length(totp_secret)>0) as totp_secret_flag "
            "FROM users WHERE id=?",
            (user_id,),
        )
        row = cur.fetchone()
        return jsonify(_user_row_to_dict(row, cur)), 201
    finally:
        db.close()


@app.route("/api/users", methods=["GET"])
def list_users():
    err = _require_sysop()
    if err:
        return err
    pending_only = request.args.get("pending") == "1"
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.cursor()
        if pending_only:
            cur.execute(
                "SELECT *, (totp_secret IS NOT NULL AND length(totp_secret)>0) as totp_secret_flag "
                "FROM users WHERE approved=0 ORDER BY created_at"
            )
        else:
            cur.execute(
                "SELECT *, (totp_secret IS NOT NULL AND length(totp_secret)>0) as totp_secret_flag "
                "FROM users ORDER BY callsign COLLATE NOCASE"
            )
        rows = cur.fetchall()
        return jsonify([_user_row_to_dict(r, cur) for r in rows])
    finally:
        db.close()


@app.route("/api/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT *, (totp_secret IS NOT NULL AND length(totp_secret)>0) as totp_secret_flag "
            "FROM users WHERE id=?",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(_user_row_to_dict(row, cur))
    finally:
        db.close()


@app.route("/api/users/<int:user_id>", methods=["PATCH"])
def update_user(user_id):
    err = _require_sysop()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.cursor()
        allowed = {"approved", "banned", "name", "qth"}
        updates = {k: v for k, v in data.items() if k in allowed}
        if not updates:
            return jsonify({"error": "No valid fields to update"}), 400
        set_clause = ", ".join(f"{k}=?" for k in updates)
        cur.execute(
            f"UPDATE users SET {set_clause} WHERE id=?",
            list(updates.values()) + [user_id],
        )
        db.commit()
        return jsonify({"ok": True, "updated": list(updates.keys())})
    finally:
        db.close()


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/users/<int:user_id>/secret", methods=["POST"])
def set_user_secret(user_id):
    """
    Provision an OTP secret for a user.

    Request body (JSON):
      type    — "totp" (default) or "hotp"
      secret  — base32-encoded secret (optional; generated if omitted)

    Response includes the base32 secret and an otpauth:// provisioning URI
    suitable for generating a QR code.  The raw secret bytes are never
    returned after this call, so store the provisioning_uri somewhere safe.
    """
    err = _require_sysop()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    otp_type = str(data.get("type", "totp")).lower().strip()
    if otp_type not in ("totp", "hotp"):
        return jsonify({"error": 'type must be "totp" or "hotp"'}), 400

    secret_b32 = str(data.get("secret", "")).strip().upper()
    if secret_b32:
        # Validate and decode caller-supplied base32 secret
        try:
            # Add padding if needed
            padded = secret_b32 + "=" * (-len(secret_b32) % 8)
            secret_bytes = base64.b32decode(padded)
        except Exception:
            return jsonify({"error": "invalid base32 secret"}), 400
        if len(secret_bytes) < 10:
            return jsonify({"error": "secret too short (minimum 10 bytes / 16 base32 chars)"}), 400
    else:
        # Generate a fresh 20-byte (160-bit) secret
        secret_bytes = os.urandom(20)
        secret_b32 = base64.b32encode(secret_bytes).decode()

    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        # Look up callsign for the provisioning URI
        cur = db.cursor()
        cur.execute("SELECT callsign FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "User not found"}), 404
        callsign = row[0]

        from server.app import bbs_engine
        issuer = bbs_engine.cfg.name if bbs_engine else "BBS"

        db.execute(
            "UPDATE users SET totp_secret=?, otp_type=?, hotp_counter=0 WHERE id=?",
            (secret_bytes, otp_type, user_id),
        )
        db.commit()

        uri = otp_provisioning_uri(secret_bytes, callsign, issuer, otp_type)
        return jsonify({
            "ok": True,
            "type": otp_type,
            "base32": secret_b32,
            "provisioning_uri": uri,
        })
    finally:
        db.close()


@app.route("/api/users/<int:user_id>/secret", methods=["DELETE"])
def clear_user_secret(user_id):
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        db.execute("UPDATE users SET totp_secret=NULL, hotp_counter=0 WHERE id=?", (user_id,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()
