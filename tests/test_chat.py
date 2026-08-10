"""
tests/test_chat.py — Integration tests for the Chat plugin.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

import tempfile
from pathlib import Path

from bbs.plugins.chat.chat import ChatPlugin, _compact_history, _rooms, get_or_create_room
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle


async def _boot_plugin(db_path: str) -> ChatPlugin:
    """Initialize a ChatPlugin against *db_path*, as a BBS start-up would."""
    plugin = ChatPlugin()
    await plugin.initialize(
        {
            "enabled": True,
            "history_lines": 10,
            "default_rooms": [{"name": "main", "description": "Main chat room"}],
        },
        db_path,
    )
    return plugin


async def _register_user(bbs_server: _BbsServerHandle, callsign: str) -> None:
    """Log in once and leave, so *callsign* exists in the users table."""
    async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
        await client.do_login(callsign)
        await client.sendln("B")


def _db_path(bbs_server: _BbsServerHandle) -> str:
    return str(bbs_server.engine.cfg.db_path)


class TestChatEntry:
    async def test_enter_chat_shows_room_name(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        text = await logged_in_client.wait_for("main>")
        assert "main" in text.lower()

    async def test_enter_chat_shows_who_is_present(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        text = await logged_in_client.wait_for("main>")
        # Our own callsign should appear in the user list
        assert "W1TEST" in text.upper()

    async def test_enter_chat_shows_commands_help(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        text = await logged_in_client.wait_for("main>")
        assert "/QUIT" in text


class TestChatCommands:
    async def test_who_command(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/WHO")
        text = await logged_in_client.wait_for("main>")
        assert "W1TEST" in text.upper()

    async def test_quit_returns_to_main_menu(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/QUIT")
        text = await logged_in_client.wait_for(">")
        # Back at main menu — full menu: "[B]"/"[C]"; compact menu: "..., C, ..."
        assert "[B]" in text or "[C]" in text or "[Q]" in text or ", C," in text or ", B," in text

    async def test_unknown_slash_command(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/NOTACOMMAND")
        text = await logged_in_client.wait_for("main>")
        assert "Unknown" in text or "unknown" in text

    async def test_rooms_command(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/ROOMS")
        text = await logged_in_client.wait_for("main>")
        assert "main" in text.lower()

    async def test_join_new_room(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/JOIN testroom")
        text = await logged_in_client.wait_for("testroom>")
        assert "testroom" in text.lower()

    async def test_msg_to_nonexistent_user(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/MSG W9NOBODY Hello there")
        text = await logged_in_client.wait_for("main>")
        assert "not in this room" in text or "W9NOBODY" in text


class TestChatBroadcast:
    async def test_two_users_can_exchange_messages(
        self, bbs_server: _BbsServerHandle
    ):
        """Alice sends a message; Bob should receive it."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
            async with BbsTestClient(bbs_server.host, bbs_server.port) as bob:
                # Both log in
                await alice.do_login("W1ALICE")
                await bob.do_login("W1BOB")

                # Both enter chat
                await alice.sendln("C")
                await alice.wait_for("main>")
                await bob.sendln("C")
                await bob.wait_for("main>")

                # Alice says something
                await alice.sendln("Hello Bob!")

                # Bob should see it within a reasonable timeout
                text = await bob.wait_for("W1ALICE", timeout=5.0)
                assert "Hello Bob" in text or "W1ALICE" in text

                # Clean up
                await alice.sendln("/QUIT")
                await bob.sendln("/QUIT")

    async def test_join_notification_visible_to_existing_member(
        self, bbs_server: _BbsServerHandle
    ):
        """When Charlie joins, Alice (already in the room) gets a join notification."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
            async with BbsTestClient(bbs_server.host, bbs_server.port) as charlie:
                await alice.do_login("W1ALICEJ")
                await alice.sendln("C")
                await alice.wait_for("main>")

                await charlie.do_login("W1CHARLIE")
                await charlie.sendln("C")
                await charlie.wait_for("main>")

                # Alice's buffer should contain join notice
                text = await alice.wait_for("W1CHARLIE", timeout=5.0)
                assert "W1CHARLIE" in text.upper()

                await alice.sendln("/QUIT")
                await charlie.sendln("/QUIT")


class TestRoomPersistence:
    """Rooms users create with /JOIN must outlive a restart."""

    async def test_room_with_history_is_restored_at_startup(self):
        tmp = tempfile.mkdtemp(prefix="bbs2_chat_persist_")
        db_path = str(Path(tmp) / "test.db")
        saved = dict(_rooms)  # the registry is shared process-wide
        _rooms.clear()
        try:
            await _boot_plugin(db_path)
            get_or_create_room("userroom")          # as /JOIN userroom would
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT INTO chat_history (room, ts, line) VALUES (?,?,?)",
                    ("userroom", 0, "[10:00] W1AAA: hi"),
                )
                await db.commit()

            _rooms.clear()                          # restart
            await _boot_plugin(db_path)

            assert "userroom" in _rooms
            assert _rooms["userroom"].get_history() == ["[10:00] W1AAA: hi"]
        finally:
            _rooms.clear()
            _rooms.update(saved)

    async def test_room_without_history_is_not_resurrected(self):
        tmp = tempfile.mkdtemp(prefix="bbs2_chat_persist_")
        db_path = str(Path(tmp) / "test.db")
        saved = dict(_rooms)
        _rooms.clear()
        try:
            await _boot_plugin(db_path)
            get_or_create_room("ephemeral")         # joined, nothing ever said
            _rooms.clear()
            await _boot_plugin(db_path)
            assert "ephemeral" not in _rooms
            assert "main" in _rooms                 # config rooms always exist
        finally:
            _rooms.clear()
            _rooms.update(saved)


class TestPendingNotice:
    """Waiting messages are announced on the main menu at login."""

    async def test_held_chat_message_is_announced(self, bbs_server: _BbsServerHandle):
        await _register_user(bbs_server, "W1WAIT")
        bulletins = bbs_server.engine.plugin_registry.get("bulletins")
        bulletins.enabled = False
        try:
            async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
                await alice.do_login("W1SND3")
                await alice.sendln("C")
                await alice.wait_for("main>")
                await alice.sendln("/MSG W1WAIT ping")
                await alice.wait_for("main>", timeout=5.0)
                await alice.sendln("/QUIT")
        finally:
            bulletins.enabled = True

        async with BbsTestClient(bbs_server.host, bbs_server.port) as bob:
            menu = await bob.do_login("W1WAIT")
            assert "chat message" in menu
            assert "waiting" in menu

    async def test_unread_bulletin_is_announced(self, bbs_server: _BbsServerHandle):
        await _register_user(bbs_server, "W1UNRD")
        async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
            await alice.do_login("W1SND4")
            await alice.sendln("C")
            await alice.wait_for("main>")
            await alice.sendln("/MSG W1UNRD meet on 145.010")
            await alice.wait_for("main>", timeout=5.0)
            await alice.sendln("/QUIT")

        async with BbsTestClient(bbs_server.host, bbs_server.port) as bob:
            menu = await bob.do_login("W1UNRD")
            assert "unread message" in menu

    async def test_no_notice_when_nothing_is_waiting(self, bbs_server: _BbsServerHandle):
        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            menu = await client.do_login("W1QUIET")
            assert "waiting" not in menu
            assert "unread message" not in menu


class TestReplyCommand:
    """/R answers whoever whispered last."""

    async def test_reply_reaches_the_last_sender(self, bbs_server: _BbsServerHandle):
        async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
            async with BbsTestClient(bbs_server.host, bbs_server.port) as bob:
                await alice.do_login("W1RPA")
                await bob.do_login("W1RPB")
                await alice.sendln("C")
                await alice.wait_for("main>")
                await bob.sendln("C")
                await bob.wait_for("main>")

                await alice.sendln("/MSG W1RPB you there?")
                await bob.wait_for("you there?", timeout=5.0)

                await bob.sendln("/R yes, go ahead with two words")

                text = await alice.wait_for("two words", timeout=5.0)
                assert "W1RPB" in text.upper()
                assert "yes, go ahead with two words" in text

                await alice.sendln("/QUIT")
                await bob.sendln("/QUIT")

    async def test_reply_without_a_sender_is_refused(
        self, logged_in_client: BbsTestClient
    ):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/R hello?")
        text = await logged_in_client.wait_for("main>")
        assert "Nobody has messaged you yet" in text


class TestHistoryCompaction:
    """Scrollback collapses join/leave runs into one 'visited' line."""

    def test_run_between_messages_becomes_one_line(self):
        rows = [
            (1, "[10:00] W1AAA: morning"),
            (2, "*** W1BBB joined main ***"),
            (3, "*** W1BBB left main ***"),
            (4, "*** W1CCC joined main ***"),
            (5, "[10:05] W1AAA: still here"),
        ]
        out = _compact_history(rows)
        assert out == [
            (1, "[10:00] W1AAA: morning"),
            (None, "*** visited: W1BBB, W1CCC ***"),
            (5, "[10:05] W1AAA: still here"),
        ]

    def test_trailing_run_is_emitted(self):
        rows = [
            (1, "[10:00] W1AAA: morning"),
            (2, "*** W1BBB joined main ***"),
        ]
        out = _compact_history(rows)
        assert out[-1] == (None, "*** visited: W1BBB ***")

    def test_reader_is_left_out_of_the_summary(self):
        rows = [
            (1, "*** W1AAA joined main ***"),
            (2, "*** W1BBB joined main ***"),
        ]
        assert _compact_history(rows, exclude="W1AAA") == [
            (None, "*** visited: W1BBB ***")
        ]

    def test_summary_omitted_when_only_the_reader_visited(self):
        rows = [(1, "*** W1AAA joined main ***")]
        assert _compact_history(rows, exclude="W1AAA") == []

    def test_other_system_lines_are_kept(self):
        rows = [
            (1, "*** Room old has been deleted by the sysop. Use /JOIN to switch rooms. ***"),
            (2, "[10:00] W1AAA: hi"),
        ]
        assert _compact_history(rows) == rows

    def test_plain_history_is_unchanged(self):
        rows = [(1, "[10:00] W1AAA: hi"), (2, "[10:01] W1BBB: hi back")]
        assert _compact_history(rows) == rows

    async def test_join_replays_the_new_room_history(
        self, bbs_server: _BbsServerHandle
    ):
        async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
            await alice.do_login("W1HRA")
            await alice.sendln("C")
            await alice.wait_for("main>")
            await alice.sendln("/JOIN histroom")
            await alice.wait_for("histroom>")
            await alice.sendln("earlier traffic here")
            await alice.wait_for("histroom>")
            await alice.sendln("/QUIT")
            await alice.wait_for(">")
        await asyncio.sleep(0.3)  # let the persist task land

        async with BbsTestClient(bbs_server.host, bbs_server.port) as bob:
            await bob.do_login("W1HRB")
            await bob.sendln("C")
            await bob.wait_for("main>")
            await bob.sendln("/JOIN histroom")
            text = await bob.wait_for("histroom>", timeout=5.0)
            await bob.sendln("/QUIT")

        assert "--- recent ---" in text
        assert "earlier traffic here" in text

    async def test_join_to_empty_room_replays_nothing(
        self, bbs_server: _BbsServerHandle
    ):
        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            await client.do_login("W1EMPT")
            await client.sendln("C")
            await client.wait_for("main>")
            await client.sendln("/JOIN quietroom")
            text = await client.wait_for("quietroom>", timeout=5.0)
            await client.sendln("/QUIT")

        assert "--- recent ---" not in text.split("Joined room:")[-1]

    async def test_entry_scrollback_is_compacted(self, bbs_server: _BbsServerHandle):
        """Fill main's scrollback with churn, then check what a new user sees."""
        for n in range(5):  # 5 x (join + leave) = 10 lines = the whole window
            async with BbsTestClient(bbs_server.host, bbs_server.port) as churn:
                await churn.do_login(f"W1CH{n}")
                await churn.sendln("C")
                await churn.wait_for("main>")
                await churn.sendln("/QUIT")
                await churn.wait_for(">")
        await asyncio.sleep(0.3)  # let the persist tasks land

        async with BbsTestClient(bbs_server.host, bbs_server.port) as newcomer:
            await newcomer.do_login("W1NEW")
            await newcomer.sendln("C")
            text = await newcomer.wait_for("main>", timeout=5.0)
            await newcomer.sendln("/QUIT")

        scrollback = text.split("--- recent ---")[-1].split("--- end ---")[0]
        assert "visited:" in scrollback
        assert "joined main" not in scrollback
        assert "left main" not in scrollback
        assert "W1CH4" in scrollback       # churners are named once
        assert "W1NEW" not in scrollback   # the reader is not their own visitor


class TestPrivateMsg:
    """/MSG delivery: live where possible, stored where not."""

    async def test_msg_to_self_is_refused(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/MSG W1TEST Hello me")
        text = await logged_in_client.wait_for("main>")
        assert "cannot /MSG yourself" in text

    async def test_msg_to_unknown_callsign_is_not_stored(
        self, logged_in_client: BbsTestClient, bbs_server: _BbsServerHandle
    ):
        await logged_in_client.sendln("C")
        await logged_in_client.wait_for("main>")
        await logged_in_client.sendln("/MSG W9NOBODY Hello there")
        text = await logged_in_client.wait_for("main>")
        assert "not a known user" in text
        assert "nothing sent" in text

        async with aiosqlite.connect(_db_path(bbs_server)) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM chat_offline_msgs WHERE to_call='W9NOBODY'"
            ) as cur:
                assert (await cur.fetchone())[0] == 0

    async def test_msg_reaches_recipient_in_another_room(
        self, bbs_server: _BbsServerHandle
    ):
        """A whisper follows the recipient into whatever room they are in."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
            async with BbsTestClient(bbs_server.host, bbs_server.port) as bob:
                await alice.do_login("W1XRA")
                await bob.do_login("W1XRB")
                await alice.sendln("C")
                await alice.wait_for("main>")
                await bob.sendln("C")
                await bob.wait_for("main>")
                await bob.sendln("/JOIN elsewhere")
                await bob.wait_for("elsewhere>")

                await alice.sendln("/MSG W1XRB psst")

                bob_text = await bob.wait_for("psst", timeout=5.0)
                assert "W1XRA" in bob_text.upper()
                alice_text = await alice.wait_for("main>", timeout=5.0)
                assert "elsewhere" in alice_text  # sender told where it landed

                await alice.sendln("/QUIT")
                await bob.sendln("/QUIT")

    async def test_msg_to_offline_user_becomes_a_bulletin(
        self, bbs_server: _BbsServerHandle
    ):
        await _register_user(bbs_server, "W1OFFL")

        async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
            await alice.do_login("W1SNDR")
            await alice.sendln("C")
            await alice.wait_for("main>")
            await alice.sendln("/MSG W1OFFL see you at the net")
            text = await alice.wait_for("main>", timeout=5.0)
            assert "not in chat" in text
            assert "message" in text and "#" in text
            await alice.sendln("/QUIT")

        async with aiosqlite.connect(_db_path(bbs_server)) as db:
            async with db.execute(
                "SELECT from_call, subject, body FROM bulletin_messages"
                " WHERE to_call='W1OFFL' COLLATE NOCASE"
            ) as cur:
                rows = await cur.fetchall()
        assert len(rows) == 1
        from_call, subject, body = rows[0]
        assert from_call.upper() == "W1SNDR"
        assert "W1SNDR" in subject
        assert "see you at the net" in body

    async def test_msg_is_held_in_chat_when_bulletins_disabled(
        self, bbs_server: _BbsServerHandle
    ):
        """With no bulletins plugin to take it, the whisper waits in chat."""
        await _register_user(bbs_server, "W1HELD")
        bulletins = bbs_server.engine.plugin_registry.get("bulletins")
        assert bulletins is not None
        bulletins.enabled = False
        try:
            async with BbsTestClient(bbs_server.host, bbs_server.port) as alice:
                await alice.do_login("W1SND2")
                await alice.sendln("C")
                await alice.wait_for("main>")
                await alice.sendln("/MSG W1HELD tnx for the qso")
                text = await alice.wait_for("main>", timeout=5.0)
                assert "held" in text
                await alice.sendln("/QUIT")
        finally:
            bulletins.enabled = True

        # W1HELD sees it on their next visit to chat, and only once.
        async with BbsTestClient(bbs_server.host, bbs_server.port) as bob:
            await bob.do_login("W1HELD")
            await bob.sendln("C")
            text = await bob.wait_for("main>", timeout=5.0)
            assert "tnx for the qso" in text
            assert "W1SND2" in text.upper()
            await bob.sendln("/QUIT")

        async with aiosqlite.connect(_db_path(bbs_server)) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM chat_offline_msgs WHERE to_call='W1HELD'"
            ) as cur:
                assert (await cur.fetchone())[0] == 0
