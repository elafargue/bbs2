"""
server/websocket/handlers.py — SocketIO event handlers and the asyncio→Flask bridge.

Per flask-vue-scaffold-conventions:
  - All handlers defined here with @socketio.on(...)
  - Registered by importing this module in web_interface.py
  - safe_emit() wraps all outbound emits

The asyncio→Flask bridge is a daemon thread that drains bbs_engine.event_queue
and emits SocketIO events to connected sysop clients.  It also persists every
log line to the activity_log DB table so the log survives restarts.
"""
from __future__ import annotations

import logging
import queue as stdlib_queue
import sqlite3
import threading
import time
from typing import Any

from flask import request
from flask_socketio import emit, join_room, leave_room

from server.app import app, socketio

logger = logging.getLogger(__name__)

_SYSOP_ROOM = "sysop"
_bridge_thread: threading.Thread | None = None

# Per web-terminal session state: sid → {reader, output_queue, drain_thread}
_web_sessions: dict[str, dict] = {}


def safe_emit(event: str, data: Any, room: str | None = None) -> None:
    """Emit a SocketIO event, swallowing errors from disconnected clients."""
    try:
        if room:
            socketio.emit(event, data, to=room)
        else:
            socketio.emit(event, data)
    except Exception:
        pass


def _read_activity_log(db_path: str, n: int) -> list[str]:
    """Return the last *n* activity log lines from the DB, oldest first."""
    try:
        with sqlite3.connect(str(db_path)) as db:
            cur = db.execute(
                "SELECT line FROM activity_log ORDER BY id DESC LIMIT ?", (n,)
            )
            rows = cur.fetchall()
            return [r[0] for r in reversed(rows)]
    except Exception:
        logger.debug("Could not read activity_log from DB", exc_info=True)
        return []


def _persist_log_line(db_path: str, line: str) -> None:
    """Insert a log line into the persistent activity_log table."""
    try:
        with sqlite3.connect(str(db_path)) as db:
            db.execute("INSERT INTO activity_log (line) VALUES (?)", (line,))
    except Exception:
        logger.debug("Failed to persist log line to DB", exc_info=True)


# ── Connection events ─────────────────────────────────────────────────────────

@socketio.on("connect")
def on_ws_connect():
    logger.debug("WebSocket client connected: %s", request.sid)


@socketio.on("disconnect")
def on_ws_disconnect():
    logger.debug("WebSocket client disconnected: %s", request.sid)
    _cleanup_web_session(request.sid)


# ── Sysop room join/leave ─────────────────────────────────────────────────────

@socketio.on("join_admin")
def on_join_admin(data):
    """
    Client joins the sysop room.  Validates sysop session before admitting.
    data: {} (session cookie handles auth)
    """
    from flask import session as flask_session
    if not flask_session.get("sysop"):
        emit("error", {"message": "Not authorized"})
        return

    join_room(_SYSOP_ROOM)

    from server.app import bbs_engine
    if bbs_engine is None:
        emit("bbs_status", {"online": False})
        return

    # Load full persistent log history from DB; fall back to in-memory buffer
    db_path = bbs_engine.cfg.db_path
    log_lines = _read_activity_log(str(db_path), 2000)
    if not log_lines:
        log_lines = bbs_engine.recent_log_lines(500)

    # Send current state snapshot to the newly-joined sysop
    emit("admin_dashboard_init", {
        "users": bbs_engine.connected_users_snapshot(),
        "plugins": bbs_engine.plugin_stats_snapshot(),
        "log": log_lines,
        "bbs_callsign": bbs_engine.cfg.full_callsign,
    })


@socketio.on("leave_admin")
def on_leave_admin(_data):
    leave_room(_SYSOP_ROOM)



def start_bridge() -> None:
    """
    Start the background thread that consumes bbs_engine.event_queue and
    emits SocketIO events to the sysop room.
    Called once by web_interface.py after the engine reference is available.
    """
    global _bridge_thread
    if _bridge_thread and _bridge_thread.is_alive():
        return
    _bridge_thread = threading.Thread(
        target=_bridge_loop, name="bbs-event-bridge", daemon=True
    )
    _bridge_thread.start()
    logger.info("BBS→SocketIO bridge thread started")


def _bridge_loop() -> None:
    """Drain the engine event queue, emit to sysop room, and persist log lines."""
    import queue as stdlib_queue

    from server.app import bbs_engine

    while True:
        if bbs_engine is None:
            time.sleep(0.5)
            continue
        try:
            event: dict = bbs_engine.event_queue.get(timeout=1.0)
        except stdlib_queue.Empty:
            continue
        except Exception:
            time.sleep(0.1)
            continue

        etype = event.get("type")
        if etype == "log":
            safe_emit("bbs_log_line", {"line": event["line"]}, room=_SYSOP_ROOM)
            _persist_log_line(str(bbs_engine.cfg.db_path), event["line"])
        elif etype == "user_connected":
            safe_emit("user_connected", event, room=_SYSOP_ROOM)
            # Also push updated full snapshot
            safe_emit(
                "users_snapshot",
                bbs_engine.connected_users_snapshot(),
                room=_SYSOP_ROOM,
            )
        elif etype == "user_disconnected":
            safe_emit("user_disconnected", event, room=_SYSOP_ROOM)
            safe_emit(
                "users_snapshot",
                bbs_engine.connected_users_snapshot(),
                room=_SYSOP_ROOM,
            )
        elif etype == "plugin_stats":
            safe_emit(
                "plugin_stats_update",
                bbs_engine.plugin_stats_snapshot(),
                room=_SYSOP_ROOM,
            )


# ── Web terminal handlers ─────────────────────────────────────────────────────

@socketio.on("web_terminal_connect")
def on_web_terminal_connect(_data):
    """
    Sysop requests a new BBS session in the browser terminal.
    Creates a synthetic Connection wired via queues, starts the BBS session
    as an asyncio task, and launches a drain thread to push output to xterm.js.
    """
    from flask import session as flask_session
    if not flask_session.get("sysop"):
        emit("web_terminal_error", {"message": "Not authorized"})
        return

    sid = request.sid

    from server.app import bbs_engine
    if bbs_engine is None:
        emit("web_terminal_error", {"message": "BBS engine not running"})
        return

    if sid in _web_sessions:
        emit("web_terminal_error", {"message": "Terminal session already active"})
        return

    output_queue: stdlib_queue.Queue = stdlib_queue.Queue()  # unbounded

    try:
        reader = bbs_engine.start_web_session(sid, output_queue)
    except Exception as exc:
        logger.exception("Failed to start web terminal session for %s", sid)
        emit("web_terminal_error", {"message": f"Failed to start session: {exc}"})
        return

    drain_thread = threading.Thread(
        target=_drain_web_output,
        args=(sid, output_queue),
        daemon=True,
        name=f"web-drain:{sid[:8]}",
    )
    drain_thread.start()

    _web_sessions[sid] = {
        "reader": reader,
        "output_queue": output_queue,
        "drain_thread": drain_thread,
    }
    emit("web_terminal_ready", {})


@socketio.on("web_terminal_input")
def on_web_terminal_input(data):
    """Forward browser keystrokes into the BBS session's asyncio StreamReader."""
    sid = request.sid
    state = _web_sessions.get(sid)
    if not state:
        return

    raw = data.get("data", "")
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    if not raw:
        return

    from server.app import bbs_engine
    if bbs_engine is not None:
        bbs_engine.feed_web_input(state["reader"], raw)


@socketio.on("web_terminal_resize")
def on_web_terminal_resize(data):
    """Update terminal dimensions when the browser window is resized."""
    sid = request.sid
    from server.app import bbs_engine
    if bbs_engine is None:
        return
    try:
        cols = max(10, int(data.get("cols", 80)))
        rows = max(4, int(data.get("rows", 24)))
    except (TypeError, ValueError):
        return
    bbs_engine.resize_web_session(sid, cols, rows)


@socketio.on("web_terminal_disconnect")
def on_web_terminal_disconnect(_data):
    """Sysop closed the terminal tab / clicked Disconnect."""
    _cleanup_web_session(request.sid)


# ── Web terminal helpers ──────────────────────────────────────────────────────

def _cleanup_web_session(sid: str) -> None:
    """
    Close the BBS session for *sid* if one exists.
    Safe to call even if no web session is active for that sid.
    """
    state = _web_sessions.pop(sid, None)
    if not state:
        return
    from server.app import bbs_engine
    if bbs_engine is not None:
        bbs_engine.close_web_input(state["reader"])


def _drain_web_output(
    sid: str,
    output_queue: "stdlib_queue.Queue[bytes | None]",
) -> None:
    """
    Background thread: drain BBS output from *output_queue* and emit
    'web_terminal_output' Socket.IO events to the specific browser client.
    Exits when a None sentinel is received (session ended).
    """
    while True:
        try:
            data = output_queue.get(timeout=1.0)
        except stdlib_queue.Empty:
            continue

        if data is None:
            # Session ended — notify browser and exit thread.
            safe_emit("web_terminal_closed", {}, room=sid)
            _web_sessions.pop(sid, None)
            break

        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("latin-1", errors="replace")
        safe_emit("web_terminal_output", {"data": text}, room=sid)

