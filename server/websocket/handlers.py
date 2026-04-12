"""
server/websocket/handlers.py — SocketIO event handlers and the asyncio→Flask bridge.

Per flask-vue-scaffold-conventions:
  - All handlers defined here with @socketio.on(...)
  - Registered by importing this module in web_interface.py
  - safe_emit() wraps all outbound emits

The asyncio→Flask bridge is a daemon thread that drains bbs_engine.event_queue
and emits SocketIO events to connected sysop clients.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import request
from flask_socketio import emit, join_room, leave_room

from server.app import app, socketio

logger = logging.getLogger(__name__)

_SYSOP_ROOM = "sysop"
_bridge_thread: threading.Thread | None = None


def safe_emit(event: str, data: Any, room: str | None = None) -> None:
    """Emit a SocketIO event, swallowing errors from disconnected clients."""
    try:
        if room:
            socketio.emit(event, data, to=room)
        else:
            socketio.emit(event, data)
    except Exception:
        pass


# ── Connection events ─────────────────────────────────────────────────────────

@socketio.on("connect")
def on_ws_connect():
    logger.debug("WebSocket client connected: %s", request.sid)


@socketio.on("disconnect")
def on_ws_disconnect():
    logger.debug("WebSocket client disconnected: %s", request.sid)


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

    # Send current state snapshot to the newly-joined sysop
    emit("admin_dashboard_init", {
        "users": bbs_engine.connected_users_snapshot(),
        "plugins": bbs_engine.plugin_stats_snapshot(),
        "log": bbs_engine.recent_log_lines(100),
        "bbs_callsign": bbs_engine.cfg.full_callsign,
    })


@socketio.on("leave_admin")
def on_leave_admin(_data):
    leave_room(_SYSOP_ROOM)


# ── Asyncio → SocketIO bridge ─────────────────────────────────────────────────

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
    """Drain the engine event queue and emit to sysop room."""
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
