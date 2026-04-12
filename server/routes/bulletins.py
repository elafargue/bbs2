"""
server/routes/bulletins.py — Bulletin area management REST API.

All endpoints require sysop login.

GET    /api/bulletins/areas              — list all areas with message counts
POST   /api/bulletins/areas              — create an area
PATCH  /api/bulletins/areas/<id>         — update name / description / default flag
DELETE /api/bulletins/areas/<id>         — delete area (cascades messages)
POST   /api/bulletins/areas/<id>/default — set as default area (clears others)
DELETE /api/bulletins/areas/<id>/default — clear default flag
"""
from __future__ import annotations

import re
import sqlite3

from flask import jsonify, request, session

from server.app import app

_AREA_NAME_RE = re.compile(r'^[A-Z0-9][A-Z0-9\-]{0,19}$')


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _sync_db() -> sqlite3.Connection | None:
    from server.app import bbs_engine
    if bbs_engine is None:
        return None
    return sqlite3.connect(str(bbs_engine.cfg.db_path))


def _area_row(row, cursor) -> dict:
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


# ── Ensure the schema has an is_default column (added lazily) ─────────────────

def _ensure_default_col(db: sqlite3.Connection) -> None:
    try:
        db.execute("ALTER TABLE bulletin_areas ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


# ── List ──────────────────────────────────────────────────────────────────────

@app.route("/api/bulletins/areas", methods=["GET"])
def list_bulletin_areas():
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        _ensure_default_col(db)
        cur = db.cursor()
        cur.execute("""
            SELECT a.id, a.name, a.description, a.is_default,
                   a.read_level, a.post_level, a.created_at,
                   COUNT(m.id) AS message_count
            FROM bulletin_areas a
            LEFT JOIN bulletin_messages m ON m.area_id = a.id AND m.deleted = 0
            GROUP BY a.id
            ORDER BY a.name COLLATE NOCASE
        """)
        rows = cur.fetchall()
        return jsonify([_area_row(r, cur) for r in rows])
    finally:
        db.close()


# ── Create ────────────────────────────────────────────────────────────────────

@app.route("/api/bulletins/areas", methods=["POST"])
def create_bulletin_area():
    err = _require_sysop()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).upper().strip()
    if not name or not _AREA_NAME_RE.match(name):
        return jsonify({"error": "Invalid area name (1–20 uppercase alphanumeric)"}), 400
    description = str(data.get("description", "")).strip()[:120]
    read_level  = int(data.get("read_level", 0))
    post_level  = int(data.get("post_level", 1))
    is_default  = 1 if data.get("is_default") else 0

    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        _ensure_default_col(db)
        cur = db.cursor()
        cur.execute("SELECT id FROM bulletin_areas WHERE name=? COLLATE NOCASE", (name,))
        if cur.fetchone():
            return jsonify({"error": f"Area '{name}' already exists"}), 409
        if is_default:
            cur.execute("UPDATE bulletin_areas SET is_default=0")
        cur.execute(
            "INSERT INTO bulletin_areas (name, description, read_level, post_level, is_default)"
            " VALUES (?,?,?,?,?)",
            (name, description, read_level, post_level, is_default),
        )
        db.commit()
        area_id = cur.lastrowid
        cur.execute("""
            SELECT a.id, a.name, a.description, a.is_default,
                   a.read_level, a.post_level, a.created_at,
                   COUNT(m.id) AS message_count
            FROM bulletin_areas a
            LEFT JOIN bulletin_messages m ON m.area_id = a.id AND m.deleted = 0
            WHERE a.id=?
            GROUP BY a.id
        """, (area_id,))
        row = cur.fetchone()
        return jsonify(_area_row(row, cur)), 201
    finally:
        db.close()


# ── Update ────────────────────────────────────────────────────────────────────

@app.route("/api/bulletins/areas/<int:area_id>", methods=["PATCH"])
def update_bulletin_area(area_id: int):
    err = _require_sysop()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        _ensure_default_col(db)
        cur = db.cursor()
        cur.execute("SELECT id FROM bulletin_areas WHERE id=?", (area_id,))
        if not cur.fetchone():
            return jsonify({"error": "Area not found"}), 404

        updates, params = [], []
        if "name" in data:
            name = str(data["name"]).upper().strip()
            if not _AREA_NAME_RE.match(name):
                return jsonify({"error": "Invalid area name"}), 400
            updates.append("name=?"); params.append(name)
        if "description" in data:
            updates.append("description=?"); params.append(str(data["description"])[:120])
        if "read_level" in data:
            updates.append("read_level=?"); params.append(int(data["read_level"]))
        if "post_level" in data:
            updates.append("post_level=?"); params.append(int(data["post_level"]))
        if "is_default" in data:
            if data["is_default"]:
                cur.execute("UPDATE bulletin_areas SET is_default=0")
            updates.append("is_default=?"); params.append(1 if data["is_default"] else 0)

        if updates:
            params.append(area_id)
            cur.execute(f"UPDATE bulletin_areas SET {', '.join(updates)} WHERE id=?", params)
            db.commit()

        cur.execute("""
            SELECT a.id, a.name, a.description, a.is_default,
                   a.read_level, a.post_level, a.created_at,
                   COUNT(m.id) AS message_count
            FROM bulletin_areas a
            LEFT JOIN bulletin_messages m ON m.area_id = a.id AND m.deleted = 0
            WHERE a.id=?
            GROUP BY a.id
        """, (area_id,))
        return jsonify(_area_row(cur.fetchone(), cur))
    finally:
        db.close()


# ── Delete ────────────────────────────────────────────────────────────────────

@app.route("/api/bulletins/areas/<int:area_id>", methods=["DELETE"])
def delete_bulletin_area(area_id: int):
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        _ensure_default_col(db)
        cur = db.cursor()
        cur.execute("SELECT name FROM bulletin_areas WHERE id=?", (area_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Area not found"}), 404
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("DELETE FROM bulletin_areas WHERE id=?", (area_id,))
        db.commit()
        return jsonify({"ok": True, "deleted": row[0]})
    finally:
        db.close()
