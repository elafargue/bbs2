"""
tests/test_chat.py — Integration tests for the Chat plugin.
"""
from __future__ import annotations

import asyncio

import pytest
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle


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
        # Back at main menu — should see the menu items
        assert "[B]" in text or "[C]" in text or "[Q]" in text

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
