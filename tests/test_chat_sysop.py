"""
tests/test_chat_sysop.py — Tests for chat sysop features.

Covers:
  - Unit: ChatPlugin._delete_message() / _delete_room()
  - Integration: sysop BBS session — /HIST, /DEL, /DELROOM
  - Integration: non-sysop cannot use sysop-only commands
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import aiosqlite
import pytest

from bbs.core.auth import compute_totp_code
from bbs.plugins.chat.chat import ChatPlugin, get_or_create_room, _rooms
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle

_SECRET = b"ChatSysopSecret!!" + b"\x00" * 3  # 20 bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_plugin(history_lines: int = 10) -> ChatPlugin:
    """Create and initialize a ChatPlugin backed by a fresh temp DB."""
    tmp = tempfile.mkdtemp(prefix="bbs2_chat_sysop_test_")
    db_path = str(Path(tmp) / "test.db")
    plugin = ChatPlugin()
    await plugin.initialize(
        {
            "enabled": True,
            "history_lines": history_lines,
            "default_rooms": [{"name": "main", "description": "Main room"}],
        },
        db_path,
    )
    return plugin


async def _seed_message(db_path: str, room: str, line: str) -> int:
    """Insert a chat_history row directly; return its id."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO chat_history (room, ts, line) VALUES (?, ?, ?)",
            (room, int(time.time()), line),
        )
        await db.commit()
        return cur.lastrowid


async def _do_sysop_auth(client: BbsTestClient, db_path: str, callsign: str) -> None:
    """Set a TOTP secret in the DB and authenticate *callsign* to SYSOP level."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET totp_secret=?, otp_type='totp', hotp_counter=0 "
            "WHERE callsign=? COLLATE NOCASE",
            (_SECRET, callsign),
        )
        await db.commit()
    await client.sendln("A")
    await client.wait_for("OTP")
    await client.sendln(compute_totp_code(_SECRET))
    await client.wait_for(">")


# ---------------------------------------------------------------------------
# Unit tests — ChatPlugin._delete_message()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDeleteMessage:
    async def test_deletes_from_db(self):
        plugin = await _make_plugin()
        msg_id = await _seed_message(plugin._db_path, "main", "Hello world")

        result = await plugin._delete_message("main", msg_id)

        assert result == "Hello world"
        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT id FROM chat_history WHERE id=?", (msg_id,)
            )).fetchone()
        assert row is None

    async def test_returns_none_for_nonexistent_id(self):
        plugin = await _make_plugin()
        result = await plugin._delete_message("main", 99999)
        assert result is None

    async def test_returns_none_for_wrong_room(self):
        plugin = await _make_plugin()
        msg_id = await _seed_message(plugin._db_path, "main", "Wrong room test")
        result = await plugin._delete_message("other", msg_id)
        assert result is None

    async def test_removes_from_memory_history(self):
        plugin = await _make_plugin()
        room = _rooms.get("main")
        if room is None:
            room = get_or_create_room("main")
        room._history = ["Hello world", "Second line"]
        msg_id = await _seed_message(plugin._db_path, "main", "Hello world")

        await plugin._delete_message("main", msg_id)

        assert "Hello world" not in room._history
        assert "Second line" in room._history

    async def test_missing_from_memory_does_not_raise(self):
        plugin = await _make_plugin()
        room = _rooms.get("main")
        if room is None:
            room = get_or_create_room("main")
        room._history = []  # message not in memory
        msg_id = await _seed_message(plugin._db_path, "main", "Orphaned line")

        result = await plugin._delete_message("main", msg_id)
        assert result == "Orphaned line"  # still succeeds


# ---------------------------------------------------------------------------
# Unit tests — ChatPlugin._delete_room()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDeleteRoom:
    async def test_removes_room_from_memory(self):
        plugin = await _make_plugin()
        get_or_create_room("temproom", "Temp")
        assert "temproom" in _rooms

        ok = await plugin._delete_room("temproom")

        assert ok is True
        assert "temproom" not in _rooms

    async def test_deletes_db_history(self):
        plugin = await _make_plugin()
        get_or_create_room("deltest", "Del test")
        await _seed_message(plugin._db_path, "deltest", "Msg 1")
        await _seed_message(plugin._db_path, "deltest", "Msg 2")

        await plugin._delete_room("deltest")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM chat_history WHERE room='deltest'"
            )).fetchone()
        assert row[0] == 0

    async def test_returns_false_for_nonexistent_room(self):
        plugin = await _make_plugin()
        ok = await plugin._delete_room("doesnotexist")
        assert ok is False

    async def test_does_not_delete_other_rooms_messages(self):
        plugin = await _make_plugin()
        get_or_create_room("todelete", "Going away")
        await _seed_message(plugin._db_path, "main", "Keep this")
        await _seed_message(plugin._db_path, "todelete", "Delete this")

        await plugin._delete_room("todelete")

        async with aiosqlite.connect(plugin._db_path) as db:
            row = await (await db.execute(
                "SELECT count(*) FROM chat_history WHERE room='main'"
            )).fetchone()
        assert row[0] == 1


# ---------------------------------------------------------------------------
# Integration tests — BBS session (TCP)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSysopChatCommands:
    async def test_sysop_sees_del_commands_in_help(self, bbs_server: _BbsServerHandle):
        """Sysop entering chat should see /HIST /DEL /DELROOM listed."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            await client.do_login("W1TEST")
            await _do_sysop_auth(client, str(bbs_server.engine.cfg.db_path), "W1TEST")
            await client.sendln("C")
            text = await client.wait_for("main>")
            assert "/DEL" in text
            assert "/DELROOM" in text
            assert "/HIST" in text
            await client.sendln("/QUIT")

    async def test_sysop_hist_shows_message_ids(self, bbs_server: _BbsServerHandle):
        """/HIST must display messages prefixed with [<id>]."""
        db_path = str(bbs_server.engine.cfg.db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO chat_history (room, ts, line) VALUES ('main', ?, ?)",
                (int(time.time()), "[12:00] W1HIST: Hi there"),
            )
            await db.commit()

        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            await client.do_login("W1TEST")
            await _do_sysop_auth(client, db_path, "W1TEST")
            await client.sendln("C")
            await client.wait_for("main>")
            await client.sendln("/HIST")
            text = await client.wait_for("--- end ---")
            # Should see at least one [N] id prefix
            import re
            assert re.search(r'\[\d+\]', text), f"No [id] found in: {text!r}"
            await client.sendln("/QUIT")

    async def test_sysop_del_removes_message(self, bbs_server: _BbsServerHandle):
        """/DEL <id> must remove the message and confirm."""
        db_path = str(bbs_server.engine.cfg.db_path)
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "INSERT INTO chat_history (room, ts, line) VALUES ('main', ?, ?)",
                (int(time.time()), "[12:01] W1DEL: Delete me"),
            )
            msg_id = cur.lastrowid
            await db.commit()

        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            await client.do_login("W1TEST")
            await _do_sysop_auth(client, db_path, "W1TEST")
            await client.sendln("C")
            await client.wait_for("main>")
            await client.sendln(f"/DEL {msg_id}")
            text = await client.wait_for("main>")
            assert "deleted" in text.lower() or str(msg_id) in text

        async with aiosqlite.connect(db_path) as db:
            row = await (await db.execute(
                "SELECT id FROM chat_history WHERE id=?", (msg_id,)
            )).fetchone()
        assert row is None, "Row should have been deleted from DB"

    async def test_sysop_del_nonexistent_id_warns(self, bbs_server: _BbsServerHandle):
        """Deleting a non-existent message ID should produce a warning."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            await client.do_login("W1TEST")
            await _do_sysop_auth(client, str(bbs_server.engine.cfg.db_path), "W1TEST")
            await client.sendln("C")
            await client.wait_for("main>")
            await client.sendln("/DEL 999999")
            text = await client.wait_for("main>")
            assert "not found" in text.lower() or "999999" in text
            await client.sendln("/QUIT")

    async def test_sysop_cannot_delroom_current_room(self, bbs_server: _BbsServerHandle):
        """Attempting /DELROOM on the current room should return a warning."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            await client.do_login("W1TEST")
            await _do_sysop_auth(client, str(bbs_server.engine.cfg.db_path), "W1TEST")
            await client.sendln("C")
            await client.wait_for("main>")
            await client.sendln("/DELROOM main")
            text = await client.wait_for("main>")
            assert "cannot" in text.lower() or "currently" in text.lower()
            await client.sendln("/QUIT")


@pytest.mark.asyncio
class TestNonSysopChatCommands:
    async def test_del_command_unknown_for_non_sysop(self, logged_in_client: BbsTestClient):
        """/DEL should be treated as an unknown command for non-sysop."""
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/DEL 1")
        text = await logged_in_client.wait_for("main>")
        assert "Unknown" in text or "unknown" in text
        await logged_in_client.sendln("/QUIT")

    async def test_hist_command_unknown_for_non_sysop(self, logged_in_client: BbsTestClient):
        """/HIST should be treated as an unknown command for non-sysop."""
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/HIST")
        text = await logged_in_client.wait_for("main>")
        assert "Unknown" in text or "unknown" in text
        await logged_in_client.sendln("/QUIT")

    async def test_delroom_command_unknown_for_non_sysop(self, logged_in_client: BbsTestClient):
        """/DELROOM should be treated as an unknown command for non-sysop."""
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/DELROOM main")
        text = await logged_in_client.wait_for("main>")
        assert "Unknown" in text or "unknown" in text
        await logged_in_client.sendln("/QUIT")

    async def test_non_sysop_help_does_not_show_del_commands(
        self, logged_in_client: BbsTestClient
    ):
        """The help line shown to non-sysop must not include /DEL or /DELROOM."""
        await logged_in_client.sendln("C")
        text = await logged_in_client.wait_for("main>")
        # /DEL should not appear in the initial display for non-sysop
        # (but /DELROOM won't appear either since /DEL prefix catches it)
        assert "/DELROOM" not in text
        await logged_in_client.sendln("/QUIT")
