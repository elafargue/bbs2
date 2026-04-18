"""
bbs/plugins/info/info.py — BBS Info ("I") plugin.

Displays a configurable BBS description / welcome text to users.
The message is stored in a single-row ``bbs_info`` table so it
survives restarts and can be edited live from the web UI without
touching the YAML config file.

Commands (from the main menu)
-----------------------------
  I  — Display the BBS info / description text
       Sysop is offered an additional "E" option to edit the message
       directly from the terminal (line-by-line, end with a single ".").

Sysop can also edit the message via the web interface at:
  GET  /api/info
  PUT  /api/info
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from bbs.core.plugin_registry import BBSPlugin

if TYPE_CHECKING:
    from bbs.core.session import BBSSession


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bbs_info (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    message TEXT    NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO bbs_info (id, message) VALUES (1, '');
"""


class InfoPlugin(BBSPlugin):
    name = "info"
    display_name = "BBS Info"
    menu_key = "I"
    min_auth_level_name = "IDENTIFIED"

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)

        import aiosqlite

        async with aiosqlite.connect(db_path, timeout=30) as db:
            await db.executescript(_SCHEMA_SQL)
            # Seed the message from config only when the DB row is still empty
            default_msg = cfg.get("message", "").strip()
            if default_msg:
                await db.execute(
                    "UPDATE bbs_info SET message = ? WHERE id = 1 AND message = ''",
                    (default_msg,),
                )
            await db.commit()

    async def handle_session(self, session: "BBSSession") -> None:
        """Display the info message; offer sysop an edit option."""
        term = session.term
        db = session.db

        async with db.execute("SELECT message FROM bbs_info WHERE id = 1") as cur:
            row = await cur.fetchone()
        message = (row[0] if row else "")

        if not message:
            await term.sendln("No BBS info message has been configured yet.")
        else:
            await term.paginate(message.splitlines())

        if not session.auth.is_sysop:
            return

        # Sysop: offer to edit
        await term.sendln("")
        await term.send("Edit info message? [Y/N]: ")
        choice = (await term.readline(max_len=2, timeout=60)).upper().strip()
        if choice != "Y":
            return

        await self._edit_message(session)

    async def _edit_message(self, session: "BBSSession") -> None:
        """Line-by-line editor — sysop enters text, ends with a lone '.'."""
        term = session.term
        db = session.db

        await term.sendln("Enter new info message.")
        await term.sendln("Type each line and press ENTER.  Type '/EX' to finish,")
        await term.sendln("or '/ABORT' on the first line to cancel.")
        await term.sendln("")

        lines: list[str] = []
        while True:
            await term.send(f"{len(lines) + 1:2}: ")
            line = await term.readline(max_len=200, timeout=120)
            if len(lines) == 0 and line.upper().strip() == "/ABORT":
                await term.sendln("Aborted — no changes made.")
                return
            if line.upper().strip() == "/EX":
                break
            lines.append(line)

        new_message = "\n".join(lines)
        await db.execute("UPDATE bbs_info SET message = ? WHERE id = 1", (new_message,))
        await db.commit()
        await term.sendln("Info message updated.")

    def get_stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "enabled": self.enabled,
        }
