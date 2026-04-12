"""
bbs/core/session.py — Per-connection session state and lifecycle.

A BBSSession is created for every accepted connection regardless of transport.
It wires together:
  - The Connection (asyncio reader/writer + remote address)
  - The Terminal renderer
  - The AuthState (managed by AuthService)
  - An open aiosqlite database connection (one per session)
  - Idle timeout tracking

The session lifecycle is:
  CONNECTED → [identify callsign] → ACTIVE → DISCONNECTED

The engine calls session.run() as an asyncio task; it handles the greeting,
identification, main-menu loop, and teardown.
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

import aiosqlite

from bbs.core.auth import AuthLevel, AuthService, AuthState
from bbs.core.terminal import Terminal

if TYPE_CHECKING:
    from bbs.config import BBSConfig
    from bbs.core.plugin_registry import PluginRegistry
    from bbs.transport.base import Connection

logger = logging.getLogger(__name__)


class SessionState(Enum):
    CONNECTED = auto()
    ACTIVE = auto()
    DISCONNECTED = auto()


class BBSSession:
    """
    Represents one live user session.

    Plugins receive a BBSSession and interact with the user through:
      session.term   — Terminal (send text, read input)
      session.auth   — AuthState (check level, callsign)
      session.db     — open aiosqlite.Connection for DB operations
    """

    def __init__(
        self,
        conn: "Connection",
        cfg: "BBSConfig",
        auth_service: AuthService,
        plugin_registry: "PluginRegistry",
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.auth_service = auth_service
        self.plugin_registry = plugin_registry

        self.term: Terminal  # set in run() after ANSI detection
        self.auth = AuthState()
        self.db: aiosqlite.Connection  # opened in run()

        self.state = SessionState.CONNECTED
        self.connected_at = time.time()
        self._last_activity = time.time()

        # Unique session ID for web dashboard / logs
        self.session_id = f"{conn.transport_id}:{conn.remote_addr}:{int(self.connected_at)}"

    @property
    def remote_addr(self) -> str:
        return self.conn.remote_addr

    @property
    def idle_seconds(self) -> float:
        return time.time() - self._last_activity

    def touch(self) -> None:
        self._last_activity = time.time()

    # ── Main session coroutine ────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Full session lifecycle.  Called by the engine as an asyncio Task.
        """
        db_path = str(self.cfg.db_path)
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            self.db = db

            self.term = await Terminal.create(
                self.conn.reader,
                self.conn.writer,
            )

            try:
                await self._greet()
                await self._identify()
                if self.state != SessionState.DISCONNECTED:
                    self.state = SessionState.ACTIVE
                    await self._main_loop()
            except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                pass
            except Exception:
                logger.exception("Unhandled error in session %s", self.session_id)
            finally:
                await self._farewell()
                self.state = SessionState.DISCONNECTED

    # ── Greeting ─────────────────────────────────────────────────────────────

    async def _greet(self) -> None:
        await self.term.sendln()
        await self.term.send_header(f" {self.cfg.name} ")
        await self.term.sendln(f"Sysop: {self.cfg.sysop}  QTH: {self.cfg.location}")
        await self.term.sendln(f"BBS: {self.cfg.full_callsign}")
        await self.term.sendln()

    # ── Identification ────────────────────────────────────────────────────────

    async def _identify(self) -> None:
        """
        Determine the user's callsign.

        AX.25 / KISS transports: callsign is in remote_addr ("W1AW-3") —
        extract and trust it immediately.

        TCP transport: no callsign embedded; ask the user.
        """
        from bbs.ax25.address import callsign_only

        if self.conn.transport_id in ("kernel_ax25", "kiss_tcp", "kiss_serial", "netrom"):
            # Callsign comes from connection layer — already verified by kernel/TNC
            callsign = callsign_only(self.remote_addr)
            self.auth, created = await self.auth_service.identify(
                self.db, callsign, from_ax25=True
            )
            await self.term.sendln(f"Welcome, {callsign}!")
            if created:
                await self.term.sendln("(New account created — sysop approval pending for write access)")
        else:
            # TCP / unknown — ask for callsign
            await self.term.send("Callsign: ")
            callsign = (await self.term.readline(max_len=10)).upper().strip()
            if not callsign:
                await self.term.sendln("No callsign entered. Goodbye.")
                self.state = SessionState.DISCONNECTED
                return
            self.auth, created = await self.auth_service.identify(
                self.db, callsign, from_ax25=False
            )
            await self.term.sendln(f"Welcome, {callsign}!")
            if created:
                await self.term.sendln("(New account — sysop approval pending for write access)")

        level_label = self.auth_service.level_label(self.auth.level)
        await self.term.sendln(f"Access level: {level_label}")
        await self.term.sendln()

    # ── Main menu loop ────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        idle_timeout = self.cfg.idle_timeout or None

        while self.state == SessionState.ACTIVE:
            # Check idle timeout
            if idle_timeout and self.idle_seconds > idle_timeout:
                await self.term.sendln("Idle timeout — disconnecting.")
                break

            # Build menu from loaded plugins
            menu_items = self.plugin_registry.menu_items(self.auth.level)
            menu_items += [
                ("A", "Auth (upgrade access)"),
                ("Q", "Quit"),
                ("?", "Help"),
            ]

            await self.term.send_menu(self.cfg.name, menu_items, prompt="> ")
            choice_raw = await self.term.readline(
                max_len=4, echo=True, timeout=idle_timeout
            )
            if not choice_raw:
                # Timeout or EOF
                await self.term.sendln("Idle timeout — disconnecting.")
                break

            self.touch()
            choice = choice_raw.strip().upper()

            if choice == "Q":
                break
            elif choice == "A":
                await self._handle_auth()
            elif choice == "?":
                await self._show_help()
            else:
                plugin = self.plugin_registry.get_by_key(choice)
                if plugin:
                    await plugin.handle_session(self)
                else:
                    await self.term.sendln("Unknown command.")

    # ── Auth command ─────────────────────────────────────────────────────────

    async def _handle_auth(self) -> None:
        if self.auth.is_authenticated:
            await self.term.sendln(f"Already authenticated as {self.auth.callsign}.")
            return

        prompt = await self.auth_service.otp_prompt(self.db, self.auth)
        await self.term.send(prompt)
        code = await self.term.readline(max_len=8, echo=False)
        if not code or not code.strip():
            await self.term.sendln("Auth cancelled.")
            return

        success, msg = await self.auth_service.verify_otp(
            self.db, self.auth, code
        )
        await self.term.sendln(msg)
        if success:
            level_label = self.auth_service.level_label(self.auth.level)
            await self.term.sendln(f"Access upgraded to: {level_label}")

    # ── Help ─────────────────────────────────────────────────────────────────

    async def _show_help(self) -> None:
        lines = [
            "BBS HELP",
            "--------",
            "Select a menu item by typing its letter and pressing ENTER.",
            "",
            "A  - Authenticate: prove your callsign with HMAC challenge/response.",
            "     Required for posting messages and other write operations.",
            "     Your secret is set out-of-band by the sysop.",
            "",
            "Q  - Quit / disconnect.",
            "",
            "On AX.25 connections your callsign is identified automatically.",
            "On Telnet/TCP connections you must type your callsign at login.",
        ]
        await self.term.paginate(lines)

    # ── Farewell ──────────────────────────────────────────────────────────────

    async def _farewell(self) -> None:
        try:
            await self.term.sendln()
            await self.term.sendln(f"73 de {self.cfg.callsign}  -- disconnecting --")
            await self.term.flush()
        except Exception:
            pass
        await self.conn.close()
