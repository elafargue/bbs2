"""
bbs/db/schema.py — SQLite schema creation and migrations.

All tables are created with IF NOT EXISTS so this is safe to call on every
startup.  Version-based migrations are appended below the initial schema.
"""
from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# DDL
# ──────────────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Users ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    -- Optional display name / full name
    name            TEXT    NOT NULL DEFAULT '',
    -- QTH / location string
    qth             TEXT    NOT NULL DEFAULT '',
    -- Account state
    approved        INTEGER NOT NULL DEFAULT 0,  -- 0=pending, 1=approved
    banned          INTEGER NOT NULL DEFAULT 0,
    -- TOTP/HOTP secret stored as raw bytes (20 bytes / 160 bits, RFC 4226).
    -- Base32-encoded form is shared with the user's authenticator app.
    -- NULL means no secret set (user can identify but cannot use auth-gated features).
    totp_secret     BLOB,
    -- OTP algorithm type: 'totp' (time-based, RFC 6238) or 'hotp' (counter, RFC 4226)
    otp_type        TEXT    NOT NULL DEFAULT 'totp',
    -- HOTP counter — incremented on each successful HOTP verification
    hotp_counter    INTEGER NOT NULL DEFAULT 0,
    -- Failed auth attempt tracking
    auth_failures   INTEGER NOT NULL DEFAULT 0,
    locked_until    INTEGER,                       -- Unix timestamp or NULL
    -- Timestamps
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_seen       INTEGER
);

-- Index for fast callsign lookups (case-insensitive via COLLATE NOCASE)
CREATE INDEX IF NOT EXISTS idx_users_callsign ON users (callsign COLLATE NOCASE);

-- ── Bulletin Areas ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bulletin_areas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT    NOT NULL DEFAULT '',
    -- Minimum auth level to read: 0=anyone, 1=registered, 2=approved, 3=sysop
    read_level  INTEGER NOT NULL DEFAULT 0,
    -- Minimum auth level to post
    post_level  INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- ── Bulletin Messages ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bulletin_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id     INTEGER NOT NULL REFERENCES bulletin_areas(id) ON DELETE CASCADE,
    -- Sequential message number within this area (for user-facing references)
    msg_number  INTEGER NOT NULL,
    subject     TEXT    NOT NULL,
    from_call   TEXT    NOT NULL COLLATE NOCASE,
    -- "ALL" or a specific callsign for personal messages
    to_call     TEXT    NOT NULL DEFAULT 'ALL' COLLATE NOCASE,
    body        TEXT    NOT NULL DEFAULT '',
    parent_id   INTEGER REFERENCES bulletin_messages(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    -- Soft-delete: sysop can hide without destroying
    deleted     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bmsg_area    ON bulletin_messages (area_id, deleted, created_at);
CREATE INDEX IF NOT EXISTS idx_bmsg_tocall  ON bulletin_messages (to_call COLLATE NOCASE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bmsg_number ON bulletin_messages (area_id, msg_number);

-- ── Read Receipts ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS read_receipts (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL REFERENCES bulletin_messages(id) ON DELETE CASCADE,
    read_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (user_id, message_id)
);

-- ── Schema version ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""

_CURRENT_VERSION = 2


async def init_db(db_path: str) -> None:
    """
    Open the database, create all tables if they don't exist, and run any
    pending migrations.  Should be called once at BBS startup.
    """
    import os
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA_SQL)
        await db.commit()

        # Check current version
        async with db.execute(
            "SELECT MAX(version) FROM schema_version"
        ) as cursor:
            row = await cursor.fetchone()
            current = row[0] if row and row[0] is not None else 0

        if current < _CURRENT_VERSION:
            await _run_migrations(db, current)


async def _run_migrations(db: aiosqlite.Connection, from_version: int) -> None:
    """Apply incremental migrations from from_version to _CURRENT_VERSION."""
    if from_version < 2:
        # Add OTP columns; copy any existing hmac_secret data to totp_secret.
        for stmt in (
            "ALTER TABLE users ADD COLUMN totp_secret BLOB",
            "ALTER TABLE users ADD COLUMN otp_type TEXT NOT NULL DEFAULT 'totp'",
            "ALTER TABLE users ADD COLUMN hotp_counter INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                await db.execute(stmt)
            except Exception:
                pass  # column already exists — idempotent
        # Migrate any existing HMAC secrets into the new column
        try:
            await db.execute(
                "UPDATE users SET totp_secret = hmac_secret WHERE hmac_secret IS NOT NULL"
            )
        except Exception:
            pass
        await db.commit()
        from_version = 2

    await db.execute(
        "INSERT INTO schema_version (version) VALUES (?)", (_CURRENT_VERSION,)
    )
    await db.commit()
    logger.info("Database schema at version %d", _CURRENT_VERSION)
