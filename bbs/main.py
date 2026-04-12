"""
bbs/main.py — BBS2 entry point.

Starts:
  1. asyncio event loop running the BBS engine (main thread)
  2. Flask-SocketIO web interface (daemon thread)

Usage:
  bbs2 [--config path/to/bbs.yaml] [--debug] [--set-sysop-password]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BBS2 — Ham Radio BBS for Linux AX.25"
    )
    parser.add_argument(
        "--config",
        default="config/bbs.yaml",
        help="Path to bbs.yaml config file (default: config/bbs.yaml)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--set-sysop-password",
        action="store_true",
        help="Interactively set the sysop web password and update bbs.yaml",
    )
    args = parser.parse_args()

    # ── Logging setup ─────────────────────────────────────────────────────────
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy third-party loggers unless in debug mode
    if not args.debug:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        logging.getLogger("engineio").setLevel(logging.WARNING)
        logging.getLogger("socketio").setLevel(logging.WARNING)

    logger = logging.getLogger("bbs2")

    # ── Config ────────────────────────────────────────────────────────────────
    from bbs.config import load_config
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print(
            f"Copy config/bbs.yaml.example to {args.config} and edit it.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Set sysop password mode ───────────────────────────────────────────────
    if args.set_sysop_password:
        _set_sysop_password(args.config)
        return

    # ── Web secret key validation ─────────────────────────────────────────────
    try:
        secret_key = cfg.web_secret_key
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── BBS Engine ────────────────────────────────────────────────────────────
    from bbs.core.engine import BBSEngine
    engine = BBSEngine(cfg)

    # Make engine visible to web interface
    import server.app as server_app
    server_app.bbs_engine = engine

    # ── Web interface (daemon thread) ─────────────────────────────────────────
    from server.web_interface import start_web

    web_thread = threading.Thread(
        target=start_web,
        kwargs={
            "host": cfg.web_host,
            "port": cfg.web_port,
            "secret_key": secret_key,
            "debug": args.debug,
        },
        name="web-interface",
        daemon=True,
    )
    web_thread.start()
    logger.info(
        "Web interface starting on http://%s:%d", cfg.web_host, cfg.web_port
    )

    # ── Run BBS engine in main thread ─────────────────────────────────────────
    logger.info("Starting BBS engine for %s", cfg.full_callsign)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
        engine.stop()


def _set_sysop_password(config_path: str) -> None:
    """Prompt for a new sysop password, hash it, and write it to the config."""
    import getpass
    import re

    import bcrypt
    import yaml

    pw = getpass.getpass("New sysop password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw != pw2:
        print("Passwords do not match.")
        sys.exit(1)
    if len(pw) < 12:
        print("Password must be at least 12 characters.")
        sys.exit(1)

    hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

    with open(config_path) as fh:
        raw = yaml.safe_load(fh) or {}

    raw.setdefault("web", {})["sysop_password_hash"] = hashed

    with open(config_path, "w") as fh:
        yaml.dump(raw, fh, default_flow_style=False)

    print(f"Sysop password updated in {config_path}")


if __name__ == "__main__":
    main()
