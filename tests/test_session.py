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


class TestMenuBandwidthMode:
    """First display is full; subsequent displays are compact; bare Enter redraws full."""

    async def test_first_display_is_full_menu(self, bbs_server: _BbsServerHandle):
        """After login the full menu must show item descriptions."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as client:
            text = await client.do_login("W1TMBD1")
            # Full menu contains descriptions like "Bulletins" or "Chat"
            assert "Bulletins" in text or "Chat" in text

    async def test_second_display_is_compact(self, logged_in_client: BbsTestClient):
        """After the first menu, returning from a command should show the
        compact (keys-only) menu."""
        # Send an unknown command to cycle back to the menu prompt
        await logged_in_client.sendln("Z")
        text = await logged_in_client.wait_for(">")
        # Compact menu: descriptions like "Bulletins" or "Chat" should NOT appear,
        # but the keys should be present in the comma-separated list.
        assert "Bulletins" not in text
        assert "Chat" not in text
        assert "B" in text and "C" in text

    async def test_bare_enter_redraws_full_menu(self, logged_in_client: BbsTestClient):
        """Pressing Enter (empty input) at the compact menu redraws the full menu."""
        # Cycle past the first (full) menu
        await logged_in_client.sendln("Z")
        await logged_in_client.wait_for(">")
        # Now on compact menu — send bare Enter
        await logged_in_client.sendln("")
        text = await logged_in_client.wait_for(">")
        # Full menu has item descriptions
        assert "Bulletins" in text or "Chat" in text

    async def test_bare_enter_does_not_disconnect(self, logged_in_client: BbsTestClient):
        """Pressing Enter must never disconnect the user."""
        await logged_in_client.sendln("")
        # If disconnected we would never get the prompt back
        text = await logged_in_client.wait_for(">")
        assert ">" in text

    async def test_plugin_first_entry_is_full_menu(self, logged_in_client: BbsTestClient):
        """Entering a plugin for the first time shows the full menu."""
        await logged_in_client.sendln("BU")
        text = await logged_in_client.wait_for("choice:")
        # Full plugin menu must contain descriptions
        assert "Areas" in text or "List messages" in text or "Send" in text

    async def test_plugin_second_loop_is_compact(self, logged_in_client: BbsTestClient):
        """Within a plugin visit, the second menu display is compact."""
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for("choice:")  # consume full menu
        # Send unknown command so plugin loops back to its menu
        await logged_in_client.sendln("Z")
        text = await logged_in_client.wait_for("choice:")
        # Compact: descriptions gone, keys present
        assert "List messages" not in text
        assert "Areas" not in text
        # Keys like A, L, S, Q should be in the comma list
        assert "A" in text and "Q" in text

    async def test_plugin_reentry_is_full_menu(self, logged_in_client: BbsTestClient):
        """Re-entering a plugin after returning to main menu shows the full menu again."""
        # First visit
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for("choice:")
        # Exit plugin back to main menu
        await logged_in_client.sendln("Q")
        await logged_in_client.wait_for(">")
        # Re-enter
        await logged_in_client.sendln("BU")
        text = await logged_in_client.wait_for("choice:")
        # Should be full again
        assert "Areas" in text or "List messages" in text or "Send" in text
