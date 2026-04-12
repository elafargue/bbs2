"""
bbs/plugins/bulletins/bulletin.py — Bulletins / message board plugin.

Access model
------------
- Reading:     AuthLevel.IDENTIFIED (just having a callsign via AX.25 is enough)
- Posting:     AuthLevel.AUTHENTICATED (must pass HMAC challenge)
- Sysop ops:   AuthLevel.SYSOP

Commands (from the plugin's own sub-menu)
-----------------------------------------
  L  — List areas
  R  — Read messages in current area (paged, newest-first)
  S  — Send / post a new message
  D  — Delete own message (by number in current area)
  Q  — Return to main menu
"""
from __future__ import annotations

import time
from typing import Any, Optional, TYPE_CHECKING

import aiosqlite

from bbs.core.auth import AuthLevel
from bbs.core.plugin_registry import BBSPlugin

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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA_SQL)
        await db.commit()
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
    menu_key = "B"
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
        await _ensure_schema(db_path, self._default_areas)

    async def handle_session(self, session: "BBSSession") -> None:
        """Main entry point — runs the bulletins sub-menu until user quits."""
        term = session.term
        db = session.db
        current_area_id: Optional[int] = None
        current_area_name: str = ""

        while True:
            if current_area_id:
                unread = await _count_unread(db, current_area_id, session.auth.user_id)
                area_label = f"{current_area_name} ({unread} new)"
            else:
                area_label = "(no area selected)"

            items = [
                ("L", f"List areas"),
                ("R", f"Read  [{area_label}]"),
                ("S", f"Send message"),
                ("D", f"Delete message"),
                ("Q", f"Back to main menu"),
            ]
            await term.send_menu("BULLETINS", items)
            choice = (await term.readline(max_len=4)).upper().strip()

            if choice == "Q":
                break
            elif choice == "L":
                current_area_id, current_area_name = await self._list_areas(session)
            elif choice == "R":
                if not current_area_id:
                    current_area_id, current_area_name = await self._list_areas(session)
                if current_area_id:
                    await self._read_messages(session, current_area_id, current_area_name)
            elif choice == "S":
                await self._post_message(session, current_area_id, current_area_name)
            elif choice == "D":
                if current_area_id:
                    await self._delete_message(session, current_area_id)
                else:
                    await term.sendln("Select an area first (L).")

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

        lines = ["", "BULLETIN AREAS", "-" * 40]
        for i, row in enumerate(areas, 1):
            lines.append(f"  {i:2}. {row['name']:<10} {row['description']}")
        lines += ["", "Enter area number (or ENTER to cancel): "]
        await term.paginate(lines[:-1])
        await term.send(lines[-1])

        choice_str = (await term.readline(max_len=4)).strip()
        if not choice_str.isdigit():
            return None, ""
        idx = int(choice_str) - 1
        if idx < 0 or idx >= len(areas):
            await term.sendln("Invalid selection.")
            return None, ""

        selected = areas[idx]
        return int(selected["id"]), str(selected["name"])

    # ── Read messages ─────────────────────────────────────────────────────────

    async def _read_messages(
        self, session: "BBSSession", area_id: int, area_name: str
    ) -> None:
        term = session.term
        db = session.db
        db.row_factory = aiosqlite.Row

        async with db.execute(
            """SELECT id, msg_number, subject, from_call, to_call, body, created_at
               FROM bulletin_messages
               WHERE area_id=? AND deleted=0
               ORDER BY created_at DESC""",
            (area_id,),
        ) as cur:
            messages = await cur.fetchall()

        if not messages:
            await term.sendln(f"No messages in {area_name}.")
            return

        # Index / header list
        lines = [
            "",
            f"  {area_name} — {len(messages)} message(s)",
            f"  {'#':>4}  {'FROM':<10} {'TO':<10}  SUBJECT",
            "  " + "-" * 56,
        ]
        for m in messages:
            subj = str(m["subject"])[:self._max_subject]
            lines.append(
                f"  {m['msg_number']:>4}  {m['from_call']:<10} {m['to_call']:<10}  {subj}"
            )
        lines.append("")
        lines.append("Enter msg# to read (or ENTER to return): ")

        await term.paginate(lines[:-1])
        await term.send(lines[-1])
        choice_str = (await term.readline(max_len=6)).strip()
        if not choice_str.isdigit():
            return

        msg_num = int(choice_str)
        # Find matching message
        target = next((m for m in messages if m["msg_number"] == msg_num), None)
        if not target:
            await term.sendln("Message not found.")
            return

        # Display full message
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(target["created_at"]))
        msg_lines = [
            "",
            f"From: {target['from_call']}  To: {target['to_call']}  Date: {ts}",
            f"Subj: {target['subject']}",
            "-" * 60,
        ]
        msg_lines += str(target["body"]).splitlines()
        msg_lines.append("")

        await term.paginate(msg_lines)

        # Mark as read
        if session.auth.user_id:
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO read_receipts (user_id, message_id) VALUES (?,?)",
                    (session.auth.user_id, target["id"]),
                )
                await db.commit()
            except Exception:
                pass

    # ── Post message ──────────────────────────────────────────────────────────

    async def _post_message(
        self,
        session: "BBSSession",
        area_id: Optional[int],
        area_name: str,
    ) -> None:
        term = session.term
        db = session.db

        # Auth check — must be authenticated to post
        if not session.auth.is_authenticated:
            await term.sendln(
                "AUTH required to post. Type 'A' at main menu to authenticate."
            )
            return

        # Area selection if none chosen yet
        if not area_id:
            area_id, area_name = await self._list_areas(session)
        if not area_id:
            return

        # Gather subject
        await term.send(f"Subject ({self._max_subject} chars max): ")
        subject = (await term.readline(max_len=self._max_subject)).strip()
        if not subject:
            await term.sendln("Cancelled.")
            return

        # Gather To: (default ALL)
        await term.send("To [ALL]: ")
        to_call = (await term.readline(max_len=10)).upper().strip() or "ALL"

        # Gather body — blank line ends input
        await term.sendln(f"Enter message body. Blank line to finish ({self._max_body} bytes max):")
        body_lines = []
        total_bytes = 0
        while True:
            await term.send("> ")
            line = await term.readline(max_len=80)
            if not line:
                break
            total_bytes += len(line) + 1
            if total_bytes > self._max_body:
                await term.sendln("[Body limit reached]")
                break
            body_lines.append(line)

        body = "\n".join(body_lines)

        # Confirm
        await term.send("Post message? [Y/N]: ")
        confirm = (await term.readline(max_len=2)).upper().strip()
        if confirm != "Y":
            await term.sendln("Cancelled.")
            return

        db.row_factory = aiosqlite.Row
        # Next message number in this area
        async with db.execute(
            "SELECT COALESCE(MAX(msg_number),0)+1 AS next FROM bulletin_messages WHERE area_id=?",
            (area_id,),
        ) as cur:
            row = await cur.fetchone()
            next_num = row["next"] if row else 1

        await db.execute(
            """INSERT INTO bulletin_messages
               (area_id, msg_number, subject, from_call, to_call, body)
               VALUES (?,?,?,?,?,?)""",
            (area_id, next_num, subject, session.auth.callsign, to_call, body),
        )
        await db.commit()
        await term.sendln(f"Message #{next_num} posted to {area_name}.")

    # ── Delete message ────────────────────────────────────────────────────────

    async def _delete_message(
        self, session: "BBSSession", area_id: int
    ) -> None:
        term = session.term
        db = session.db

        if not session.auth.is_authenticated:
            await term.sendln("AUTH required to delete messages.")
            return

        await term.send("Delete message#: ")
        choice_str = (await term.readline(max_len=6)).strip()
        if not choice_str.isdigit():
            return

        msg_num = int(choice_str)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, from_call FROM bulletin_messages WHERE area_id=? AND msg_number=? AND deleted=0",
            (area_id, msg_num),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await term.sendln("Message not found.")
            return

        # Only own message or sysop
        if (
            row["from_call"].upper() != session.auth.callsign.upper()
            and not session.auth.is_sysop
        ):
            await term.sendln("You can only delete your own messages.")
            return

        await db.execute(
            "UPDATE bulletin_messages SET deleted=1 WHERE id=?", (row["id"],)
        )
        await db.commit()
        await term.sendln(f"Message #{msg_num} deleted.")

    # ── Web stats ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "display_name": self.display_name}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _count_unread(
    db: aiosqlite.Connection, area_id: int, user_id: Optional[int]
) -> int:
    if not user_id:
        async with db.execute(
            "SELECT COUNT(*) FROM bulletin_messages WHERE area_id=? AND deleted=0",
            (area_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async with db.execute(
        """SELECT COUNT(*) FROM bulletin_messages m
           LEFT JOIN read_receipts r ON r.message_id=m.id AND r.user_id=?
           WHERE m.area_id=? AND m.deleted=0 AND r.message_id IS NULL""",
        (user_id, area_id),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0
