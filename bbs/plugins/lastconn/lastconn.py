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
from bbs.core.session import PathLength
from bbs.db.connections import get_recent_connections

if TYPE_CHECKING:
    from bbs.core.session import BBSSession

_AUTH_LABELS = {0: "anon", 1: "ident", 2: "auth", 3: "sysop"}


class LastConnectionsPlugin(BBSPlugin):
    name = "lastconn"
    display_name = "Last Connections"
    menu_key = "LC"
    help_text = "Recent connection log showing callsign, transport, and time."
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
            await term.sendln(term.note("No connections recorded yet."))
            await term.sendln()
            return

        # Compact rendering on long RF paths: fewer rows, narrower timestamps,
        # and drop the lowest-value columns (Auth, then Transport).
        pl = session.path_length
        total_rows = len(rows)
        compact_limit = 10 if pl is PathLength.LONG else 20 if pl is PathLength.MEDIUM else None
        truncated = compact_limit is not None and total_rows > compact_limit
        if compact_limit is not None:
            rows = rows[:compact_limit]

        first_date_only = pl is not PathLength.SHORT   # MEDIUM and LONG: date only
        last_date_only  = pl is PathLength.LONG        # LONG only
        show_transport  = pl is not PathLength.LONG
        show_auth       = pl is PathLength.SHORT

        col_call   = 9
        col_first  = 10 if first_date_only else 16
        col_last   = 10 if last_date_only  else 16
        col_trn    = 12
        first_fmt  = "%Y-%m-%d" if first_date_only else "%Y-%m-%d %H:%M"
        last_fmt   = "%Y-%m-%d" if last_date_only  else "%Y-%m-%d %H:%M"
        active_lbl = "* Active *" if last_date_only else "** Active **"

        if truncated:
            header = f"LAST CONNECTIONS  (last {len(rows)} stations)"
        else:
            header = f"LAST CONNECTIONS  (past {days} days, {len(rows)} stations)"
        await term.sendln(term.label(header, "meta"))
        await term.sendln(term.note("-" * min(len(header), term.width)))

        lines = []
        for row in rows:
            call = str(row["callsign"]).upper().ljust(col_call)[:col_call]
            first = time.strftime(
                first_fmt, time.localtime(row["first_seen"])
            ).ljust(col_first)[:col_first]
            if row.get("connected"):
                last = active_lbl.ljust(col_last)[:col_last]
                last_disp = term.ok(last)
                call_disp = term.style(call, "accent", bold=True)
            else:
                last = time.strftime(
                    last_fmt, time.localtime(row["last_seen"])
                ).ljust(col_last)[:col_last]
                last_disp = term.note(last)
                call_disp = call
            parts = [call_disp, term.note(first), last_disp]
            if show_transport:
                parts.append(str(row["transport"]).ljust(col_trn)[:col_trn])
            if show_auth:
                parts.append(_AUTH_LABELS.get(row["auth_level"], "?"))
            lines.append(" ".join(parts))

        # Column header
        hdr_parts = [
            f"{'CALLSIGN':<{col_call}}",
            f"{'FIRST SEEN':<{col_first}}",
            f"{'LAST SEEN':<{col_last}}",
        ]
        if show_transport:
            hdr_parts.append(f"{'TRANSPORT':<{col_trn}}")
        if show_auth:
            hdr_parts.append("AUTH")
        col_hdr = " ".join(hdr_parts)

        await term.sendln(term.label(col_hdr, "meta"))
        await term.sendln(term.note("-" * min(len(col_hdr), term.width)))
        await term.flush()

        await term.paginate(lines, timeout=float(session.cfg.idle_timeout) or None)
        await term.sendln()
