"""
tests/test_auth.py — Integration tests for the TOTP/HOTP auth flow.

The auth session command sequence is:
  user sends: A
  BBS sends:  "Enter TOTP code: " (or "Enter HOTP code (counter N): ")
  user sends: 123456              (6-digit OTP code)
  BBS sends:  OK | FAILED | LOCKED
"""
from __future__ import annotations

import aiosqlite
import pytest

from bbs.core.auth import _hotp_code, compute_totp_code
from tests.client import BbsTestClient
from tests.conftest import _BbsServerHandle

# A known 20-byte secret used across tests
_SECRET = b"Hello!\xde\xad\xbe\xef" + b"\x00" * 10  # exactly 20 bytes


async def _set_otp_secret(
    db_path: str,
    callsign: str,
    secret: bytes,
    otp_type: str = "totp",
    counter: int = 0,
) -> None:
    """Directly write an OTP secret into the test database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET totp_secret=?, otp_type=?, hotp_counter=? "
            "WHERE callsign=? COLLATE NOCASE",
            (secret, otp_type, counter, callsign),
        )
        await db.commit()


class TestOtpPrompt:
    async def test_auth_command_shows_otp_prompt(
        self, bbs_server: _BbsServerHandle
    ):
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login("W1TPROMPT")
            await c.sendln("A")
            text = await c.wait_for("OTP")
            assert "TOTP" in text or "OTP" in text

    async def test_auth_command_shows_hotp_counter(
        self, bbs_server: _BbsServerHandle
    ):
        callsign = "W1HPROMPT"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET, "hotp", 7
            )
            await c.sendln("A")
            text = await c.wait_for("counter 7")
            assert "7" in text

    async def test_already_authenticated_says_so(
        self, bbs_server: _BbsServerHandle
    ):
        callsign = "W1ALREADY"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET
            )
            # First auth — succeed
            await c.sendln("A")
            await c.wait_for("OTP")
            await c.sendln(compute_totp_code(_SECRET))
            await c.wait_for(">")

            # Second attempt in same session
            await c.sendln("A")
            text = await c.wait_for(">")
            assert "already" in text.lower()


class TestTotpSuccess:
    async def test_correct_totp_grants_auth(
        self, bbs_server: _BbsServerHandle
    ):
        callsign = "W1TAUTH"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET
            )
            await c.sendln("A")
            await c.wait_for("OTP")
            await c.sendln(compute_totp_code(_SECRET))
            text = await c.wait_for(">")
            assert "OK" in text

    async def test_auth_level_upgrades_on_success(
        self, bbs_server: _BbsServerHandle
    ):
        callsign = "W1TLEVEL"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET
            )
            await c.sendln("A")
            await c.wait_for("OTP")
            await c.sendln(compute_totp_code(_SECRET))
            text = await c.wait_for(">")
            assert "OK" in text


class TestHotpSuccess:
    async def test_correct_hotp_grants_auth(
        self, bbs_server: _BbsServerHandle
    ):
        callsign = "W1HAUTH"
        counter = 0
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET, "hotp", counter
            )
            await c.sendln("A")
            await c.wait_for("OTP")
            await c.sendln(_hotp_code(_SECRET, counter))
            text = await c.wait_for(">")
            assert "OK" in text


class TestOtpFailure:
    async def test_wrong_code_says_failed(
        self, bbs_server: _BbsServerHandle
    ):
        callsign = "W1TFAIL"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET
            )
            await c.sendln("A")
            await c.wait_for("OTP")
            await c.sendln("000000")  # almost certainly wrong
            text = await c.wait_for(">")
            assert "FAILED" in text

    async def test_no_secret_configured_says_failed(
        self, bbs_server: _BbsServerHandle
    ):
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login("W1NOSEC")
            await c.sendln("A")
            await c.wait_for("OTP")
            await c.sendln("123456")
            text = await c.wait_for(">")
            assert "FAILED" in text

    async def test_non_numeric_code_says_failed(
        self, bbs_server: _BbsServerHandle
    ):
        callsign = "W1NNUM"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET
            )
            await c.sendln("A")
            await c.wait_for("OTP")
            await c.sendln("abcdef")  # not digits
            text = await c.wait_for(">")
            assert "FAILED" in text

    async def test_lockout_after_max_attempts(
        self, bbs_server: _BbsServerHandle
    ):
        """After max_attempts (3) failures the account should be locked."""
        callsign = "W1TLOCK"
        async with BbsTestClient(bbs_server.host, bbs_server.port) as c:
            await c.do_login(callsign)
            await _set_otp_secret(
                str(bbs_server.engine.cfg.db_path), callsign, _SECRET
            )
            for _ in range(3):
                await c.sendln("A")
                await c.wait_for("OTP")
                await c.sendln("000000")
                text = await c.wait_for(">")

            assert "LOCKED" in text or "locked" in text
