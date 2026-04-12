"""
bbs/plugins/lastconn/lastconn.py — Last Connections plugin.

Displays a paginated list of the recent connection journal: who connected,
when they first appeared, when they were last seen, and what access level
they reached.

Access: IDENTIFIED (any station with a callsign can see the list).
"""
from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from bbs.core.auth import AuthLevel
from bbs.core.plugin_registry import BBSPlugin
from bbs.db.connections import get_recent_connections

if TYPE_CHECKING:
    from bbs.core.session import BBSSession

_AUTH_LABELS = {0: "anon", 1: "ident", 2: "auth", 3: "sysop"}


def _fmt_ts(ts: int) -> str:
    """Format a Unix timestamp as a compact local datetime string."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class LastConnectionsPlugin(BBSPlugin):
    name = "lastconn"
    display_name = "Last Connections"
    menu_key = "LC"
    min_auth_level_name = "IDENTIFIED"

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)

    async def handle_session(self, session: "BBSSession") -> None:
        term = session.term

        days = session.cfg.connection_log_days or 30
        rows = await get_recent_connections(
            str(session.cfg.db_path),
            days=days,
            limit=int(self._cfg.get("limit", 200)),
        )

        if not rows:
            await term.sendln("No connections recorded yet.")
            await term.sendln()
            return

        header = f"LAST CONNECTIONS  (past {days} days, {len(rows)} stations)"
        await term.sendln(header)
        await term.sendln("-" * min(len(header), term.width))

        lines = []
        col_call = 9
        col_ts = 16
        col_trn = 12
        for row in rows:
            call = str(row["callsign"]).upper().ljust(col_call)[:col_call]
            first = _fmt_ts(row["first_seen"]).ljust(col_ts)[:col_ts]
            last = (
                "** Active **".ljust(col_ts)[:col_ts]
                if row.get("connected")
                else _fmt_ts(row["last_seen"]).ljust(col_ts)[:col_ts]
            )
            trn = str(row["transport"]).ljust(col_trn)[:col_trn]
            lvl = _AUTH_LABELS.get(row["auth_level"], "?")
            lines.append(f"{call} {first} {last} {trn} {lvl}")

        # Column header
        col_hdr = (
            f"{'CALLSIGN':<{col_call}} "
            f"{'FIRST SEEN':<{col_ts}} "
            f"{'LAST SEEN':<{col_ts}} "
            f"{'TRANSPORT':<{col_trn}} AUTH"
        )
        await term.sendln(col_hdr)
        await term.sendln("-" * min(len(col_hdr), term.width))
        await term.flush()

        await term.paginate(lines)
        await term.sendln()
