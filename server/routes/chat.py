"""
server/routes/chat.py — Chat room management REST API.

All mutating endpoints require sysop login.

GET    /api/chat/rooms                         — list rooms (stats from memory + DB)
GET    /api/chat/rooms/<room>/messages?n=50    — recent messages with IDs
DELETE /api/chat/rooms/<room>/messages/<id>    — delete a specific message
DELETE /api/chat/rooms/<room>                  — delete entire room
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


def _db_path() -> str | None:
    from server.app import bbs_engine
    if bbs_engine is None:
        return None
    return str(bbs_engine.cfg.db_path)


# ── Rooms list ────────────────────────────────────────────────────────────────

@app.route("/api/chat/rooms", methods=["GET"])
def list_chat_rooms():
    err = _require_sysop()
    if err:
        return err

    # Import in-memory room registry from the plugin.
    try:
        from bbs.plugins.chat.chat import _rooms
    except ImportError:
        _rooms = {}

    db = _sync_db()
    msg_counts: dict[str, int] = {}
    if db:
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT room, COUNT(*) FROM chat_history GROUP BY room"
            )
            for room_name, cnt in cur.fetchall():
                msg_counts[room_name] = cnt
        except sqlite3.OperationalError:
            pass  # table not yet created
        finally:
            db.close()

    result = []
    for name, room in _rooms.items():
        result.append({
            "name": room.name,
            "description": room.description,
            "members_online": room.member_count,
            "message_count": msg_counts.get(name, 0),
        })
    # Include rooms that are only in DB (no in-memory room object)
    for name, cnt in msg_counts.items():
        if name not in _rooms:
            result.append({
                "name": name,
                "description": "",
                "members_online": 0,
                "message_count": cnt,
            })

    return jsonify(sorted(result, key=lambda r: r["name"]))


# ── Messages in a room ────────────────────────────────────────────────────────

@app.route("/api/chat/rooms/<room>/messages", methods=["GET"])
def list_chat_messages(room: str):
    err = _require_sysop()
    if err:
        return err
    n = min(int(request.args.get("n", 50)), 500)
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id, ts, line FROM (
                SELECT id, ts, line FROM chat_history
                WHERE room = ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (room, n),
        )
        rows = cur.fetchall()
        return jsonify([{"id": r[0], "ts": r[1], "line": r[2]} for r in rows])
    except sqlite3.OperationalError:
        return jsonify([])
    finally:
        db.close()


# ── Delete a message ──────────────────────────────────────────────────────────

@app.route("/api/chat/rooms/<room>/messages/<int:msg_id>", methods=["DELETE"])
def delete_chat_message(room: str, msg_id: int):
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT line FROM chat_history WHERE id=? AND room=?", (msg_id, room)
        )
        row = cur.fetchone()
        if row is None:
            return jsonify({"error": "Message not found"}), 404
        line_text = row[0]
        cur.execute("DELETE FROM chat_history WHERE id=?", (msg_id,))
        db.commit()
    finally:
        db.close()

    # Remove from in-memory history too.
    try:
        from bbs.plugins.chat.chat import _rooms
        mem_room = _rooms.get(room)
        if mem_room and line_text in mem_room._history:
            mem_room._history.remove(line_text)
    except ImportError:
        pass

    return jsonify({"deleted": msg_id})


# ── Delete a room ─────────────────────────────────────────────────────────────

@app.route("/api/chat/rooms/<room>", methods=["DELETE"])
def delete_chat_room(room: str):
    err = _require_sysop()
    if err:
        return err
    db = _sync_db()
    if not db:
        return jsonify({"error": "BBS engine not running"}), 503
    try:
        cur = db.cursor()
        try:
            cur.execute("DELETE FROM chat_history WHERE room=?", (room,))
        except sqlite3.OperationalError:
            pass  # table not yet created — still proceed to remove in-memory room
        db.commit()
    finally:
        db.close()

    try:
        from bbs.plugins.chat.chat import _rooms
        mem_room = _rooms.pop(room, None)
        if mem_room is not None:
            mem_room._broadcast(
                f"*** Room {room} has been deleted by the sysop. Use /JOIN to switch rooms. ***",
                exclude=None,
            )
    except ImportError:
        pass

    return jsonify({"deleted": room})
