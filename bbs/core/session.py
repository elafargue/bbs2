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

from bbs.ax25.address import callsign_only
from bbs.core.auth import AuthLevel, AuthService, AuthState
from bbs.core.terminal import Terminal
from bbs.db.connections import upsert_connection
from bbs.db import users as user_db

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

        # Per-session scratch space for plugins (keyed by plugin name)
        self.plugin_state: dict = {}

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
        async with aiosqlite.connect(db_path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            self.db = db

            _ax25_transports = ("kernel_ax25", "kiss_tcp", "kiss_serial", "agwpe")
            _is_ax25 = self.conn.transport_id in _ax25_transports
            _is_web = self.conn.transport_id == "web"
            self.term = await Terminal.create(
                self.conn.reader,
                self.conn.writer,
                echo=not _is_ax25,
                must_echo=_is_web,
                # AX.25 TNCs typically run with LFADD ON, so sending \r\n
                # produces a double newline.  Send \r only and let the TNC
                # or terminal emulator supply the LF.
                eol="\r" if _is_ax25 else "\r\n",
                # Web sessions are served to xterm.js which always supports
                # truecolor; start with a good default (the user can change
                # it with CO, and _apply_user_preferences will honour their
                # saved setting after login).
                color_mode="truecolor" if _is_web else "off",
            )

            try:
                await self._greet()
                await self._identify()
                # Record the connection as live as soon as the callsign is known.
                if self.auth.callsign and self.cfg.connection_log_days != 0:
                    try:
                        await upsert_connection(
                            str(self.cfg.db_path),
                            callsign=self.auth.callsign,
                            transport=self.conn.transport_id,
                            connected_at=self.connected_at,
                            auth_level=self.auth.level.value,
                            connected=1,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to record connect for %s", self.auth.callsign
                        )
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
        await self.term.sendln(
            f"{self.term.label('Sysop:')} {self.cfg.sysop}  {self.term.label('QTH:')} {self.cfg.location}"
        )
        await self.term.sendln(self.term.field("BBS:", self.cfg.full_callsign, "meta"))
        await self.term.sendln()

    # ── Identification ────────────────────────────────────────────────────────

    async def _identify(self) -> None:
        """
        Determine the user's callsign.

        AX.25 / KISS transports: callsign is in remote_addr ("W1AW-3") —
        extract and trust it immediately.

        TCP transport: no callsign embedded; ask the user.
        """
        if self.conn.transport_id in ("kernel_ax25", "kiss_tcp", "kiss_serial", "agwpe"):
            # Callsign comes from connection layer — already verified by kernel/TNC.
            # Strip the SSID: user accounts are keyed on the base callsign so that
            # the same operator connecting via -7 or -3 gets the same record.
            display_call = self.remote_addr.upper().strip()
            try:
                base_call = callsign_only(display_call)
            except ValueError:
                base_call = display_call
            self.auth, created = await self.auth_service.identify(
                self.db, base_call, from_ax25=True
            )
            await self._apply_user_preferences()
            await self.term.sendln(
                f"{self.term.label('Welcome,', 'success')} {self.term.style(display_call, 'accent', bold=True)}!"
            )
            if created:
                await self.term.sendln(
                    self.term.note("(New account created — sysop approval pending for write access)")
                )
        elif self.conn.transport_id == "web":
            # Web terminal is sysop-only; identify automatically as the BBS
            # sysop callsign so the session is always tracked in connection_log.
            base_call = self.cfg.callsign.upper().strip()
            self.auth, created = await self.auth_service.identify(
                self.db, base_call, from_ax25=False
            )
            await self._apply_user_preferences()
            await self.term.sendln(
                f"{self.term.label('Sysop web terminal —', 'meta')} "
                f"{self.term.style(base_call, 'accent', bold=True)}"
            )
        else:
            # TCP / unknown — ask for callsign
            await self.term.send("Callsign: ")
            raw_call = (await self.term.readline(max_len=10)).upper().strip()
            if not raw_call:
                await self.term.sendln("No callsign entered. Goodbye.")
                self.state = SessionState.DISCONNECTED
                return
            # Strip SSID for consistency with radio paths
            try:
                base_call = callsign_only(raw_call)
            except ValueError:
                base_call = raw_call
            self.auth, created = await self.auth_service.identify(
                self.db, base_call, from_ax25=False
            )
            await self._apply_user_preferences()
            await self.term.sendln(
                f"{self.term.label('Welcome,', 'success')} {self.term.style(base_call, 'accent', bold=True)}!"
            )
            if created:
                await self.term.sendln(
                    self.term.note("(New account — sysop approval pending for write access)")
                )

        level_label = self.auth_service.level_label(self.auth.level)
        await self.term.sendln(
            self.term.field("Access level:", level_label, "meta")
        )
        await self.term.sendln()

    # ── Main menu loop ────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        idle_timeout = self.cfg.idle_timeout or None

        while self.state == SessionState.ACTIVE:
            # Check idle timeout
            if idle_timeout and self.idle_seconds > idle_timeout:
                await self.term.sendln(self.term.warn("Idle timeout — disconnecting."))
                break

            # Build menu from loaded plugins
            menu_items = self.plugin_registry.menu_items(self.auth.level)
            menu_items += [
                ("CO", "Color"),
                ("A", "Auth"),
                ("B", "Bye (disconnect)"),
                ("?", "Help"),
            ]

            await self.term.send_menu(self.cfg.name, menu_items, prompt="> ")
            choice_raw = await self.term.readline(
                max_len=8, timeout=idle_timeout
            )
            if not choice_raw:
                # Timeout or EOF
                await self.term.sendln(self.term.warn("Idle timeout — disconnecting."))
                break

            self.touch()
            choice = choice_raw.strip().upper()

            if choice in ("B", "BYE"):
                break
            elif choice in ("CO", "COLOR"):
                await self._handle_color()
            elif choice == "A":
                await self._handle_auth()
            elif choice == "?":
                await self._show_help()
            else:
                plugin = self.plugin_registry.get_by_key(choice)
                if plugin:
                    await plugin.handle_session(self)
                else:
                    await self.term.sendln(self.term.warn("Unknown command."))

    # ── Auth command ─────────────────────────────────────────────────────────

    async def _handle_auth(self) -> None:
        if self.auth.is_authenticated:
            await self.term.sendln(
                self.term.note(f"Already authenticated as {self.auth.callsign}.")
            )
            return

        prompt = await self.auth_service.otp_prompt(self.db, self.auth)
        await self.term.send(prompt)
        code = await self.term.readline(max_len=8, echo=False)
        if not code or not code.strip():
            await self.term.sendln(self.term.note("Auth cancelled."))
            return

        success, msg = await self.auth_service.verify_otp(
            self.db, self.auth, code
        )
        await self.term.sendln(msg)
        if success:
            level_label = self.auth_service.level_label(self.auth.level)
            await self.term.sendln(
                f"{self.term.ok('Access upgraded to:')} {level_label}"
            )

    async def _handle_color(self) -> None:
        await self.term.sendln()
        await self.term.sendln(self.term.label("Color output", "meta"))
        await self.term.sendln(self.term.note("------------"))
        await self.term.sendln(
            self.term.field(
                "Current mode:",
                self._describe_color_mode(self.term.color_mode.value),
                "meta",
            )
        )
        await self.term.sendln(f"{self.term.label('O')} - Off")
        await self.term.sendln(f"{self.term.label('A')} - ANSI 16-color")
        await self.term.sendln(f"{self.term.label('T')} - 24-bit truecolor")
        await self.term.send(self.term.prompt("Selection (ENTER cancels): "))
        choice = (await self.term.readline(max_len=1)).strip().upper()

        selected_mode = {
            "O": "off",
            "A": "ansi16",
            "T": "truecolor",
        }.get(choice)

        if not choice:
            await self.term.sendln(self.term.note("Color mode unchanged."))
            return
        if selected_mode is None:
            await self.term.sendln(self.term.warn("Unknown color selection."))
            return

        self.term.set_color_mode(selected_mode)
        if self.auth.user_id is not None:
            await user_db.set_color_mode(self.db, self.auth.user_id, selected_mode)
        await self.term.sendln(
            f"{self.term.ok('Color mode saved:')} {self._describe_color_mode(selected_mode)}."
        )

    # ── Help ─────────────────────────────────────────────────────────────────

    async def _show_help(self) -> None:
        lines = [
            "BBS HELP",
            "--------",
            "Select a menu item by typing its letter and pressing ENTER.",
            "",
            "A  - Authenticate: prove your callsign with an OTP code.",
            "     Required for sysop auth or authenticated messages.",
            "     Your secret is set out-of-band by the sysop, reach out to them",
            "     to request it.",
            "",
            "CO - Color output mode: Off, ANSI 16-color, or truecolor.",
            "",
            "B  - Bye / disconnect.",

        ]
        await self.term.paginate(lines)

    async def _apply_user_preferences(self) -> None:
        if self.auth.user_id is None:
            return
        user = await user_db.get_by_id(self.db, self.auth.user_id)
        if user is None:
            return
        self.term.set_color_mode(user.color_mode)

    @staticmethod
    def _describe_color_mode(color_mode: str) -> str:
        return {
            "off": "Off",
            "ansi16": "ANSI 16-color",
            "truecolor": "24-bit truecolor",
        }.get(color_mode, "Off")

    # ── Farewell ──────────────────────────────────────────────────────────────

    async def _farewell(self) -> None:
        try:
            await self.term.sendln()
            await self.term.sendln(
                f"{self.term.label('73 de', 'meta')} {self.cfg.callsign}  {self.term.note('-- disconnecting --')}"
            )
            await self.term.flush()
        except Exception:
            pass
        await self.conn.close()
