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

/MSG delivery
-------------
A whisper is delivered live to the recipient wherever they are in chat (any
room, not just the sender's).  When they are not in chat at all it is stored
rather than dropped:

  1. If the bulletins plugin is enabled, the whisper is posted there as a
     private message addressed to the recipient.
  2. Otherwise it is queued in chat_offline_msgs and shown to the recipient
     the next time they enter chat.

Access: IDENTIFIED (just a valid callsign via AX.25 is enough to chat).
"""
from __future__ import annotations

import asyncio
import logging
import re
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

-- /MSG whispers for stations that were not in chat, held until they return.
-- Only used when the bulletins plugin is unavailable to take them.
CREATE TABLE IF NOT EXISTS chat_offline_msgs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    to_call   TEXT    NOT NULL COLLATE NOCASE,
    from_call TEXT    NOT NULL COLLATE NOCASE,
    room      TEXT    NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL,
    text      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_offline_to ON chat_offline_msgs (to_call, id);
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
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
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
        """Send a private message.  Returns False if the recipient is not in
        this room, or if their queue is full — the caller then falls back to
        offline delivery rather than silently dropping the whisper."""
        q = self._members.get(to_call.upper())
        if not q:
            return False
        ts = time.strftime("%H:%M")
        line = f"[{ts}] *{from_call}→{to_call}*: {text}"
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            logger.warning("chat: dropped private message to %s (queue full)", to_call)
            return False
        _last_whisper[to_call.upper()] = from_call.upper()
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
                logger.warning("chat: dropped message to %s (queue full)", call)

    @property
    def member_count(self) -> int:
        return len(self._members)


# Module-level room registry — shared across all sessions
_rooms: dict[str, ChatRoom] = {}

# recipient callsign → callsign that last whispered them, for /R.  Module-level
# because the sender writes it into the *recipient's* state, across sessions.
_last_whisper: dict[str, str] = {}


def get_or_create_room(name: str, description: str = "") -> ChatRoom:
    key = name.lower()
    if key not in _rooms:
        _rooms[key] = ChatRoom(name, description)
    return _rooms[key]


#: Matches the join/leave notices produced by ChatRoom.join()/leave().
_JOIN_LEAVE_RE = re.compile(r"^\*\*\* (\S+) (?:joined|left) .+ \*\*\* *$")


def _compact_history(
    rows: list[tuple[int, str]], exclude: Optional[str] = None
) -> list[tuple[Optional[int], str]]:
    """Collapse runs of join/leave notices in scrollback into one line.

    A busy room otherwise replays dozens of '*** W1ABC joined main ***' lines
    between two actual messages, which is a lot of airtime at 1200 bps.  Each
    run becomes a single 'visited' line naming the callsigns once, in the order
    they first appeared.  *exclude* drops one callsign from the summaries — the
    reader's own arrival is already in the log by the time they see it.

    Input rows are (db_id, line); the returned id is None for a summary line
    (it has no single database row behind it) and the original id otherwise.
    """
    out: list[tuple[Optional[int], str]] = []
    visitors: list[str] = []
    skip = exclude.upper() if exclude else None

    def flush() -> None:
        if visitors:
            out.append((None, f"*** visited: {', '.join(visitors)} ***"))
            visitors.clear()

    for row_id, line in rows:
        m = _JOIN_LEAVE_RE.match(line)
        if m:
            call = m.group(1)
            if call.upper() != skip and call not in visitors:
                visitors.append(call)
        else:
            flush()
            out.append((row_id, line))
    flush()
    return out


def find_member_room(callsign: str) -> Optional[ChatRoom]:
    """Return the room *callsign* is currently sitting in, or None."""
    call = callsign.upper()
    for room in _rooms.values():
        if call in room._members:
            return room
    return None


# ── Plugin class ──────────────────────────────────────────────────────────────

class ChatPlugin(BBSPlugin):
    name = "chat"
    display_name = "Chat"
    menu_key = "C"
    help_text = "Real-time text chat with other connected stations."
    min_auth_level_name = "IDENTIFIED"

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)
        global HISTORY_LINES
        HISTORY_LINES = cfg.get("history_lines", 20)
        # Bulletin area that offline /MSG whispers are posted to.  Empty =
        # let the bulletins plugin pick its default area.
        self._msg_area: str = str(cfg.get("msg_area", "") or "").strip()

        default_rooms = cfg.get("default_rooms", [{"name": "main", "description": "Main chat room"}])
        for room_cfg in default_rooms:
            get_or_create_room(room_cfg["name"], room_cfg.get("description", ""))

        # Create schema and restore persisted history into each room.
        async with aiosqlite.connect(db_path, timeout=30) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
            # Rooms users created with /JOIN are not in the config — rebuild
            # them from their surviving history so they are not orphaned
            # (listed by the web API, invisible to /ROOMS) across a restart.
            async with db.execute("SELECT DISTINCT room FROM chat_history") as cur:
                for (room_name,) in await cur.fetchall():
                    get_or_create_room(room_name)
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
        # Signal all member reader tasks to exit by sending a None sentinel.
        for q in room._members.values():
            try:
                q.put_nowait(None)  # type: ignore[arg-type]
            except asyncio.QueueFull:
                pass
        return True

    async def pending_notice(self, session: "BBSSession") -> Optional[str]:
        """Report whispers held for this user while they were away."""
        callsign = session.auth.callsign
        if not self._db_path or not callsign:
            return None
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM chat_offline_msgs"
                    " WHERE to_call=? COLLATE NOCASE",
                    (callsign,),
                ) as cur:
                    row = await cur.fetchone()
        except Exception:
            return None
        count = int(row[0]) if row else 0
        if not count:
            return None
        plural = "s" if count != 1 else ""
        return f"You have {count} chat message{plural} waiting — {self.menu_key} to read."

    # ── /MSG delivery ─────────────────────────────────────────────────────────

    async def _deliver_private_msg(
        self,
        session: "BBSSession",
        room: ChatRoom,
        from_call: str,
        dest: str,
        text: str,
    ) -> None:
        """Handle one /MSG: live delivery if the recipient is in chat anywhere,
        otherwise store it so it reaches them later.  Always tells the sender
        what happened."""
        term = session.term
        ts = time.strftime("%H:%M")

        if dest == from_call.upper():
            await term.sendln(term.warn("You cannot /MSG yourself."))
            return

        # 1. Live delivery — current room first, then any other room.
        target = room if dest in room._members else find_member_room(dest)
        if target is not None and target.private_msg(from_call, dest, text):
            where = "" if target is room else f" (in {target.name})"
            await term.sendln(term.note(f"[{ts}] *you→{dest}{where}*: {text}"))
            return

        # 2. Not reachable live — refuse typos before storing anything.  Being a
        # registered user is the test: it also rejects malformed callsigns.
        if not await self._is_known_user(session.db, dest):
            await term.sendln(term.warn(
                f"{dest} is not in chat and is not a known user here — nothing sent."
            ))
            return

        # 3. Hand off to bulletins when it is available.
        posted = await self._post_offline_bulletin(session, room, from_call, dest, text)
        if posted is not None:
            msg_number, area_name = posted
            await term.sendln(term.ok(
                f"{dest} is not in chat — saved as {area_name} message "
                f"#{msg_number} addressed to them."
            ))
            return

        # 4. Otherwise hold it in chat until they next enter.
        if await self._store_offline_msg(from_call, dest, room.name, text):
            await term.sendln(term.ok(
                f"{dest} is not in chat — held, and shown when they next join."
            ))
        else:
            await term.sendln(term.warn(
                f"{dest} is not in chat and the message could not be stored."
            ))

    @staticmethod
    async def _is_known_user(db: Any, callsign: str) -> bool:
        """True if *callsign* has a user record on this BBS."""
        try:
            async with db.execute(
                "SELECT 1 FROM users WHERE callsign=? COLLATE NOCASE LIMIT 1",
                (callsign,),
            ) as cur:
                return await cur.fetchone() is not None
        except Exception:
            logger.exception("chat: user lookup failed for %s", callsign)
            return False

    async def _post_offline_bulletin(
        self,
        session: "BBSSession",
        room: ChatRoom,
        from_call: str,
        dest: str,
        text: str,
    ) -> Optional[tuple[int, str]]:
        """Post an undeliverable whisper as a private bulletin.  Returns
        (msg_number, area_name), or None when bulletins cannot take it."""
        bulletins = session.plugin_registry.get("bulletins")
        if bulletins is None or not bulletins.enabled:
            return None
        post = getattr(bulletins, "post_private_message", None)
        if post is None:
            return None
        stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        body = f"{text}\n\n-- sent from chat room '{room.name}' at {stamp} UTC"
        try:
            return await post(
                session.db,
                from_call,
                dest,
                f"Chat msg from {from_call}",
                body,
                area_name=self._msg_area or None,
                authenticated=session.auth.is_authenticated,
            )
        except Exception:
            logger.exception("chat: failed to post offline /MSG to bulletins")
            return None

    async def _store_offline_msg(
        self, from_call: str, to_call: str, room_name: str, text: str
    ) -> bool:
        """Queue a whisper in chat_offline_msgs.  False if it could not be saved."""
        if not self._db_path:
            return False
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                await db.execute(
                    "INSERT INTO chat_offline_msgs (to_call, from_call, room, ts, text)"
                    " VALUES (?,?,?,?,?)",
                    (to_call.upper(), from_call.upper(), room_name, int(time.time()), text),
                )
                await db.commit()
            return True
        except Exception:
            logger.exception("chat: failed to store offline /MSG for %s", to_call)
            return False

    async def _flush_offline_msgs(self, term: Any, callsign: str) -> None:
        """Show and clear any whispers held for *callsign*."""
        if not self._db_path:
            return
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                async with db.execute(
                    "SELECT id, from_call, room, ts, text FROM chat_offline_msgs"
                    " WHERE to_call=? COLLATE NOCASE ORDER BY id",
                    (callsign.upper(),),
                ) as cur:
                    rows = await cur.fetchall()
                if not rows:
                    return
                await db.execute(
                    "DELETE FROM chat_offline_msgs WHERE to_call=? COLLATE NOCASE",
                    (callsign.upper(),),
                )
                await db.commit()
        except Exception:
            logger.exception("chat: failed to read offline /MSGs for %s", callsign)
            return

        await term.sendln(term.note(f"--- {len(rows)} message(s) left for you ---"))
        for _row_id, from_call, room_name, ts, text in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            await term.sendln(f"[{stamp}] *{from_call}→you* ({room_name}): {text}")
        await term.sendln(term.note("--- end ---"))
        # /R answers the most recent of them.
        _last_whisper[callsign.upper()] = str(rows[-1][1]).upper()

    async def _replay_history(
        self, term: Any, room: ChatRoom, callsign: str, is_sysop: bool
    ) -> None:
        """Print a room's recent scrollback — on entering chat and on /JOIN.

        Join/leave churn is collapsed; the sysop sees message IDs so they can
        /DEL them.  Prints nothing at all for an empty room.
        """
        db_history: list = []
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
                    db_history = await cur.fetchall()
        plain_history = room.get_history()
        if not db_history and not plain_history:
            return

        if db_history:
            rows = [(int(row_id), line) for row_id, line in db_history]
        else:
            rows = [(0, line) for line in plain_history[-10:]]
        compacted = _compact_history(rows, exclude=callsign)
        if not compacted:                 # nothing but our own arrival
            return

        await term.sendln(term.note("--- recent ---"))
        for row_id, line in compacted:
            if row_id is None:            # visitor summary
                await term.sendln(term.note(line))
            elif is_sysop and row_id:
                await term.sendln(f"{term.note(f'[{row_id}]')} {line}")
            else:
                await term.sendln(line)
        await term.sendln(term.note("--- end ---"))

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
        await self._replay_history(term, current_room, callsign, is_sysop)
        cmds = "/WHO  /MSG <call> <text>  /R <text>  /JOIN <room>  /ROOMS /QUIT"
        if is_sysop:
            cmds += "  /HIST  /DEL <id>  /DELROOM <room>"
        await term.sendln(f"{term.label('Commands:', 'meta')} {cmds}")
        # Whispers left while this station was away — shown last so they sit
        # right above the prompt on a slow scrolling terminal.
        await self._flush_offline_msgs(term, callsign)
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
                    if line is None:  # room deleted — signal chat loop to exit
                        inbox.put_nowait(None)  # re-queue so _chat_loop can see it
                        break
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
                # If the room was deleted (reader_task finished naturally), exit.
                if reader_task.done() and not reader_task.cancelled():
                    await term.sendln(term.warn("Room has been deleted. Leaving chat."))
                    break
                await term.send(term.prompt(f"{room.name}> "))
                line = await term.readline(max_len=MAX_MSG_LEN, echo=False)
                session.touch()

                if not line:
                    continue

                if line.startswith("/"):
                    cmd_parts = line.split(None, 2)
                    cmd = cmd_parts[0].upper()
                    session.log_command("chat", cmd)

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
                            await self._deliver_private_msg(
                                session, room, callsign,
                                cmd_parts[1].upper(), cmd_parts[2],
                            )
                    elif cmd == "/R":
                        # Reply to whoever whispered us last — the whole rest of
                        # the line is the message, not just the first two words.
                        reply_to = _last_whisper.get(callsign.upper())
                        body = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
                        if not reply_to:
                            await term.sendln(term.warn(
                                "Nobody has messaged you yet. Use /MSG <callsign> <message>."
                            ))
                        elif not body:
                            await term.sendln(term.warn(f"Usage: /R <message>  (replies to {reply_to})"))
                        else:
                            await self._deliver_private_msg(
                                session, room, callsign, reply_to, body
                            )
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
                            await self._replay_history(term, room, callsign, is_sysop)
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
                        await term.sendln(term.warn("Unknown command. Try /WHO /MSG /R /JOIN /ROOMS /QUIT"))
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
