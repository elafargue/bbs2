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
from typing import Any, Optional, TYPE_CHECKING

import aiosqlite

from bbs.core.plugin_registry import BBSPlugin

if TYPE_CHECKING:
    from bbs.core.session import BBSSession

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 160
HISTORY_LINES = 50  # overridden by config


class ChatRoom:
    """In-memory broadcast room."""

    def __init__(self, name: str, description: str) -> None:
        self.name = name.lower()
        self.description = description
        # callsign → asyncio.Queue[str]
        self._members: dict[str, asyncio.Queue[str]] = {}
        self._history: list[str] = []
        self._history_size = HISTORY_LINES

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
        HISTORY_LINES = cfg.get("history_lines", 50)

        default_rooms = cfg.get("default_rooms", [{"name": "main", "description": "Main chat room"}])
        for room_cfg in default_rooms:
            get_or_create_room(room_cfg["name"], room_cfg.get("description", ""))

    async def handle_session(self, session: "BBSSession") -> None:
        term = session.term
        callsign = session.auth.callsign

        # Join default room
        default_room = next(iter(_rooms.values())) if _rooms else get_or_create_room("main")
        current_room = default_room
        inbox = current_room.join(callsign)

        await term.sendln(f"Entered chat room: {current_room.name}")
        await term.sendln(f"Users here: {', '.join(current_room.who())}")
        # Show recent history
        history = current_room.get_history()
        if history:
            await term.sendln("--- recent ---")
            for line in history[-10:]:
                await term.sendln(line)
            await term.sendln("--- end ---")
        await term.sendln("Commands: /WHO  /MSG <call> <text>  /JOIN <room>  /ROOMS /QUIT")
        await term.sendln()

        try:
            await self._chat_loop(session, current_room, inbox, callsign)
        finally:
            current_room.leave(callsign)

    async def _chat_loop(
        self,
        session: "BBSSession",
        room: ChatRoom,
        inbox: asyncio.Queue[str],
        callsign: str,
    ) -> None:
        term = session.term

        async def _reader() -> None:
            """Forward incoming chat lines to the terminal."""
            while True:
                try:
                    line = await asyncio.wait_for(inbox.get(), timeout=0.5)
                    await term.sendln(line)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break

        reader_task = asyncio.create_task(_reader())

        try:
            while True:
                await term.send(f"{room.name}> ")
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
                        await term.sendln(f"Users in {room.name}: {', '.join(members)}")
                    elif cmd == "/MSG":
                        if len(cmd_parts) < 3:
                            await term.sendln("Usage: /MSG <callsign> <message>")
                        else:
                            dest = cmd_parts[1].upper()
                            text = cmd_parts[2]
                            if not room.private_msg(callsign, dest, text):
                                await term.sendln(f"{dest} is not in this room.")
                    elif cmd == "/JOIN":
                        if len(cmd_parts) < 2:
                            await term.sendln("Usage: /JOIN <room>")
                        else:
                            new_name = cmd_parts[1].lower()
                            new_room = get_or_create_room(new_name)
                            room.leave(callsign)
                            room = new_room
                            inbox = room.join(callsign)
                            await term.sendln(f"Joined room: {room.name}")
                            await term.sendln(f"Users here: {', '.join(room.who())}")
                    elif cmd == "/ROOMS":
                        for r in _rooms.values():
                            await term.sendln(f"  {r.name:<12} {r.member_count} user(s)  {r.description}")
                    else:
                        await term.sendln("Unknown command. Try /WHO /MSG /JOIN /ROOMS /QUIT")
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
