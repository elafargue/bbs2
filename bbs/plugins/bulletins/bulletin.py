"""
bbs/plugins/bulletins/bulletin.py — Bulletins / message board plugin.

Access model
------------
- Reading:     AuthLevel.IDENTIFIED (just having a callsign via AX.25 is enough)
- Posting:     AuthLevel.IDENTIFIED on radio transports (callsign trusted from AX.25 header)
               AuthLevel.AUTHENTICATED on TCP (must pass OTP challenge)
               Messages posted while authenticated are marked with '*' in the listing.
- Sysop ops:   AuthLevel.SYSOP

Commands (from the plugin's own sub-menu)
-----------------------------------------
  A    — List / select areas
  L    — List messages in current area
  R #  — Read message number #
  D #  — Delete message number # (authenticated / sysop only; shown only when area selected)
  S [call] — Send / post a new message (optionally to a specific recipient callsign instead of ALL)
  Q    — Return to main menu
"""
from __future__ import annotations

import re
import textwrap
import time
from typing import Any, Optional, TYPE_CHECKING

import aiosqlite

from bbs.ax25.address import _CALLSIGN_RE
from bbs.core.auth import AuthLevel
from bbs.core.plugin_registry import BBSPlugin
from bbs.core.session import PathLength

if TYPE_CHECKING:
    from bbs.core.session import BBSSession

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS bulletin_areas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT    NOT NULL DEFAULT '',
    read_level  INTEGER NOT NULL DEFAULT 0,
    post_level  INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS bulletin_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id     INTEGER NOT NULL REFERENCES bulletin_areas(id) ON DELETE CASCADE,
    msg_number  INTEGER NOT NULL,
    subject     TEXT    NOT NULL,
    from_call   TEXT    NOT NULL COLLATE NOCASE,
    to_call     TEXT    NOT NULL DEFAULT 'ALL' COLLATE NOCASE,
    body        TEXT    NOT NULL DEFAULT '',
    parent_id   INTEGER REFERENCES bulletin_messages(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    deleted     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bmsg_area   ON bulletin_messages (area_id, deleted, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bmsg_num ON bulletin_messages (area_id, msg_number);

CREATE TABLE IF NOT EXISTS read_receipts (
    user_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL REFERENCES bulletin_messages(id) ON DELETE CASCADE,
    read_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (user_id, message_id)
);
"""


async def _ensure_schema(db_path: str, default_areas: list[dict]) -> None:
    async with aiosqlite.connect(db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA_SQL)
        await db.commit()
        # Lazily add is_default column (not in original schema — tolerate older DBs)
        try:
            await db.execute(
                "ALTER TABLE bulletin_areas ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
        except Exception:
            pass  # column already exists
        # Lazily add authenticated column to bulletin_messages
        try:
            await db.execute(
                "ALTER TABLE bulletin_messages ADD COLUMN authenticated INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
        except Exception:
            pass  # column already exists
        # Seed default areas
        for area in default_areas:
            await db.execute(
                "INSERT OR IGNORE INTO bulletin_areas (name, description) VALUES (?,?)",
                (area["name"].upper(), area.get("description", "")),
            )
        await db.commit()


# ── Plugin class ──────────────────────────────────────────────────────────────

class BulletinsPlugin(BBSPlugin):
    name = "bulletins"
    display_name = "Bulletins"
    menu_key = "BU"
    help_text = "Read and post messages in topic areas. Authentication required to post."
    min_auth_level_name = "IDENTIFIED"

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)
        self._max_body = cfg.get("max_body_bytes", 4096)
        self._max_subject = cfg.get("max_subject_chars", 25)
        self._default_areas: list[dict] = cfg.get(
            "default_areas",
            [
                {"name": "GENERAL", "description": "General discussion"},
                {"name": "TECH",    "description": "Technical topics"},
            ],
        )
        default_area_cfg: str = cfg.get("default_area", "")
        self._default_area_name: Optional[str] = default_area_cfg.upper().strip() or None
        self._enforce_active: bool = bool(cfg.get("enforce_active", False))
        await _ensure_schema(db_path, self._default_areas)

    async def handle_session(self, session: "BBSSession") -> None:
        """Main entry point — runs the bulletins sub-menu until user quits."""
        term = session.term
        db = session.db

        # Restore area selection from earlier in this session
        _bstate = session.plugin_state.setdefault("bulletins", {})
        current_area_id: Optional[int] = _bstate.get("area_id")
        current_area_name: str = _bstate.get("area_name", "")

        # Auto-select the default area on first visit (DB flag takes priority over yaml)
        if current_area_id is None:
            current_area_id, current_area_name = await self._resolve_default_area(db)
            if current_area_id:
                _bstate["area_id"] = current_area_id
                _bstate["area_name"] = current_area_name

        while True:
            if current_area_id:
                unread = await _count_unread(
                    db, current_area_id, session.auth.user_id,
                    callsign=session.auth.callsign, is_sysop=session.auth.is_sysop,
                )
                area_label = f"{current_area_name} ({unread} new)"
            else:
                area_label = "(no area selected)"

            items: list[tuple[str, str]] = [
                ("A",     "Areas (list/select)"),
                ("L",     f"List messages  [{area_label}]"),
                ("R#",   "Read message number #"),
                ("S [call]", "Send message"),
                ("Q",     "Back to main menu"),
                ("?",   "Help"),
            ]
            if current_area_id:
                items.insert(4, ("D#", "Delete message number #"))
            if session.auth.is_sysop:
                items.insert(-1, ("SA", "Sysop: manage areas"))
            raw = await term.prompt_menu("BULLETINS", items)
            choice, numarg = _parse_cmd(raw)

            if choice == "Q":
                break
            elif choice == "A":
                aid, aname = await self._list_areas(session)
                if aid:
                    current_area_id, current_area_name = aid, aname
                    _bstate["area_id"] = current_area_id
                    _bstate["area_name"] = current_area_name
            elif choice == "L":
                if not current_area_id:
                    await term.sendln("Select an area first (A).")
                else:
                    await self._list_messages(session, current_area_id, current_area_name)
            elif choice == "R":
                if not current_area_id:
                    await term.sendln("Select an area first (A).")
                else:
                    await self._do_read(session, current_area_id, numarg)
            elif choice == "S":
                # numarg may be a callsign token like 'ALL' or 'W1AW'
                to_preset = numarg.upper() if numarg else None
                await self._post_message(session, current_area_id, current_area_name,
                                         to_call=to_preset)
            elif choice == "D":
                if not current_area_id:
                    await term.sendln("Select an area first (A).")
                else:
                    await self._delete_message(session, current_area_id, numarg)
            elif choice == "SA":
                if not session.auth.is_sysop:
                    await term.sendln("Sysop access required.")
                else:
                    await self._sysop_areas(session)
            elif choice == "?":
                await self._show_help(session)

    # ── Help ─────────────────────────────────────────────────────────────────

    async def _show_help(self, session: "BBSSession") -> None:
        term = session.term
        auth = session.auth

        # Describe the current user's auth level
        if auth.is_sysop:
            level_desc = term.ok("SYSOP")
        elif auth.is_authenticated:
            level_desc = term.ok("AUTHENTICATED")
        elif auth.is_identified:
            level_desc = term.style("IDENTIFIED", "orange", bold=True)
        else:
            level_desc = term.warn("ANONYMOUS")

        lines = [
            "",
            term.label("BULLETINS — HELP", "meta"),
            term.note("-" * 40),
            "",
            term.field("Your access level:", level_desc, "meta"),
            "",
            term.label("IDENTIFIED  (callsign verified via AX.25 or login)", "meta"),
            f"  {term.ok('YES')}  Browse areas              (A)",
            f"  {term.ok('YES')}  List messages             (L)",
            f"  {term.ok('YES')}  Read public messages      (R#)",
            f"  {term.ok('YES')}  Read private messages addressed to you",
            f"  {term.ok('YES')}  Post messages (without authenticated 'CALLSIGN*' mark) (S)",
            f"  {term.warn('NO ')}  Delete messages",
            "",
            term.label("AUTHENTICATED  (IDENTIFIED + OTP challenge passed — type A)", "meta"),
            f"  {term.ok('YES')}  Everything above",
            f"  {term.ok('YES')}  Post messages             (S)",
            f"        Authenticated posts show {term.style('CALLSIGN*', 'success', bold=True)} in listings",
            f"  {term.ok('YES')}  Delete your own messages  (D#)",
            "",
            term.label("PRIVATE MESSAGES", "meta"),
            "  Address a message to a specific callsign instead of ALL.",
            "  Only the sender and the recipient can see it.",
            f"  {term.warn('NOTE')} Sysop can always see all messages.",
            "",
            term.label("SYSOP", "meta"),
            f"  {term.ok('YES')}  Delete any message",
            f"  {term.ok('YES')}  Manage areas              (SA)",
            "",
        ]
        await term.paginate(lines, timeout=float(session.cfg.idle_timeout) or None)

    # ── Default area lookup ───────────────────────────────────────────────────

    async def _resolve_default_area(
        self, db: aiosqlite.Connection
    ) -> tuple[Optional[int], str]:
        db.row_factory = aiosqlite.Row
        # Prefer the DB-flagged default (set via SA command or web UI)
        async with db.execute(
            "SELECT id, name FROM bulletin_areas WHERE is_default=1 LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row:
            return int(row["id"]), str(row["name"])
        # Fall back to the name configured in bbs.yaml
        if self._default_area_name:
            async with db.execute(
                "SELECT id, name FROM bulletin_areas WHERE name=? COLLATE NOCASE",
                (self._default_area_name,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                return int(row["id"]), str(row["name"])
        return None, ""

    # ── Sysop area management ─────────────────────────────────────────────────

    async def _sysop_areas(self, session: "BBSSession") -> None:
        term = session.term
        while True:
            items: list[tuple[str, str]] = [
                ("N",   "New area"),
                ("E #", "Edit area number #"),
                ("K #", "Kill (delete) area number # and all messages"),
                ("D #", "Set area # as default"),
                ("Q",   "Back"),
            ]
            raw = await term.prompt_menu("SYSOP: AREAS", items)
            cmd, numarg = _parse_cmd(raw)
            if cmd == "Q":
                break
            elif cmd == "N":
                await self._sysop_new_area(session)
            elif cmd == "E":
                await self._sysop_edit_area(session, numarg)
            elif cmd == "K":
                await self._sysop_kill_area(session, numarg)
            elif cmd == "D":
                await self._sysop_set_default(session, numarg)

    async def _get_areas_indexed(
        self, db: aiosqlite.Connection
    ) -> list:
        """Return all areas ordered by name for numbered display."""
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name, description, is_default FROM bulletin_areas ORDER BY name"
        ) as cur:
            return await cur.fetchall()

    async def _show_areas_index(self, term: Any, areas: list) -> None:
        lines = [
            "",
            term.label("  #   NAME        DEFAULT  DESCRIPTION", "meta"),
            term.note("  " + "-" * 50),
        ]
        for i, a in enumerate(areas, 1):
            dflt = "*" if a["is_default"] else " "
            name_padded = f"{a['name']:<10}"
            lines.append(
                f"  {i:2}.  {term.style(name_padded, 'accent', bold=True)}  "
                f"[{term.style(dflt, 'warning', bold=True) if dflt == '*' else ' '}]    {a['description']}"
            )
        lines.append("")
        await term.paginate(lines, timeout=float(session.cfg.idle_timeout) or None)

    async def _sysop_new_area(self, session: "BBSSession") -> None:
        term = session.term
        db = session.db
        await term.send("Area name (up to 20 chars, ENTER to cancel): ")
        name = (await term.readline(max_len=20)).upper().strip()
        if not name:
            return
        import re as _re
        if not _re.match(r'^[A-Z0-9][A-Z0-9\-]{0,19}$', name):
            await term.sendln(term.warn("Invalid name — use uppercase letters, digits, hyphens only."))
            return
        await term.send("Description: ")
        desc = (await term.readline(max_len=80)).strip()
        await term.send(f"Create area '{name}'? [Y/N]: ")
        if (await term.readline(max_len=2)).upper().strip() != "Y":
            await term.sendln(term.note("Cancelled."))
            return
        try:
            await db.execute(
                "INSERT INTO bulletin_areas (name, description) VALUES (?,?)",
                (name, desc),
            )
            await db.commit()
            await term.sendln(term.ok(f"Area '{name}' created."))
        except Exception as exc:
            await term.sendln(f"{term.error('Error:')} {exc}")

    async def _sysop_edit_area(self, session: "BBSSession", numarg: Optional[str]) -> None:
        term = session.term
        db = session.db
        areas = await self._get_areas_indexed(db)
        if not areas:
            await term.sendln("No areas defined.")
            return
        await self._show_areas_index(term, areas)
        if not numarg or not numarg.isdigit():
            await term.send("Area #: ")
            numarg = (await term.readline(max_len=4)).strip()
        if not numarg.isdigit():
            return
        idx = int(numarg) - 1
        if idx < 0 or idx >= len(areas):
            await term.sendln(term.warn("Invalid selection."))
            return
        area = areas[idx]
        await term.sendln(
            f"{term.label('Editing', 'meta')} '{area['name']}' — press ENTER to keep current value."
        )
        await term.send(f"Name [{area['name']}]: ")
        new_name = (await term.readline(max_len=20)).upper().strip() or area["name"]
        await term.send(f"Description [{area['description']}]: ")
        new_desc = (await term.readline(max_len=80)).strip() or area["description"]
        await db.execute(
            "UPDATE bulletin_areas SET name=?, description=? WHERE id=?",
            (new_name, new_desc, area["id"]),
        )
        await db.commit()
        await term.sendln(term.ok("Area updated."))

    async def _sysop_kill_area(self, session: "BBSSession", numarg: Optional[str]) -> None:
        term = session.term
        db = session.db
        areas = await self._get_areas_indexed(db)
        if not areas:
            await term.sendln("No areas defined.")
            return
        await self._show_areas_index(term, areas)
        if not numarg or not numarg.isdigit():
            await term.send("Kill area #: ")
            numarg = (await term.readline(max_len=4)).strip()
        if not numarg.isdigit():
            return
        idx = int(numarg) - 1
        if idx < 0 or idx >= len(areas):
            await term.sendln(term.warn("Invalid selection."))
            return
        area = areas[idx]
        await term.send(f"Delete '{area['name']}' and ALL its messages? [Y/N]: ")
        if (await term.readline(max_len=2)).upper().strip() != "Y":
            await term.sendln(term.note("Cancelled."))
            return
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("DELETE FROM bulletin_areas WHERE id=?", (area["id"],))
        await db.commit()
        await term.sendln(term.ok(f"Area '{area['name']}' deleted."))

    async def _sysop_set_default(self, session: "BBSSession", numarg: Optional[str]) -> None:
        term = session.term
        db = session.db
        # Lazily add is_default column if missing (older DBs)
        try:
            await db.execute(
                "ALTER TABLE bulletin_areas ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0"
            )
            await db.commit()
        except Exception:
            pass
        areas = await self._get_areas_indexed(db)
        if not areas:
            await term.sendln("No areas defined.")
            return
        await self._show_areas_index(term, areas)
        if not numarg or not numarg.isdigit():
            await term.send("Set default area #: ")
            numarg = (await term.readline(max_len=4)).strip()
        if not numarg.isdigit():
            return
        idx = int(numarg) - 1
        if idx < 0 or idx >= len(areas):
            await term.sendln(term.warn("Invalid selection."))
            return
        area = areas[idx]
        await db.execute("UPDATE bulletin_areas SET is_default=0")
        await db.execute("UPDATE bulletin_areas SET is_default=1 WHERE id=?", (area["id"],))
        await db.commit()
        await term.sendln(term.ok(f"'{area['name']}' is now the default area."))

    # ── List areas ────────────────────────────────────────────────────────────

    async def _list_areas(
        self, session: "BBSSession"
    ) -> tuple[Optional[int], str]:
        term = session.term
        db = session.db
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT id, name, description FROM bulletin_areas ORDER BY name"
        ) as cur:
            areas = await cur.fetchall()

        if not areas:
            await term.sendln("No bulletin areas defined.")
            return None, ""

        lines = ["", term.label("BULLETIN AREAS", "meta"), term.note("-" * 40)]
        for i, row in enumerate(areas, 1):
            name_padded = f"{row['name']:<10}"
            if session.path_length is PathLength.SHORT:
                lines.append(
                    f"  {i:2}. {term.style(name_padded, 'accent', bold=True)} {row['description']}"
                )
            else:
                lines.append(
                    f"  {i:2}. {term.style(name_padded, 'accent', bold=True)}"
                )
        area_prompt = (
            "Area #: " if session.path_length is PathLength.LONG
            else "Enter area number (or ENTER to cancel): "
        )
        lines += ["", area_prompt]
        await term.paginate(lines[:-1], timeout=float(session.cfg.idle_timeout) or None)
        await term.send(term.prompt(lines[-1]))

        choice_str = (await term.readline(max_len=4)).strip()
        if not choice_str.isdigit():
            return None, ""
        idx = int(choice_str) - 1
        if idx < 0 or idx >= len(areas):
            await term.sendln(term.warn("Invalid selection."))
            return None, ""

        selected = areas[idx]
        return int(selected["id"]), str(selected["name"])

    # ── Fetch messages ────────────────────────────────────────────────────────

    async def _fetch_messages(
        self,
        db: "aiosqlite.Connection",
        area_id: int,
        callsign: str = "",
        is_sysop: bool = False,
    ) -> list:
        """Return messages visible to *callsign*.

        Public messages (to_call='ALL') are always returned.  Private messages
        (any other to_call) are returned only when *callsign* matches the
        sender or recipient, or when *is_sysop* is True.
        """
        db.row_factory = aiosqlite.Row
        select = (
            "SELECT id, msg_number, subject, from_call, to_call, body, created_at, authenticated"
            " FROM bulletin_messages"
        )
        if is_sysop or not callsign:
            sql = f"{select} WHERE area_id=? AND deleted=0 ORDER BY created_at DESC"
            params: tuple = (area_id,)
        else:
            sql = (
                f"{select} WHERE area_id=? AND deleted=0"
                "   AND (to_call='ALL' OR from_call=? OR to_call=?)"
                " ORDER BY created_at DESC"
            )
            params = (area_id, callsign, callsign)
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _fetch_read_ids(
        self, db: "aiosqlite.Connection", area_id: int, user_id: Optional[int]
    ) -> Optional[set]:
        """Return the set of message IDs in *area_id* already read by *user_id*.

        Returns ``None`` when there is no user_id (anonymous / unidentified
        sessions) so callers can skip the unread indicator entirely.
        """
        if not user_id:
            return None
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT r.message_id
               FROM read_receipts r
               JOIN bulletin_messages m ON m.id = r.message_id
               WHERE r.user_id=? AND m.area_id=? AND m.deleted=0""",
            (user_id, area_id),
        ) as cur:
            rows = await cur.fetchall()
        return {row["message_id"] for row in rows}

    async def _show_message_index(
        self, term: Any, area_name: str, messages: list,
        read_ids: Optional[set] = None,
        idle_timeout: Optional[float] = None,
        callsign: Optional[str] = None,
        path_length: PathLength = PathLength.SHORT,
    ) -> None:
        if path_length is PathLength.LONG:
            hdr      = f"{'#':<6} {'TO':<7} {'FROM':<9} {'DATE':<15} SUBJECT"
            date_fmt = "%y-%m-%d %H:%M"
        elif path_length is PathLength.MEDIUM:
            hdr      = f"{'#':<6} {'ST':<2} {'TO':<7} {'FROM':<9} {'DATE':<16} SUBJECT"
            date_fmt = "%Y-%m-%d %H:%M"
        else:
            hdr      = f"{'#':<6} {'ST':<2} {'SIZE':<6} {'TO':<7} {'FROM':<9} {'DATE':<20} SUBJECT"
            date_fmt = "%Y-%m-%d %H:%M:%S"
        sep = "-" * len(hdr)
        lines = [
            "",
            f"{term.label(area_name, 'meta')} {term.note(f'— {len(messages)} message(s)')}",
            term.label(hdr, 'meta'),
            term.note(sep),
        ]
        for m in messages:
            to = str(m["to_call"]).upper()
            st = "P" if any(c.isdigit() for c in to) else "B"
            size = len(m["body"]) if m["body"] else 0
            date = time.strftime(date_fmt, time.localtime(m["created_at"]))
            subj = str(m["subject"])[:self._max_subject]
            from_disp = m["from_call"] + ("*" if m["authenticated"] else "")
            is_sender = callsign and m["from_call"].upper() == callsign.upper()
            unread    = read_ids is not None and m["id"] not in read_ids and not is_sender
            num_disp  = f"{m['msg_number']:<4}{'*' if unread else ' '} "
            num_color = "warning" if unread else "accent"
            from_str  = f"{from_disp:<9}"
            is_auth   = bool(m["authenticated"])
            from_tone = "success" if is_auth else "orange"
            if path_length is PathLength.LONG:
                lines.append(
                    f"{term.style(num_disp, num_color, bold=True)}"
                    f"{to:<7} {term.style(from_str, from_tone, bold=True)} "
                    f"{term.note(f'{date:<15}')} {subj}"
                )
            elif path_length is PathLength.MEDIUM:
                st_str = f"{st:<2}"
                lines.append(
                    f"{term.style(num_disp, num_color, bold=True)}"
                    f"{term.style(st_str, 'warning' if st == 'P' else 'meta', bold=st == 'P')} "
                    f"{to:<7} {term.style(from_str, from_tone, bold=True)} "
                    f"{term.note(f'{date:<16}')} {subj}
                )
            else:
                st_str = f"{st:<2}"
                lines.append(
                    f"{term.style(num_disp, num_color, bold=True)}"
                    f"{term.style(st_str, 'warning' if st == 'P' else 'meta', bold=st == 'P')} "
                    f"{size:<6} {to:<7} {term.style(from_str, from_tone, bold=True)} "
                    f"{term.note(f'{date:<20}')} {subj}"
                )
        lines.append("")
        await term.paginate(lines, timeout=idle_timeout)

    # ── List messages (interactive) ───────────────────────────────────────────

    async def _list_messages(
        self, session: "BBSSession", area_id: int, area_name: str
    ) -> None:
        term = session.term
        messages = await self._fetch_messages(
            session.db, area_id, session.auth.callsign, session.auth.is_sysop
        )
        if not messages:
            await term.sendln(term.note(f"No messages in {area_name}."))
            return
        read_ids = await self._fetch_read_ids(session.db, area_id, session.auth.user_id)
        await self._show_message_index(term, area_name, messages, read_ids=read_ids,
                                        idle_timeout=float(session.cfg.idle_timeout) or None,
                                        callsign=session.auth.callsign,
                                        path_length=session.path_length)

    # ── Read a single message ─────────────────────────────────────────────────

    async def _do_read(
        self,
        session: "BBSSession",
        area_id: int,
        numarg: Optional[str],
    ) -> None:
        term = session.term
        if not numarg or not numarg.isdigit():
            await term.send("Message#: ")
            numarg = (await term.readline(max_len=6)).strip()
        if not numarg.isdigit():
            return
        msg_num = int(numarg)
        messages = await self._fetch_messages(
            session.db, area_id, session.auth.callsign, session.auth.is_sysop
        )
        target = next((m for m in messages if m["msg_number"] == msg_num), None)
        if not target:
            await term.sendln(term.warn("Message not found."))
            return
        await self._display_message_body(session, target)

    async def _display_message_body(
        self, session: "BBSSession", target: Any
    ) -> None:
        term = session.term
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(target["created_at"]))
        auth_mark = "*" if target["authenticated"] else ""
        from_tone = "success" if target["authenticated"] else "orange"
        msg_lines = [
            "",
            (
                f"{term.label('From:', 'meta')} {term.style(str(target['from_call']) + auth_mark, from_tone, bold=True)}  "
                f"{term.label('To:', 'meta')} {target['to_call']}  "
                f"{term.label('Date:', 'meta')} {ts}"
            ),
            f"{term.label('Subj:', 'meta')} {term.style(str(target['subject']), 'accent', bold=True)}",
            term.note("-" * 60),
        ]
        for raw in str(target["body"]).splitlines():
            # Keep display width readable on packet links regardless of terminal width.
            wrapped = textwrap.wrap(
                raw,
                width=80,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            msg_lines.extend(wrapped or [""])
        msg_lines.append("")
        _cfg = getattr(session, "cfg", None)
        _timeout = (float(getattr(_cfg, "idle_timeout", 0)) or None) if _cfg else None
        await term.paginate(msg_lines, timeout=_timeout)

        if session.auth.user_id:
            try:
                await session.db.execute(
                    "INSERT OR IGNORE INTO read_receipts (user_id, message_id) VALUES (?,?)",
                    (session.auth.user_id, target["id"]),
                )
                await session.db.commit()
            except Exception:
                pass

    # ── Post message ──────────────────────────────────────────────────────────

    async def _post_message(
        self,
        session: "BBSSession",
        area_id: Optional[int],
        area_name: str,
        to_call: Optional[str] = None,
    ) -> None:
        term = session.term
        db = session.db
        # Auth check — radio and web transports trust the caller's identity already;
        # TCP/Telnet users must have passed OTP (AUTHENTICATED) to post.
        _TRUSTED_TRANSPORTS = ("kernel_ax25", "kiss_tcp", "kiss_serial", "agwpe", "web")
        via_trusted = session.conn.transport_id in _TRUSTED_TRANSPORTS
        if not (via_trusted and session.auth.is_identified) and not session.auth.is_authenticated:
            await term.sendln(
                term.warn("AUTH required to post. Type 'A' at main menu to authenticate.")
            )
            return
        # Check active/approved status when enforce_active is enabled
        if self._enforce_active and not session.auth.approved:
            await term.sendln(
                term.warn("Your account is pending sysop approval. Contact the sysop to enable posting access.")
            )
            return
        is_authenticated_post = session.auth.is_authenticated

        # Area selection if none chosen yet
        if not area_id:
            area_id, area_name = await self._list_areas(session)
        if not area_id:
            return

        # Gather subject
        await term.send(f"Subject ({self._max_subject} chars max): ")
        subject = (await term.readline(max_len=self._max_subject)).strip()
        if not subject:
            await term.sendln(term.note("Cancelled."))
            return

        # Gather To: (default ALL) — skip prompt when pre-supplied via command (e.g. "S W1AW")
        async def _validate_to_call(candidate: str) -> bool:
            """Return True if candidate is ALL or a registered user callsign."""
            if candidate == "ALL":
                return True
            if not _CALLSIGN_RE.match(candidate):
                await term.sendln(term.warn(
                    f"{candidate!r} is not a valid callsign. Use ALL or a callsign like W1AW or N0CALL-1."
                ))
                return False
            async with db.execute(
                "SELECT 1 FROM users WHERE callsign=? COLLATE NOCASE LIMIT 1",
                (candidate,),
            ) as cur:
                if await cur.fetchone():
                    return True
            await term.sendln(term.warn(
                f"{candidate} is not a registered user on this BBS. Use ALL or a known callsign."
            ))
            return False

        if to_call and not await _validate_to_call(to_call):
            to_call = None
        if to_call:
            await term.sendln(f"To: {term.style(to_call, 'accent', bold=True)}")
        else:
            while True:
                await term.send("To [ALL]: ")
                to_call = (await term.readline(max_len=10)).upper().strip() or "ALL"
                if await _validate_to_call(to_call):
                    break

        # Gather body — /EX on its own line ends input (classic BBS convention)
        await term.sendln(term.label(f"Enter message body ({self._max_body} bytes max).", 'meta'))
        await term.sendln(term.note("Type /EX on a line by itself when done:"))
        body_lines = []
        total_bytes = 0
        body_line_max = max(80, int(self._max_body))
        while True:
            await term.send("> ")
            line = await term.readline(max_len=body_line_max, echo=False)
            if line.strip().upper() == "/EX":
                break
            total_bytes += len(line) + 1
            if total_bytes > self._max_body:
                await term.sendln(term.warn("[Body limit reached]"))
                break
            body_lines.append(line)

        body = "\n".join(body_lines)

        # Confirm (ENTER defaults to Y)
        await term.send("Post message? [Y/n]: ")
        confirm = (await term.readline(max_len=2)).upper().strip()
        if confirm not in ("", "Y"):
            await term.sendln(term.note("Cancelled."))
            return

        db.row_factory = aiosqlite.Row
        # Atomic INSERT: msg_number is computed inline so there is no TOCTOU
        # window between the SELECT MAX and the INSERT even under concurrent
        # sessions sharing the same SQLite database.
        cur = await db.execute(
            """INSERT INTO bulletin_messages
               (area_id, msg_number, subject, from_call, to_call, body, authenticated)
               VALUES (?,
                       (SELECT COALESCE(MAX(msg_number),0)+1
                        FROM bulletin_messages WHERE area_id=?),
                       ?,?,?,?,?)""",
            (area_id, area_id, subject, session.auth.callsign, to_call, body,
             1 if is_authenticated_post else 0),
        )
        next_num = cur.lastrowid  # approximate; use a follow-up SELECT if needed
        await db.commit()
        # Retrieve the actual msg_number we just inserted
        async with db.execute(
            "SELECT msg_number FROM bulletin_messages WHERE rowid=?", (next_num,)
        ) as sel:
            row_ins = await sel.fetchone()
            next_num = row_ins["msg_number"] if row_ins else "?"
        await term.sendln(term.ok(f"Message #{next_num} posted to {area_name}."))

        if self._bus is not None:
            await self._bus.publish("bulletin.new_message", {
                "area":      area_name,
                "from_call": session.auth.callsign,
                "to_call":   to_call,
                "subject":   subject,
                "msg_id":    next_num,
                "timestamp": __import__("time").time(),
            })

    # ── Delete message ────────────────────────────────────────────────────────

    async def _delete_message(
        self, session: "BBSSession", area_id: int, numarg: Optional[str] = None
    ) -> None:
        term = session.term
        db = session.db

        if not session.auth.is_authenticated:
            await term.sendln(term.warn("AUTH required to delete messages."))
            return

        if not numarg or not numarg.isdigit():
            await term.send("Delete message#: ")
            numarg = (await term.readline(max_len=6)).strip()
        if not numarg.isdigit():
            return

        msg_num = int(numarg)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, from_call FROM bulletin_messages WHERE area_id=? AND msg_number=? AND deleted=0",
            (area_id, msg_num),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await term.sendln(term.warn("Message not found."))
            return

        # Only own message or sysop
        if (
            row["from_call"].upper() != session.auth.callsign.upper()
            and not session.auth.is_sysop
        ):
            await term.sendln(term.warn("You can only delete your own messages."))
            return

        await db.execute(
            "UPDATE bulletin_messages SET deleted=1 WHERE id=?", (row["id"],)
        )
        await db.commit()
        await term.sendln(term.ok(f"Message #{msg_num} deleted."))

    # ── Web stats ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "display_name": self.display_name}


# ── Helpers ───────────────────────────────────────────────────────────────────

# Matches: verb + optional numeric arg (R1 / R 1) OR verb + optional word arg (S ALL / S W1AW)
_CMD_RE = re.compile(r'^([A-Z]+)\s*([A-Z0-9][-A-Z0-9]*)?$')

def _parse_cmd(raw: str) -> tuple[str, Optional[str]]:
    """Parse 'R 1', 'R1', 'D 5', 'D5', 'S ALL', 'S W1AW', 'Q', etc.
    Returns (verb, arg_or_None).  The arg may be numeric ('1') or a callsign
    token ('ALL', 'W1AW'); callers decide how to interpret it.
    """
    m = _CMD_RE.match(raw)
    if not m:
        return raw, None
    return m.group(1), m.group(2)

async def _count_unread(
    db: aiosqlite.Connection,
    area_id: int,
    user_id: Optional[int],
    callsign: Optional[str] = None,
    is_sysop: bool = False,
) -> int:
    """Count messages not yet read by *user_id* in *area_id*.

    Applies the same visibility rules as _fetch_messages.  Messages sent by
    *callsign* are never counted as unread — the sender wrote them.
    """
    if is_sysop or not callsign:
        vis_sql = ""
        vis_args: tuple = ()
    else:
        vis_sql = "AND (m.to_call='ALL' OR m.from_call=? OR m.to_call=?)"
        vis_args = (callsign, callsign)

    # Never count messages sent by this user as unread — they wrote them.
    sender_sql = " AND m.from_call!=?" if callsign else ""
    sender_args: tuple = (callsign,) if callsign else ()

    if not user_id:
        async with db.execute(
            f"SELECT COUNT(*) FROM bulletin_messages m"
            f" WHERE m.area_id=? AND m.deleted=0 {vis_sql}",
            (area_id,) + vis_args,
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async with db.execute(
        f"""SELECT COUNT(*) FROM bulletin_messages m
           LEFT JOIN read_receipts r ON r.message_id=m.id AND r.user_id=?
           WHERE m.area_id=? AND m.deleted=0 AND r.message_id IS NULL {vis_sql}{sender_sql}""",
        (user_id, area_id) + vis_args + sender_args,
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0
