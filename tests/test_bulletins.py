"""
tests/test_bulletins.py — Integration tests for the Bulletins plugin.
"""
from __future__ import annotations

import asyncio
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


class TestPostAuthRules:
    """
    Regression tests for posting access control:
    - TCP-identified-only users must NOT be able to post (need AUTHENTICATED).
    - TCP-authenticated users CAN post and their messages get an asterisk marker.
    - Unauthenticated messages have no asterisk.
    """

    async def test_identified_only_tcp_cannot_post(
        self, logged_in_client: BbsTestClient
    ):
        """A TCP session that has NOT authenticated must be refused when posting."""
        await logged_in_client.sendln("BU")
        await logged_in_client.wait_for(BULL_PROMPT)
        # Select an area so we go straight to the post flow
        await logged_in_client.sendln("A")
        await logged_in_client.wait_for("area number")
        await logged_in_client.sendln("1")
        await logged_in_client.wait_for(BULL_PROMPT)
        await logged_in_client.sendln("S")
        text = await logged_in_client.wait_for(BULL_PROMPT)
        # Should be refused — no Subject prompt should have appeared
        assert "AUTH" in text.upper() or "auth" in text.lower()
        assert "Subject" not in text

    async def test_authenticated_tcp_can_post_with_star(
        self, bbs_server: _BbsServerHandle
    ):
        """After OTP auth, a TCP user can post and the message shows '*' next to their call."""
        secret = b"auth_post_regression_secret1234"
        callsign = "W1AUTHP"

        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _do_auth(c, str(bbs_server.engine.cfg.db_path), callsign, secret)

            await c.sendln("BU")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("A")
            await c.wait_for("area number")
            await c.sendln("1")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("S")
            await c.wait_for("Subject")
            await c.sendln("Auth post test")
            await c.wait_for("To [")
            await c.sendln("")
            await c.wait_for("body")
            await c.sendln("Body line one.")
            await c.sendln("/EX")
            await c.wait_for("Post message")
            await c.sendln("Y")
            await c.wait_for(BULL_PROMPT)

            # List messages — should see callsign with '*' suffix
            await c.sendln("L")
            text = await c.wait_for("R# /")
            assert f"{callsign}*" in text.upper()
            await c.sendln("")  # back
            await c.wait_for(BULL_PROMPT)
            await c.sendln("Q")
            await c.wait_for(">")

    async def test_unauth_message_has_no_star(
        self, bbs_server: _BbsServerHandle
    ):
        """
        Messages posted by web sessions (identified, not OTP-authenticated)
        must have no '*' in the listing.
        We simulate this by direct DB insert with authenticated=0.
        """
        callsign = "W1NOSTAR"
        db_path = str(bbs_server.engine.cfg.db_path)

        # Ensure user exists and area 1 exists, then insert an unauth message.
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (callsign, approved) VALUES (?, 1)",
                (callsign,),
            )
            area_row = await (await db.execute(
                "SELECT id FROM bulletin_areas ORDER BY id LIMIT 1"
            )).fetchone()
            assert area_row, "No bulletin areas in test DB"
            area_id = area_row[0]
            next_row = await (await db.execute(
                "SELECT COALESCE(MAX(msg_number),0)+1 FROM bulletin_messages WHERE area_id=?",
                (area_id,),
            )).fetchone()
            next_num = next_row[0]
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body, authenticated) "
                "VALUES (?,?,?,?,?,?,0)",
                (area_id, next_num, "No-star test", callsign, "ALL", "body"),
            )
            await db.commit()

        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login("W1RDNOSTAR")
            await c.sendln("BU")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("A")
            await c.wait_for("area number")
            await c.sendln("1")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("L")
            text = await c.wait_for("R# /")
            # Callsign must appear without '*'
            assert callsign.upper() in text.upper()
            assert f"{callsign}*".upper() not in text.upper()
            await c.sendln("")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("Q")
            await c.wait_for(">")


class TestFromCallColors:
    """
    Unit-level tests verifying that _show_message_index and _display_message_body
    apply the correct ANSI tones to the from_call field.

    Uses Terminal directly with a mock writer so we can inspect raw ANSI output
    without the TCP test client stripping escapes.
    """

    def _make_fake_message(self, authenticated: int) -> dict:
        return {
            "id": 1,
            "msg_number": 1,
            "from_call": "W1TEST",
            "to_call": "ALL",
            "subject": "Test subject",
            "body": "body text",
            "authenticated": authenticated,
            "created_at": 0,
        }

    async def test_authenticated_from_call_uses_success_color_in_index(self):
        """Authenticated from_call in message index must use 'success' (green) tone."""
        from bbs.core.terminal import Terminal, ColorMode, fg_rgb, BOLD, RESET

        output = bytearray()

        class _FakeWriter:
            def write(self, data: bytes) -> None:
                output.extend(data)
            async def drain(self) -> None: pass
            def is_closing(self) -> bool: return False
            def close(self) -> None: pass
            async def wait_closed(self) -> None: pass

        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)

        plugin = _make_plugin_instance()
        msg = self._make_fake_message(authenticated=1)
        await plugin._show_message_index(term, "GENERAL", [msg])

        text = output.decode("ascii", errors="replace")
        # success tone in truecolor is fg_rgb(118, 214, 130)
        green_escape = fg_rgb(118, 214, 130)
        assert green_escape in text, "Expected green (success) ANSI escape for authenticated from_call"

    async def test_unauthenticated_from_call_uses_orange_color_in_index(self):
        """Unauthenticated from_call in message index must use 'orange' tone."""
        from bbs.core.terminal import Terminal, ColorMode, fg_rgb

        output = bytearray()

        class _FakeWriter:
            def write(self, data: bytes) -> None:
                output.extend(data)
            async def drain(self) -> None: pass
            def is_closing(self) -> bool: return False
            def close(self) -> None: pass
            async def wait_closed(self) -> None: pass

        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)

        plugin = _make_plugin_instance()
        msg = self._make_fake_message(authenticated=0)
        await plugin._show_message_index(term, "GENERAL", [msg])

        text = output.decode("ascii", errors="replace")
        # orange tone in truecolor is fg_rgb(210, 140, 60)
        orange_escape = fg_rgb(210, 140, 60)
        assert orange_escape in text, "Expected orange ANSI escape for unauthenticated from_call"
        # Must NOT use the green (success) escape for unauthenticated
        green_escape = fg_rgb(118, 214, 130)
        assert green_escape not in text, "Green escape must NOT appear for unauthenticated from_call"

    async def test_no_star_in_unauth_index_display(self):
        """Unauthenticated messages must not show '*' in the from_call field."""
        from bbs.core.terminal import Terminal, ColorMode

        output = bytearray()

        class _FakeWriter:
            def write(self, data: bytes) -> None:
                output.extend(data)
            async def drain(self) -> None: pass
            def is_closing(self) -> bool: return False
            def close(self) -> None: pass
            async def wait_closed(self) -> None: pass

        from tests.client import _strip_ansi
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.ANSI16)

        plugin = _make_plugin_instance()
        msg = self._make_fake_message(authenticated=0)
        await plugin._show_message_index(term, "GENERAL", [msg])

        plain = _strip_ansi(output.decode("ascii", errors="replace"))
        assert "W1TEST*" not in plain

    async def test_star_present_in_auth_index_display(self):
        """Authenticated messages must show '*' appended to the callsign."""
        from bbs.core.terminal import Terminal, ColorMode

        output = bytearray()

        class _FakeWriter:
            def write(self, data: bytes) -> None:
                output.extend(data)
            async def drain(self) -> None: pass
            def is_closing(self) -> bool: return False
            def close(self) -> None: pass
            async def wait_closed(self) -> None: pass

        from tests.client import _strip_ansi
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.ANSI16)

        plugin = _make_plugin_instance()
        msg = self._make_fake_message(authenticated=1)
        await plugin._show_message_index(term, "GENERAL", [msg])

        plain = _strip_ansi(output.decode("ascii", errors="replace"))
        assert "W1TEST*" in plain


# ---------------------------------------------------------------------------
# Helper: build a BulletinsPlugin with minimal config, without a DB
# ---------------------------------------------------------------------------

def _make_plugin_instance():
    from bbs.plugins.bulletins.bulletin import BulletinsPlugin
    p = BulletinsPlugin.__new__(BulletinsPlugin)
    p._cfg = {}
    p._max_subject = 25
    p._max_body = 4096
    return p


async def _make_bulletin_db():
    """Create an in-memory SQLite DB with the full bulletin schema.

    Returns ``(db, area_id)`` for a single 'GENERAL' area.
    """
    from bbs.plugins.bulletins.bulletin import _SCHEMA_SQL
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA_SQL)
    # Add columns that are lazily migrated in _ensure_schema
    for stmt in (
        "ALTER TABLE bulletin_areas ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE bulletin_messages ADD COLUMN authenticated INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await db.execute(stmt)
        except Exception:
            pass  # column already present
    await db.execute("INSERT INTO bulletin_areas (name, description) VALUES ('GENERAL','Test')")
    await db.commit()
    row = await (await db.execute("SELECT id FROM bulletin_areas WHERE name='GENERAL'")).fetchone()
    return db, int(row["id"])


# ---------------------------------------------------------------------------
# Unit tests — _fetch_messages visibility filter
# ---------------------------------------------------------------------------

class TestPrivateMessageFetching:
    """Unit-level tests for _fetch_messages callsign-based visibility filtering."""

    async def test_public_message_visible_to_any_callsign(self):
        """A message addressed to 'ALL' is returned for every callsign."""
        db, area_id = await _make_bulletin_db()
        try:
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,1,'Public','W1A','ALL','body')",
                (area_id,),
            )
            await db.commit()
            plugin = _make_plugin_instance()
            rows = await plugin._fetch_messages(db, area_id, callsign="W1THIRD")
            assert len(rows) == 1 and rows[0]["subject"] == "Public"
        finally:
            await db.close()

    async def test_private_message_hidden_from_third_party(self):
        """A private message must not appear for a callsign that is neither sender nor recipient."""
        db, area_id = await _make_bulletin_db()
        try:
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,1,'Secret','W1SEND','W1RECV','body')",
                (area_id,),
            )
            await db.commit()
            plugin = _make_plugin_instance()
            rows = await plugin._fetch_messages(db, area_id, callsign="W1THIRD")
            assert len(rows) == 0
        finally:
            await db.close()

    async def test_private_message_visible_to_sender(self):
        """The sender of a private message must see their own message."""
        db, area_id = await _make_bulletin_db()
        try:
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,1,'Secret','W1SEND','W1RECV','body')",
                (area_id,),
            )
            await db.commit()
            plugin = _make_plugin_instance()
            rows = await plugin._fetch_messages(db, area_id, callsign="W1SEND")
            assert len(rows) == 1 and rows[0]["subject"] == "Secret"
        finally:
            await db.close()

    async def test_private_message_visible_to_recipient(self):
        """The recipient of a private message must see it."""
        db, area_id = await _make_bulletin_db()
        try:
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,1,'Secret','W1SEND','W1RECV','body')",
                (area_id,),
            )
            await db.commit()
            plugin = _make_plugin_instance()
            rows = await plugin._fetch_messages(db, area_id, callsign="W1RECV")
            assert len(rows) == 1 and rows[0]["subject"] == "Secret"
        finally:
            await db.close()

    async def test_sysop_sees_all_messages(self):
        """A sysop (is_sysop=True) must see every message including private ones."""
        db, area_id = await _make_bulletin_db()
        try:
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,1,'Public','W1A','ALL','body')",
                (area_id,),
            )
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,2,'Private','W1B','W1C','body')",
                (area_id,),
            )
            await db.commit()
            plugin = _make_plugin_instance()
            rows = await plugin._fetch_messages(db, area_id, callsign="W1SYS", is_sysop=True)
            subjects = {r["subject"] for r in rows}
            assert subjects == {"Public", "Private"}
        finally:
            await db.close()

    async def test_user_sees_only_own_private_messages(self):
        """User sees public msgs plus only their sent/received private ones."""
        db, area_id = await _make_bulletin_db()
        try:
            inserts = [
                (1, "To me",       "W1X",    "W1USER"),   # addressed to W1USER ✓
                (2, "From me",     "W1USER", "W1Y"),       # sent by W1USER ✓
                (3, "Not for me",  "W1X",    "W1Y"),       # private, unrelated ✗
                (4, "Broadcast",   "W1X",    "ALL"),        # public ✓
            ]
            for num, subj, frm, to in inserts:
                await db.execute(
                    "INSERT INTO bulletin_messages "
                    "(area_id, msg_number, subject, from_call, to_call, body) "
                    "VALUES (?,?,?,?,?,?)",
                    (area_id, num, subj, frm, to, "body"),
                )
            await db.commit()
            plugin = _make_plugin_instance()
            rows = await plugin._fetch_messages(db, area_id, callsign="W1USER")
            subjects = {r["subject"] for r in rows}
            assert "To me"      in subjects
            assert "From me"    in subjects
            assert "Broadcast"  in subjects
            assert "Not for me" not in subjects
        finally:
            await db.close()

    async def test_case_insensitive_callsign_matching(self):
        """Visibility matching must be case-insensitive (AX.25 callsigns are case-folded)."""
        db, area_id = await _make_bulletin_db()
        try:
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,1,'CaseTest','W1UPPER','w1lower','body')",
                (area_id,),
            )
            await db.commit()
            plugin = _make_plugin_instance()
            # Recipient stored as 'w1lower', query with uppercase variant
            rows = await plugin._fetch_messages(db, area_id, callsign="W1LOWER")
            assert len(rows) == 1
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Integration tests — private message visibility through BBS sessions
# ---------------------------------------------------------------------------

class TestPrivateMessageVisibility:
    """Integration tests: private messages must be invisible to third parties in live sessions."""

    async def _insert_msgs(self, bbs_server, sender, recipient, tag):
        """Insert a public beacon + a private message; return (area_id, private_msg_num)."""
        db_path = str(bbs_server.engine.cfg.db_path)
        async with aiosqlite.connect(db_path) as db:
            for call in (sender, recipient):
                await db.execute(
                    "INSERT OR IGNORE INTO users (callsign, approved) VALUES (?,1)", (call,)
                )
            area_row = await (
                await db.execute("SELECT id FROM bulletin_areas ORDER BY name LIMIT 1")
            ).fetchone()
            area_id = area_row[0]
            next_num = (
                await (
                    await db.execute(
                        "SELECT COALESCE(MAX(msg_number),0)+1 FROM bulletin_messages WHERE area_id=?",
                        (area_id,),
                    )
                ).fetchone()
            )[0]
            # Public beacon so listing prompt always fires
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,?,?,?,?,?)",
                (area_id, next_num, f"BEACON-{tag}", "W1TEST", "ALL", "beacon"),
            )
            prv_num = next_num + 1
            await db.execute(
                "INSERT INTO bulletin_messages "
                "(area_id, msg_number, subject, from_call, to_call, body) "
                "VALUES (?,?,?,?,?,?)",
                (area_id, prv_num, f"PRIVATE-{tag}", sender, recipient, "secret"),
            )
            await db.commit()
        return area_id, prv_num

    async def _get_listing(self, bbs_server, callsign) -> str:
        """Connect as *callsign*, enter bulletins, list area 1, return listing text."""
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await c.sendln("BU")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("A")
            await c.wait_for("area number")
            await c.sendln("1")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("L")
            text = await c.wait_for("R# /")
            await c.sendln("")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("Q")
            await c.wait_for(">")
        return text

    async def test_private_message_hidden_from_third_party(self, bbs_server: _BbsServerHandle):
        sender, recipient, third = "W1PVSD1", "W1PVRC1", "W1PVTH1"
        await self._insert_msgs(bbs_server, sender, recipient, "VIS1")
        text = await self._get_listing(bbs_server, third)
        assert "PRIVATE-VIS1" not in text
        assert "BEACON-VIS1" in text   # public message is still visible

    async def test_private_message_visible_to_recipient(self, bbs_server: _BbsServerHandle):
        sender, recipient = "W1PVSD2", "W1PVRC2"
        await self._insert_msgs(bbs_server, sender, recipient, "VIS2")
        text = await self._get_listing(bbs_server, recipient)
        assert "PRIVATE-VIS2" in text

    async def test_private_message_visible_to_sender(self, bbs_server: _BbsServerHandle):
        sender, recipient = "W1PVSD3", "W1PVRC3"
        await self._insert_msgs(bbs_server, sender, recipient, "VIS3")
        text = await self._get_listing(bbs_server, sender)
        assert "PRIVATE-VIS3" in text

    async def test_direct_read_by_number_blocked_for_third_party(
        self, bbs_server: _BbsServerHandle
    ):
        """A third party who guesses the msg number must get 'not found' when reading."""
        sender, recipient, third = "W1PVSD4", "W1PVRC4", "W1PVTH4"
        _, prv_num = await self._insert_msgs(bbs_server, sender, recipient, "VIS4")

        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(third)
            await c.sendln("BU")
            await c.wait_for(BULL_PROMPT)
            await c.sendln("A")
            await c.wait_for("area number")
            await c.sendln("1")
            await c.wait_for(BULL_PROMPT)
            await c.sendln(f"R{prv_num}")
            text = await c.wait_for(BULL_PROMPT)
        assert "not found" in text.lower()


# ---------------------------------------------------------------------------
# Helper: insert a message into an area, returning (area_id, msg_number)
# ---------------------------------------------------------------------------

async def _insert_message(db_path: str, from_call: str, subject: str,
                           to_call: str = "ALL") -> tuple[int, int]:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (callsign, approved) VALUES (?,1)", (from_call,)
        )
        area_row = await (
            await db.execute("SELECT id FROM bulletin_areas ORDER BY name LIMIT 1")
        ).fetchone()
        area_id = area_row[0]
        next_num = (
            await (
                await db.execute(
                    "SELECT COALESCE(MAX(msg_number),0)+1 FROM bulletin_messages WHERE area_id=?",
                    (area_id,),
                )
            ).fetchone()
        )[0]
        await db.execute(
            "INSERT INTO bulletin_messages "
            "(area_id, msg_number, subject, from_call, to_call, body, authenticated) "
            "VALUES (?,?,?,?,?,?,1)",
            (area_id, next_num, subject, from_call, to_call, "body"),
        )
        await db.commit()
    return area_id, next_num


async def _try_delete(bbs_server, callsign: str, msg_num: int,
                      authenticated: bool = False, secret: bytes = b"") -> str:
    """Connect as *callsign*, optionally authenticate, attempt to delete *msg_num*.

    Returns all text captured from the bulletin sub-menu after the delete attempt.
    """
    async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
        await c.do_login(callsign)
        if authenticated:
            await _do_auth(c, str(bbs_server.engine.cfg.db_path), callsign, secret)
        await c.sendln("BU")
        await c.wait_for(BULL_PROMPT)
        await c.sendln("A")
        await c.wait_for("area number")
        await c.sendln("1")
        await c.wait_for(BULL_PROMPT)
        await c.sendln(f"D{msg_num}")
        text = await c.wait_for(BULL_PROMPT)
        await c.sendln("Q")
        await c.wait_for(">")
    return text


# ---------------------------------------------------------------------------
# Delete authorisation tests
# ---------------------------------------------------------------------------

class TestDeleteAuthorization:
    """Verify that delete access control is enforced correctly."""

    _SECRET = b"delete_auth_test_secret_xyz1234"

    async def test_identified_only_cannot_delete(self, bbs_server: _BbsServerHandle):
        """A user who has only identified (no OTP) must be refused."""
        _, msg_num = await _insert_message(
            str(bbs_server.engine.cfg.db_path), "W1DLTST1", "NodeleteSub1"
        )
        text = await _try_delete(bbs_server, "W1DLTST1", msg_num, authenticated=False)
        assert "auth" in text.lower()
        # Message must still exist
        async with aiosqlite.connect(str(bbs_server.engine.cfg.db_path)) as db:
            row = await (await db.execute(
                "SELECT deleted FROM bulletin_messages WHERE subject=?", ("NodeleteSub1",)
            )).fetchone()
        assert row and row[0] == 0

    async def test_authenticated_owner_can_delete(self, bbs_server: _BbsServerHandle):
        """The authenticated sender of a message can delete it."""
        callsign = "W1DLTST2"
        _, msg_num = await _insert_message(
            str(bbs_server.engine.cfg.db_path), callsign, "OwnMsgSub2"
        )
        text = await _try_delete(bbs_server, callsign, msg_num,
                                  authenticated=True, secret=self._SECRET)
        assert "deleted" in text.lower()
        # Message must be soft-deleted
        async with aiosqlite.connect(str(bbs_server.engine.cfg.db_path)) as db:
            row = await (await db.execute(
                "SELECT deleted FROM bulletin_messages WHERE subject=?", ("OwnMsgSub2",)
            )).fetchone()
        assert row and row[0] == 1

    async def test_authenticated_non_owner_cannot_delete(self, bbs_server: _BbsServerHandle):
        """An authenticated user must not delete someone else's message."""
        author   = "W1DLTST3A"
        attacker = "W1DLTST3B"
        _, msg_num = await _insert_message(
            str(bbs_server.engine.cfg.db_path), author, "OthersMsgSub3"
        )
        text = await _try_delete(bbs_server, attacker, msg_num,
                                  authenticated=True, secret=self._SECRET)
        assert "only delete your own" in text.lower()
        # Message must be untouched
        async with aiosqlite.connect(str(bbs_server.engine.cfg.db_path)) as db:
            row = await (await db.execute(
                "SELECT deleted FROM bulletin_messages WHERE subject=?", ("OthersMsgSub3",)
            )).fetchone()
        assert row and row[0] == 0

    async def test_sysop_can_delete_any_message(self, bbs_server: _BbsServerHandle):
        """The sysop account must be able to delete any user's message."""
        author = "W1DLTST4"
        _, msg_num = await _insert_message(
            str(bbs_server.engine.cfg.db_path), author, "SysopDelSub4"
        )
        # The test BBS callsign is the sysop; authenticate it so is_authenticated is True
        sysop_call = bbs_server.engine.cfg.callsign.upper()
        text = await _try_delete(bbs_server, sysop_call, msg_num,
                                  authenticated=True, secret=self._SECRET)
        assert "deleted" in text.lower()
        async with aiosqlite.connect(str(bbs_server.engine.cfg.db_path)) as db:
            row = await (await db.execute(
                "SELECT deleted FROM bulletin_messages WHERE subject=?", ("SysopDelSub4",)
            )).fetchone()
        assert row and row[0] == 1

    async def test_delete_nonexistent_message_says_not_found(self, bbs_server: _BbsServerHandle):
        """Attempting to delete a message number that doesn't exist returns not found."""
        callsign = "W1DLTST5"
        text = await _try_delete(bbs_server, callsign, 99999,
                                  authenticated=True, secret=self._SECRET)
        assert "not found" in text.lower()


# ---------------------------------------------------------------------------
# Help command tests
# ---------------------------------------------------------------------------

class TestHelp:
    """Verify the '?' help command is present in the menu and shows useful content."""

    async def test_help_appears_in_menu(self, logged_in_client: BbsTestClient):
        """The menu must list the ? key."""
        await logged_in_client.sendln("BU")
        text = await logged_in_client.wait_for(BULL_PROMPT)
        assert "?" in text
        await logged_in_client.sendln("Q")
        await logged_in_client.wait_for(">")

    async def _get_help_text(self, client: BbsTestClient) -> str:
        """Send '?' and collect the full help output, dismissing any [MORE] prompts."""
        await client.sendln("BU")
        await client.wait_for(BULL_PROMPT)
        await client.sendln("?")
        # Dismiss each [MORE] page until the menu prompt reappears
        full = ""
        while True:
            chunk = await client.wait_for(r"\[MORE\]|Enter choice:", regex=True)
            full += chunk
            if "Enter choice:" in chunk:
                break
            await client.sendln(" ")  # page forward
        return full

    async def test_help_shows_identified_section(self, logged_in_client: BbsTestClient):
        """Help output must explain what IDENTIFIED users can and cannot do."""
        text = await self._get_help_text(logged_in_client)
        low = text.lower()
        assert "identified" in low
        assert "authenticated" in low
        assert "post" in low
        await logged_in_client.sendln("Q")
        await logged_in_client.wait_for(">")

    async def test_help_shows_current_level_identified(self, logged_in_client: BbsTestClient):
        """Help must reflect that the current session is IDENTIFIED (not yet OTP)."""
        text = await self._get_help_text(logged_in_client)
        assert "identified" in text.lower()
        await logged_in_client.sendln("Q")
        await logged_in_client.wait_for(">")

    async def test_help_shows_current_level_authenticated(self, bbs_server: _BbsServerHandle):
        """After OTP auth the help must show AUTHENTICATED as the current level."""
        secret = b"help_auth_secret_xyzw1234567890!"
        callsign = "W1HLPAUTH"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _do_auth(c, str(bbs_server.engine.cfg.db_path), callsign, secret)
            text = await self._get_help_text(c)
            assert "authenticated" in text.lower()
            await c.sendln("Q")
            await c.wait_for(">")

    async def test_help_mentions_private_messages(self, logged_in_client: BbsTestClient):
        """Help must describe private message visibility rules."""
        text = await self._get_help_text(logged_in_client)
        assert "private" in text.lower()
        await logged_in_client.sendln("Q")
        await logged_in_client.wait_for(">")

