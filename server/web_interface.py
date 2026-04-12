"""
server/web_interface.py — Flask app wiring: imports routes and SocketIO handlers
as side-effects (per flask-vue-scaffold-conventions).

Also exposes start_web() which is called by main.py.
"""
from __future__ import annotations

import logging

# Side-effect imports register routes and SocketIO handlers
import server.routes.auth        # noqa: F401
import server.routes.users       # noqa: F401
import server.routes.plugins     # noqa: F401
import server.routes.activity    # noqa: F401
import server.routes.bulletins   # noqa: F401
import server.websocket.handlers # noqa: F401

from server.app import app, socketio
from server.websocket.handlers import start_bridge

logger = logging.getLogger(__name__)


def configure_app(secret_key: str) -> None:
    """Apply configuration that must happen before the first request."""
    app.config["SECRET_KEY"] = secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def start_web(host: str, port: int, secret_key: str, debug: bool = False) -> None:
    """
    Configure and start Flask-SocketIO in threading async_mode.
    This call BLOCKS — run it in a daemon thread from main.py.
    """
    configure_app(secret_key)
    start_bridge()
    logger.info("Web interface starting on %s:%d", host, port)
    socketio.run(
        app,
        host=host,
        port=port,
        debug=debug,
        use_reloader=False,
        log_output=False,
    )
