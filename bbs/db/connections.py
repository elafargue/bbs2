"""
bbs/db/connections.py — Connection journal helpers.

One row per callsign tracking first/last connection time and the highest
authentication level the station ever reached.  Rows older than the
configured retention window are pruned at startup and periodically.
"""
from __future__ import annotations

import time
from typing import Any

import aiosqlite


async def upsert_connection(
    db_path: str,
    callsign: str,
    transport: str,
    connected_at: float,
    auth_level: int,
    connected: int = 0,
) -> None:
    """
    Insert or update the connection record for *callsign*.

    - first_seen is set only on INSERT (first ever connection).
    - last_seen is updated to now on every call.
    - auth_level is promoted to the highest value seen.
    - transport is updated to the most recent transport used.
    - connected=1 while the session is live; caller must set 0 on disconnect.
    """
    first_seen = int(connected_at)
    last_seen = int(time.time())
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.execute(
            """
            INSERT INTO connection_log
                (callsign, transport, first_seen, last_seen, auth_level, connected)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(callsign) DO UPDATE SET
                transport  = excluded.transport,
                last_seen  = excluded.last_seen,
                auth_level = MAX(auth_level, excluded.auth_level),
                connected  = excluded.connected
            """,
            (callsign.upper(), transport, first_seen, last_seen, auth_level, connected),
        )
        await db.commit()


async def get_recent_connections(
    db_path: str,
    days: int,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    Return connection records whose last_seen falls within the last *days* days,
    sorted most-recent first (currently-connected stations always sort first).
    """
    cutoff = int(time.time()) - days * 86400
    async with aiosqlite.connect(db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT callsign, transport, first_seen, last_seen, auth_level, connected
            FROM connection_log
            WHERE last_seen >= ? OR connected = 1
            ORDER BY connected DESC, last_seen DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def prune_old_connections(db_path: str, days: int) -> None:
    """Delete records older than *days* days (based on last_seen)."""
    cutoff = int(time.time()) - days * 86400
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.execute(
            "DELETE FROM connection_log WHERE last_seen < ?", (cutoff,)
        )
        await db.commit()
