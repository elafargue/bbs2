"""
bbs/plugins/chat/chat.py — Multi-room chat plugin.

Design
------
All active chat sessions share ChatRoom objects kept in module-level state.
A room is an asyncio broadcast: each session has an asyncio.Queue; when
someone sends a message the ChatRoom puts it on every other queue.

Chat is intentionally minimal for 1200 bps:
  - Messages are short (max 160 chars).
  - Lines are printed one at a time; no full-screen updates.
  - /WHO, /MSG, /JOIN, /ROOMS, /QUIT commands.

Access: IDENTIFIED (just a valid callsign via AX.25 is enough to chat).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable, Optional, TYPE_CHECKING

import aiosqlite

from bbs.core.plugin_registry import BBSPlugin

if TYPE_CHECKING:
    from bbs.core.session import BBSSession

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 160
HISTORY_LINES = 20  # overridden by config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    room  TEXT    NOT NULL,
    ts    INTEGER NOT NULL,
    line  TEXT    NOT NULL
);
"""


class ChatRoom:
    """In-memory broadcast room."""

    def __init__(self, name: str, description: str) -> None:
        self.name = name.lower()
        self.description = description
        # callsign → asyncio.Queue[str]
        self._members: dict[str, asyncio.Queue[str]] = {}
        self._history: list[str] = []
        self._history_size = HISTORY_LINES
        # Optional async callback: (line: str) -> Awaitable[None]
        # Set by ChatPlugin after initialize(); fires on every broadcast.
        self._persist_cb: Optional[Callable[[str], Awaitable[None]]] = None

    def join(self, callsign: str) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._members[callsign.upper()] = q
        self._broadcast(f"*** {callsign} joined {self.name} ***", exclude=callsign)
        return q

    def leave(self, callsign: str) -> None:
        self._members.pop(callsign.upper(), None)
        self._broadcast(f"*** {callsign} left {self.name} ***", exclude=callsign)

    def broadcast(self, callsign: str, text: str) -> None:
        ts = time.strftime("%H:%M")
        line = f"[{ts}] {callsign}: {text}"
        self._broadcast(line, exclude=None)

    def private_msg(self, from_call: str, to_call: str, text: str) -> bool:
        """Send a private message.  Returns False if recipient not in room."""
        q = self._members.get(to_call.upper())
        if not q:
            return False
        ts = time.strftime("%H:%M")
        line = f"[{ts}] *{from_call}→{to_call}*: {text}"
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            pass
        return True

    def who(self) -> list[str]:
        return sorted(self._members.keys())

    def get_history(self) -> list[str]:
        return list(self._history)

    def _broadcast(self, line: str, exclude: Optional[str]) -> None:
        self._history.append(line)
        if len(self._history) > self._history_size:
            self._history.pop(0)
        # Persist asynchronously if a callback is registered.
        if self._persist_cb is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._persist_cb(line))
            except RuntimeError:
                pass  # no running loop (e.g. during tests that don't use the plugin)
        for call, q in self._members.items():
            if exclude and call == exclude.upper():
                continue
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    @property
    def member_count(self) -> int:
        return len(self._members)


# Module-level room registry — shared across all sessions
_rooms: dict[str, ChatRoom] = {}


def get_or_create_room(name: str, description: str = "") -> ChatRoom:
    key = name.lower()
    if key not in _rooms:
        _rooms[key] = ChatRoom(name, description)
    return _rooms[key]


# ── Plugin class ──────────────────────────────────────────────────────────────

class ChatPlugin(BBSPlugin):
    name = "chat"
    display_name = "Chat"
    menu_key = "C"
    min_auth_level_name = "IDENTIFIED"

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)
        global HISTORY_LINES
        HISTORY_LINES = cfg.get("history_lines", 20)

        default_rooms = cfg.get("default_rooms", [{"name": "main", "description": "Main chat room"}])
        for room_cfg in default_rooms:
            get_or_create_room(room_cfg["name"], room_cfg.get("description", ""))

        # Create schema and restore persisted history into each room.
        async with aiosqlite.connect(db_path, timeout=30) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
            for room in _rooms.values():
                room._history_size = HISTORY_LINES
                async with db.execute(
                    """
                    SELECT line FROM (
                        SELECT id, line FROM chat_history WHERE room=?
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (room.name, HISTORY_LINES),
                ) as cur:
                    rows = await cur.fetchall()
                room._history = [r[0] for r in rows]
                room._persist_cb = self._make_persist_cb(room.name)

    def _make_persist_cb(self, room_name: str) -> Callable[[str], Awaitable[None]]:
        """Return an async callable that persists one chat line for *room_name*."""
        async def _persist(line: str) -> None:
            try:
                async with aiosqlite.connect(self._db_path, timeout=30) as db:
                    await db.execute(
                        "INSERT INTO chat_history (room, ts, line) VALUES (?, ?, ?)",
                        (room_name, int(time.time()), line),
                    )
                    # Trim to the configured limit.
                    await db.execute(
                        """
                        DELETE FROM chat_history
                        WHERE room = ? AND id NOT IN (
                            SELECT id FROM chat_history
                            WHERE room = ?
                            ORDER BY id DESC
                            LIMIT ?
                        )
                        """,
                        (room_name, room_name, HISTORY_LINES),
                    )
                    await db.commit()
            except Exception:
                logger.exception("chat: failed to persist message for room %s", room_name)
        return _persist

    async def _delete_message(
        self, room_name: str, msg_id: int
    ) -> Optional[str]:
        """Delete a message from DB and in-memory history.  Returns the deleted
        line text, or None if the ID was not found in that room."""
        if not self._db_path:
            return None
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            async with db.execute(
                "SELECT line FROM chat_history WHERE id=? AND room=?",
                (msg_id, room_name),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            line_text: str = row[0]
            await db.execute("DELETE FROM chat_history WHERE id=?", (msg_id,))
            await db.commit()
        room = _rooms.get(room_name)
        if room and line_text in room._history:
            room._history.remove(line_text)
        return line_text

    async def _delete_room(self, room_name: str) -> bool:
        """Delete a chat room — removes all DB history and the in-memory room.
        Returns True if the room existed."""
        if not self._db_path:
            return False
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            await db.execute("DELETE FROM chat_history WHERE room=?", (room_name,))
            await db.commit()
        room = _rooms.pop(room_name, None)
        if room is None:
            return False
        room._broadcast(
            f"*** Room {room_name} has been deleted by the sysop. Use /JOIN to switch rooms. ***",
            exclude=None,
        )
        return True

    async def handle_session(self, session: "BBSSession") -> None:
        term = session.term
        callsign = session.auth.callsign
        is_sysop = session.auth.is_sysop

        # Join default room
        default_room = next(iter(_rooms.values())) if _rooms else get_or_create_room("main")
        current_room = default_room
        inbox = current_room.join(callsign)

        await term.sendln(
            f"{term.label('Entered chat room:', 'meta')} {term.style(current_room.name, 'accent', bold=True)}"
        )
        await term.sendln(term.field("Users here:", ", ".join(current_room.who()), "meta"))
        # Show recent history — sysop sees message IDs so they can /DEL them
        if self._db_path:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                async with db.execute(
                    """
                    SELECT id, line FROM (
                        SELECT id, line FROM chat_history WHERE room=?
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (current_room.name, HISTORY_LINES),
                ) as cur:
                    db_history = await cur.fetchall()
        else:
            db_history = []
        plain_history = current_room.get_history()
        if db_history or plain_history:
            await term.sendln(term.note("--- recent ---"))
            if db_history:
                for row_id, line in db_history:
                    if is_sysop:
                        await term.sendln(f"{term.note(f'[{row_id}]')} {line}")
                    else:
                        await term.sendln(line)
            else:
                for line in plain_history[-10:]:
                    await term.sendln(line)
            await term.sendln(term.note("--- end ---"))
        cmds = "/WHO  /MSG <call> <text>  /JOIN <room>  /ROOMS /QUIT"
        if is_sysop:
            cmds += "  /HIST  /DEL <id>  /DELROOM <room>"
        await term.sendln(f"{term.label('Commands:', 'meta')} {cmds}")
        await term.sendln()

        try:
            await self._chat_loop(session, current_room, inbox, callsign, is_sysop)
        finally:
            current_room.leave(callsign)

    async def _chat_loop(
        self,
        session: "BBSSession",
        room: ChatRoom,
        inbox: asyncio.Queue[str],
        callsign: str,
        is_sysop: bool = False,
    ) -> None:
        term = session.term

        async def _reader() -> None:
            """Forward incoming chat lines to the terminal."""
            while True:
                try:
                    line = await asyncio.wait_for(inbox.get(), timeout=0.5)
                    await term.sendln(line)
                    await term.send(term.prompt(f"{room.name}> "))
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break

        reader_task = asyncio.create_task(_reader())

        try:
            while True:
                await term.send(term.prompt(f"{room.name}> "))
                line = await term.readline(max_len=MAX_MSG_LEN, echo=False)
                session.touch()

                if not line:
                    continue

                if line.startswith("/"):
                    cmd_parts = line.split(None, 2)
                    cmd = cmd_parts[0].upper()

                    if cmd == "/QUIT":
                        break
                    elif cmd == "/WHO":
                        members = room.who()
                        await term.sendln(
                            term.field(f"Users in {room.name}:", ", ".join(members), "meta")
                        )
                    elif cmd == "/MSG":
                        if len(cmd_parts) < 3:
                            await term.sendln(term.warn("Usage: /MSG <callsign> <message>"))
                        else:
                            dest = cmd_parts[1].upper()
                            text = cmd_parts[2]
                            if not room.private_msg(callsign, dest, text):
                                await term.sendln(term.warn(f"{dest} is not in this room."))
                    elif cmd == "/JOIN":
                        if len(cmd_parts) < 2:
                            await term.sendln(term.warn("Usage: /JOIN <room>"))
                        else:
                            new_name = cmd_parts[1].lower()
                            new_room = get_or_create_room(new_name)
                            # Wire up persistence for dynamically created rooms.
                            if new_room._persist_cb is None and self._db_path:
                                new_room._history_size = HISTORY_LINES
                                new_room._persist_cb = self._make_persist_cb(new_room.name)
                            room.leave(callsign)
                            room = new_room
                            inbox = room.join(callsign)
                            await term.sendln(
                                f"{term.ok('Joined room:')} {term.style(room.name, 'accent', bold=True)}"
                            )
                            await term.sendln(term.field("Users here:", ", ".join(room.who()), "meta"))
                    elif cmd == "/ROOMS":
                        for r in _rooms.values():
                            await term.sendln(
                                f"  {term.style(f'{r.name:<12}', 'accent', bold=True)} "
                                f"{term.note(f'{r.member_count} user(s)')}  {r.description}"
                            )
                    elif cmd == "/HIST" and is_sysop:
                        if self._db_path:
                            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                                async with db.execute(
                                    """
                                    SELECT id, line FROM (
                                        SELECT id, line FROM chat_history WHERE room=?
                                        ORDER BY id DESC LIMIT ?
                                    ) ORDER BY id ASC
                                    """,
                                    (room.name, HISTORY_LINES),
                                ) as cur:
                                    rows = await cur.fetchall()
                            await term.sendln(term.note("--- history ---"))
                            for row_id, line in rows:
                                await term.sendln(f"{term.note(f'[{row_id}]')} {line}")
                            await term.sendln(term.note("--- end ---"))
                        else:
                            await term.sendln(term.warn("No DB path configured."))
                    elif cmd == "/DEL" and is_sysop:
                        if len(cmd_parts) < 2 or not cmd_parts[1].isdigit():
                            await term.sendln(term.warn("Usage: /DEL <message-id>"))
                        else:
                            deleted = await self._delete_message(room.name, int(cmd_parts[1]))
                            if deleted is None:
                                await term.sendln(term.warn(f"Message #{cmd_parts[1]} not found in this room."))
                            else:
                                await term.sendln(term.ok(f"Message #{cmd_parts[1]} deleted."))
                    elif cmd == "/DELROOM" and is_sysop:
                        if len(cmd_parts) < 2:
                            await term.sendln(term.warn("Usage: /DELROOM <room-name>"))
                        else:
                            target = cmd_parts[1].lower()
                            if target == room.name:
                                await term.sendln(term.warn("Cannot delete the room you are currently in. Use /JOIN first."))
                            else:
                                ok = await self._delete_room(target)
                                if ok:
                                    await term.sendln(term.ok(f"Room '{target}' deleted."))
                                else:
                                    await term.sendln(term.warn(f"Room '{target}' not found."))
                    else:
                        await term.sendln(term.warn("Unknown command. Try /WHO /MSG /JOIN /ROOMS /QUIT"))
                else:
                    room.broadcast(callsign, line)
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

    def get_stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "display_name": self.display_name,
            "rooms": {n: r.member_count for n, r in _rooms.items()},
        }
