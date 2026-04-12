"""
tests/test_session.py — Integration tests for session lifecycle and navigation.
"""
from __future__ import annotations

import pytest
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle


class TestGreeting:
    async def test_greeting_shows_bbs_name(self, bbs_client: BbsTestClient):
        text = await bbs_client.wait_for("Callsign:")
        assert "Test BBS" in text

    async def test_greeting_shows_sysop(self, bbs_client: BbsTestClient):
        text = await bbs_client.wait_for("Callsign:")
        assert "W1TEST" in text


class TestIdentification:
    async def test_identify_creates_session(self, bbs_client: BbsTestClient):
        await bbs_client.wait_for("Callsign:")
        await bbs_client.sendln("W1CALLER")
        text = await bbs_client.wait_for(">")
        assert "W1CALLER" in text
        assert "Welcome" in text

    async def test_identify_shows_access_level(self, bbs_client: BbsTestClient):
        await bbs_client.wait_for("Callsign:")
        await bbs_client.sendln("W1CALLER")
        text = await bbs_client.wait_for(">")
        # "ident" or "identified" should appear
        assert "ident" in text.lower()

    async def test_empty_callsign_disconnects(self, bbs_client: BbsTestClient):
        await bbs_client.wait_for("Callsign:")
        await bbs_client.sendln("")
        text = await bbs_client.wait_for("Goodbye")
        assert "Goodbye" in text or "callsign" in text.lower()

    async def test_second_login_same_callsign_is_welcome_back(
        self, bbs_server: _BbsServerHandle
    ):
        # First login creates the account
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c1:
            await c1.wait_for("Callsign:")
            await c1.sendln("W1REPEAT")
            await c1.wait_for(">")
            await c1.quit()

        # Second login should NOT say "(New account…)"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c2:
            await c2.wait_for("Callsign:")
            await c2.sendln("W1REPEAT")
            text = await c2.wait_for(">")
            assert "New account" not in text


class TestMainMenu:
    async def test_menu_shows_bulletins(self, bbs_client: BbsTestClient):
        text = await bbs_client.do_login("W1TMENU1")
        assert "[B]" in text or "Bulletins" in text

    async def test_menu_shows_chat(self, bbs_client: BbsTestClient):
        text = await bbs_client.do_login("W1TMENU2")
        assert "[C]" in text or "Chat" in text

    async def test_menu_shows_auth(self, bbs_client: BbsTestClient):
        text = await bbs_client.do_login("W1TMENU3")
        assert "[A]" in text or "Auth" in text

    async def test_unknown_command_gives_feedback(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("Z")
        text = await logged_in_client.wait_for(">")
        assert "Unknown" in text or "unknown" in text

    async def test_quit_disconnects(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("B")
        text = await logged_in_client.wait_for("73")
        assert "73" in text
