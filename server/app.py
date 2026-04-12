"""
server/app.py — Central Flask + Flask-SocketIO hub.

Per flask-vue-scaffold-conventions:
  - Flask and SocketIO instances live here.
  - All other modules import app and socketio from here.
  - async_mode is always "threading".
  - Shared state lives as module-level variables here.
"""
from __future__ import annotations

import os
from collections import deque
from typing import Any, Optional, TYPE_CHECKING

from flask import Flask
from flask_socketio import SocketIO

if TYPE_CHECKING:
    from bbs.core.engine import BBSEngine

app = Flask(__name__, static_folder="../static", static_url_path="/")
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path: str):
    """Serve the Vue SPA for all non-API routes."""
    from flask import send_from_directory
    import os
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    full = os.path.join(static_dir, path)
    if path and os.path.isfile(full):
        return send_from_directory(static_dir, path)
    return send_from_directory(static_dir, "index.html")

# ── Shared state ──────────────────────────────────────────────────────────────

# Reference to the running BBS engine (set by main.py after engine starts)
bbs_engine: Optional["BBSEngine"] = None

# Cached snapshots updated by the bridge thread
connected_users: list[dict[str, Any]] = []
plugin_stats: list[dict[str, Any]] = []

# Recent log lines ring buffer (bounded; initial load for new web clients)
activity_log: deque[str] = deque(maxlen=500)
