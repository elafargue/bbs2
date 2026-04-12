"""
tests/test_bulletins.py — Integration tests for the Bulletins plugin.
"""
from __future__ import annotations

import aiosqlite
import pytest

from bbs.core.auth import compute_totp_code
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle

BULL_PROMPT = "Enter choice:"


async def _do_auth(client: BbsTestClient, db_path: str, callsign: str, secret: bytes) -> None:
    """Perform TOTP auth from the main menu."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET totp_secret=?, otp_type='totp', hotp_counter=0 "
            "WHERE callsign=? COLLATE NOCASE",
            (secret, callsign),
        )
        await db.commit()
    await client.sendln("A")
    await client.wait_for("OTP")
    await client.sendln(compute_totp_code(secret))
    await client.wait_for(">")  # menu redisplayed after OK

async def _select_area(client: BbsTestClient, number: int) -> None:
    """Send 'A' to list areas, pick area by number."""
    await client.sendln("A")
    await client.wait_for("area number")
    await client.sendln(str(number))
    await client.wait_for(BULL_PROMPT)


class TestBulletinsMenu:
    async def test_enter_bulletins_shows_menu(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("BU")
        text = await logged_in_client.wait_for("BULLETINS")
        assert "BULLETINS" in text

    async def test_bulletins_menu_has_list_option(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("BU")
        text = await logged_in_client.wait_for(BULL_PROMPT)
        assert "[L]" in text or "List" in text

    async def test_quit_returns_to_main_menu(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for(BULL_PROMPT)
        await logged_in_client.sendln("Q")
        text = await logged_in_client.wait_for(">")
        # Main menu shows Bye instead of Quit
        assert "[B]" in text or "Bye" in text


class TestAreasListing:
    async def test_list_shows_default_areas(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for(BULL_PROMPT)
        await logged_in_client.sendln("A")
        text = await logged_in_client.wait_for("area number")
        assert "GENERAL" in text.upper()
        assert "TECH" in text.upper()

    async def test_cancel_area_selection(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for(BULL_PROMPT)
        await logged_in_client.sendln("A")
        await logged_in_client.wait_for("area number")
        await logged_in_client.sendln("")  # empty → cancel
        await logged_in_client.wait_for(BULL_PROMPT)  # stays in bulletins menu

    async def test_invalid_area_number(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for(BULL_PROMPT)
        await logged_in_client.sendln("A")
        await logged_in_client.wait_for("area number")
        await logged_in_client.sendln("99")
        text = await logged_in_client.wait_for(BULL_PROMPT)
        assert "Invalid" in text or "invalid" in text


class TestReadMessages:
    async def test_empty_area_says_no_messages(self, logged_in_client: BbsTestClient):
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for(BULL_PROMPT)
        # Select area 1 via A, then list messages via L
        await logged_in_client.sendln("A")
        await logged_in_client.wait_for("area number")
        await logged_in_client.sendln("1")
        await logged_in_client.wait_for(BULL_PROMPT)
        await logged_in_client.sendln("L")
        text = await logged_in_client.wait_for(BULL_PROMPT)
        assert "No messages" in text or "empty" in text.lower()


class TestDefaultArea:
    async def test_db_default_area_autoselected(self, bbs_server: _BbsServerHandle):
        """DB is_default=1 causes the area to be pre-selected on entry."""
        db_path = str(bbs_server.engine.cfg.db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE bulletin_areas SET is_default=0")
            await db.execute(
                "UPDATE bulletin_areas SET is_default=1 WHERE name='TECH' COLLATE NOCASE"
            )
            await db.commit()
        try:
            async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
                await c.do_login("W1DFLT1")
                await c.sendln("BU")
                # Menu should show TECH as the current area without selecting it
                text = await c.wait_for(BULL_PROMPT)
                assert "TECH" in text
                await c.sendln("Q")
                await c.wait_for(">")
        finally:
            # Restore: clear default so other tests are unaffected
            async with aiosqlite.connect(db_path) as db:
                await db.execute("UPDATE bulletin_areas SET is_default=0")
                await db.commit()

    async def test_no_default_area_shows_none_selected(self, bbs_server: _BbsServerHandle):
        """With no is_default row and no yaml default, shows '(no area selected)'."""
        db_path = str(bbs_server.engine.cfg.db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE bulletin_areas SET is_default=0")
            await db.commit()
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login("W1DFLT2")
            await c.sendln("BU")
            text = await c.wait_for(BULL_PROMPT)
            assert "no area selected" in text.lower()
            await c.sendln("Q")
            await c.wait_for(">")


class TestPostAndRead:
    async def test_post_message_then_read_it(self, bbs_server: _BbsServerHandle):
        """Post a message as one authenticated client, read it back as another."""
        secret = b"bulletin_post_test_secret_12345x"
        callsign = "W1POSTER"

        # --- post ---
        async with BbsTestClient(bbs_server.host, bbs_server.port) as poster:
            await poster.do_login(callsign)
            await _do_auth(poster, str(bbs_server.engine.cfg.db_path), callsign, secret)
            await poster.sendln("BU")
            await poster.wait_for(BULL_PROMPT)
            await poster.sendln("A")
            await poster.wait_for("area number")
            await poster.sendln("1")
            await poster.wait_for(BULL_PROMPT)
            await poster.sendln("S")
            await poster.wait_for("Subject")
            await poster.sendln("Hello World")
            await poster.wait_for("To [")   # "To [ALL]:" prompt
            await poster.sendln("")          # accept default ALL
            await poster.wait_for("body")
            await poster.sendln("This is the test message body.")
            await poster.sendln("/EX")          # /EX to finish body
            await poster.wait_for("Post message")
            await poster.sendln("Y")
            text = await poster.wait_for(BULL_PROMPT)
            assert "posted" in text.lower() or "#" in text

        # --- read ---
        async with BbsTestClient(bbs_server.host, bbs_server.port) as reader:
            await reader.do_login("W1READER")
            await reader.sendln("BU")
            await reader.wait_for(BULL_PROMPT)
            await reader.sendln("A")
            await reader.wait_for("area number")
            await reader.sendln("1")
            await reader.wait_for(BULL_PROMPT)
            await reader.sendln("R 1")
            msg_text = await reader.wait_for("or ENTER")  # post-read prompt
            assert "Hello World" in msg_text or "test message" in msg_text.lower()
            await reader.sendln("")  # ENTER → back to bulletins menu
            await reader.wait_for(BULL_PROMPT)

    async def test_post_without_area_prompts_to_select(
        self, logged_in_client: BbsTestClient
    ):
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for(BULL_PROMPT)
        # Send without selecting area first
        await logged_in_client.sendln("S")
        # Should either prompt for area or say "select area"
        text = await logged_in_client.wait_for(BULL_PROMPT)
        assert (
            "area" in text.lower()
            or "Subject" in text
            or "AREAS" in text
        )
