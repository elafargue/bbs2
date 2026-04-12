"""
bbs/db/users.py — User CRUD operations.

All functions take a db_path string (or an open aiosqlite.Connection) and
operate asynchronously.  The auth service and plugins import from here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite


@dataclass
class User:
    id: int
    callsign: str
    name: str
    qth: str
    approved: bool
    banned: bool
    totp_secret: Optional[bytes]
    otp_type: str            # 'totp' | 'hotp'
    hotp_counter: int
    auth_failures: int
    locked_until: Optional[int]
    created_at: int
    last_seen: Optional[int]

    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        return int(time.time()) < self.locked_until

    @property
    def has_secret(self) -> bool:
        return self.totp_secret is not None and len(self.totp_secret) > 0


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        callsign=row["callsign"],
        name=row["name"],
        qth=row["qth"],
        approved=bool(row["approved"]),
        banned=bool(row["banned"]),
        totp_secret=row["totp_secret"],
        otp_type=row["otp_type"] or "totp",
        hotp_counter=row["hotp_counter"] or 0,
        auth_failures=row["auth_failures"],
        locked_until=row["locked_until"],
        created_at=row["created_at"],
        last_seen=row["last_seen"],
    )


async def get_or_create(db: aiosqlite.Connection, callsign: str) -> tuple[User, bool]:
    """
    Fetch the user record for *callsign*, creating a pending account if
    none exists.  Returns (user, created).
    """
    callsign = callsign.upper().strip()
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM users WHERE callsign = ? COLLATE NOCASE", (callsign,)
    ) as cur:
        row = await cur.fetchone()

    if row:
        return _row_to_user(row), False

    await db.execute(
        "INSERT INTO users (callsign, approved) VALUES (?, 0)",
        (callsign,),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM users WHERE callsign = ? COLLATE NOCASE", (callsign,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return _row_to_user(row), True


async def get_by_callsign(
    db: aiosqlite.Connection, callsign: str
) -> Optional[User]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM users WHERE callsign = ? COLLATE NOCASE",
        (callsign.upper().strip(),),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def get_by_id(db: aiosqlite.Connection, user_id: int) -> Optional[User]:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def list_users(
    db: aiosqlite.Connection,
    pending_only: bool = False,
) -> list[User]:
    db.row_factory = aiosqlite.Row
    if pending_only:
        sql = "SELECT * FROM users WHERE approved = 0 ORDER BY created_at"
    else:
        sql = "SELECT * FROM users ORDER BY callsign COLLATE NOCASE"
    async with db.execute(sql) as cur:
        rows = await cur.fetchall()
    return [_row_to_user(r) for r in rows]


async def update_last_seen(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET last_seen = ? WHERE id = ?",
        (int(time.time()), user_id),
    )
    await db.commit()


async def set_approved(
    db: aiosqlite.Connection, user_id: int, approved: bool
) -> None:
    await db.execute(
        "UPDATE users SET approved = ? WHERE id = ?",
        (int(approved), user_id),
    )
    await db.commit()


async def set_banned(
    db: aiosqlite.Connection, user_id: int, banned: bool
) -> None:
    await db.execute(
        "UPDATE users SET banned = ? WHERE id = ?",
        (int(banned), user_id),
    )
    await db.commit()


async def set_otp_secret(
    db: aiosqlite.Connection, user_id: int, secret: bytes, otp_type: str = "totp"
) -> None:
    """Store the raw OTP secret bytes and type for a user (called by web UI)."""
    await db.execute(
        "UPDATE users SET totp_secret = ?, otp_type = ?, hotp_counter = 0 WHERE id = ?",
        (secret, otp_type, user_id),
    )
    await db.commit()


async def clear_otp_secret(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET totp_secret = NULL, hotp_counter = 0 WHERE id = ?", (user_id,)
    )
    await db.commit()


async def increment_hotp_counter(db: aiosqlite.Connection, user_id: int, new_counter: int) -> None:
    """Advance the HOTP counter to *new_counter* after a successful verification."""
    await db.execute(
        "UPDATE users SET hotp_counter = ? WHERE id = ?",
        (new_counter, user_id),
    )
    await db.commit()


async def record_auth_failure(
    db: aiosqlite.Connection, user_id: int, max_attempts: int, lockout_seconds: int
) -> int:
    """
    Increment auth failure count.  If failures reach *max_attempts*, apply
    lockout.  Returns the new failure count.
    """
    async with db.execute(
        "SELECT auth_failures FROM users WHERE id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    failures = (row[0] if row else 0) + 1

    locked_until: Optional[int] = None
    if failures >= max_attempts:
        locked_until = int(time.time()) + lockout_seconds

    await db.execute(
        "UPDATE users SET auth_failures = ?, locked_until = ? WHERE id = ?",
        (failures, locked_until, user_id),
    )
    await db.commit()
    return failures


async def reset_auth_failures(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET auth_failures = 0, locked_until = NULL WHERE id = ?",
        (user_id,),
    )
    await db.commit()


async def update_profile(
    db: aiosqlite.Connection, user_id: int, name: str, qth: str
) -> None:
    await db.execute(
        "UPDATE users SET name = ?, qth = ? WHERE id = ?",
        (name.strip(), qth.strip(), user_id),
    )
    await db.commit()
