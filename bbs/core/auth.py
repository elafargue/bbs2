"""
bbs/core/auth.py — Authentication service for BBS sessions.

Design
------
Auth is a *service* that plugins and the session manager call.  The session
carries an AuthState; plugins check it before allowing privileged operations.

Two access tiers
----------------
1. **Identified** — the callsign is known/trusted from the AX.25 connection
   header (kernel_ax25 / kiss transports).  On these transports the OS/TNC
   has already verified the callsign at the protocol level.  Most read-only
   features are available here without any challenge.

2. **Authenticated** — user has entered a valid OTP code.  Required for write
   operations (posting, changing settings).  The OTP secret is set by the
   sysop via the web interface and shared with the user as a base32 string
   (compatible with Google Authenticator, Authy, etc.).

On the TCP transport (Telnet/testing) no callsign is embedded in the
connection, so the user must type their callsign first; that callsign is
then treated as "identified" (lower trust).

Authentication protocol
-----------------------
  S → C:  "Enter OTP code: "         (TOTP — user reads from authenticator)
    or    "Enter OTP code (counter N): "  (HOTP)
  C → S:  123456                      (6-digit code, no spaces needed)
  S → C:  OK | FAILED (N attempts remaining) | LOCKED (N min remaining)

OTP algorithms
--------------
- TOTP (RFC 6238): HOTP with T = floor((unix_time - T0) / time_step).
  time_step defaults to 30 s (configurable).  One step of clock-skew
  tolerance is allowed (±1 window).
- HOTP (RFC 4226): counter-based.  Look-ahead window of 2 is used.
  Counter is advanced in the DB after a successful verification.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac_mod
import struct
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import aiosqlite

from bbs.config import BBSConfig
from bbs.db import users as user_db


class AuthLevel(int, Enum):
    """Ordered access levels — higher value = more privilege."""
    ANONYMOUS = 0      # TCP connection, callsign not yet provided
    IDENTIFIED = 1     # Callsign known (from AX.25 header or self-declared on TCP)
    AUTHENTICATED = 2  # Passed OTP verification
    SYSOP = 3          # Special sysop account


@dataclass
class AuthState:
    """Per-session authentication state, held by BBSSession."""
    callsign: str = ""
    level: AuthLevel = AuthLevel.ANONYMOUS
    user_id: Optional[int] = None

    @property
    def is_identified(self) -> bool:
        return self.level >= AuthLevel.IDENTIFIED

    @property
    def is_authenticated(self) -> bool:
        return self.level >= AuthLevel.AUTHENTICATED

    @property
    def is_sysop(self) -> bool:
        return self.level == AuthLevel.SYSOP

    def require(self, level: AuthLevel) -> bool:
        """Return True if this session meets or exceeds *level*."""
        return self.level.value >= level.value


# ── OTP helpers (RFC 4226 / RFC 6238) ───────────────────────────────────────────────────────────

def _hotp_code(secret: bytes, counter: int, digits: int = 6) -> str:
    """Compute an HOTP code (RFC 4226)."""
    msg = struct.pack(">Q", counter)
    h = _hmac_mod.new(secret, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def compute_totp_code(secret: bytes, time_step: int = 30, digits: int = 6) -> str:
    """Return the current TOTP code.  Exported for tests and the web UI."""
    t = int(time.time()) // time_step
    return _hotp_code(secret, t, digits)


def _totp_window(
    secret: bytes, time_step: int = 30, skew: int = 1, digits: int = 6
) -> list[str]:
    """Return valid codes for [current-skew .. current+skew] time steps."""
    t = int(time.time()) // time_step
    return [_hotp_code(secret, t + i, digits) for i in range(-skew, skew + 1)]


def otp_provisioning_uri(
    secret: bytes, callsign: str, issuer: str, otp_type: str = "totp"
) -> str:
    """Return an otpauth:// URI for QR-code generation."""
    b32 = base64.b32encode(secret).decode()
    call_enc = callsign.replace(" ", "%20")
    iss_enc = issuer.replace(" ", "%20")
    uri = f"otpauth://{otp_type}/{iss_enc}:{call_enc}?secret={b32}&issuer={iss_enc}"
    if otp_type == "hotp":
        uri += "&counter=0"
    return uri


class AuthService:
    """
    Stateless auth service; injected into the BBS engine and plugins.

    All methods that touch the database take an open aiosqlite.Connection so
    that connection management stays with the caller (the engine opens one
    connection per session coroutine).
    """

    def __init__(self, cfg: BBSConfig) -> None:
        self._cfg = cfg
        self._max_attempts = cfg.auth_max_attempts
        self._lockout_secs = cfg.auth_lockout_seconds
        self._time_step = cfg.totp_time_step

    # ── Identification ────────────────────────────────────────────────────────

    async def identify(
        self,
        db: aiosqlite.Connection,
        callsign: str,
        from_ax25: bool = False,
    ) -> tuple[AuthState, bool]:
        """
        Record that a session belongs to *callsign*.

        *from_ax25* should be True when the callsign comes from an AX.25
        connection header (kernel_ax25 / KISS transports) — it is already
        cryptographically asserted by the TNC/kernel, so we can trust it as
        IDENTIFIED immediately.

        Returns (AuthState, user_was_just_created).
        """
        callsign = callsign.upper().strip()
        user, created = await user_db.get_or_create(db, callsign)
        await user_db.update_last_seen(db, user.id)

        level = AuthLevel.IDENTIFIED
        state = AuthState(
            callsign=callsign,
            level=level,
            user_id=user.id,
        )
        return state, created

    # ── OTP prompt ────────────────────────────────────────────────────────────

    async def otp_prompt(self, db: aiosqlite.Connection, state: AuthState) -> str:
        """Return the prompt string to show before the user enters their OTP code."""
        user = await user_db.get_by_callsign(db, state.callsign)
        if user and user.otp_type == "hotp":
            return f"Enter HOTP code (counter {user.hotp_counter}): "
        return "Enter TOTP code: "

    # ── OTP verification ──────────────────────────────────────────────────────

    async def verify_otp(
        self,
        db: aiosqlite.Connection,
        state: AuthState,
        code: str,
    ) -> tuple[bool, str]:
        """
        Verify *code* (a 6-digit string) against the user's stored OTP secret.

        TOTP: checks current ±1 time-step window to tolerate clock skew.
        HOTP: checks counter + look-ahead window of 2; advances counter on success.

        Returns (success, message_for_user).
        """
        callsign = state.callsign
        user = await user_db.get_by_callsign(db, callsign)

        if user is None:
            return False, "FAILED"

        if user.is_locked:
            remaining = (user.locked_until or 0) - int(time.time())
            mins = max(1, remaining // 60)
            return False, f"LOCKED ({mins} min remaining)"

        if not user.has_secret:
            return False, "FAILED (no OTP secret configured — contact sysop)"

        code_clean = code.strip()
        if not code_clean.isdigit() or len(code_clean) != 6:
            failures = await user_db.record_auth_failure(
                db, user.id, self._max_attempts, self._lockout_secs
            )
            remaining = self._max_attempts - failures
            if remaining <= 0:
                mins = self._lockout_secs // 60
                return False, f"FAILED (account locked for {mins} min)"
            return False, f"FAILED ({max(0, remaining)} attempt(s) remaining)"

        secret: bytes = user.totp_secret  # type: ignore[assignment]
        matched = False
        new_counter: Optional[int] = None

        if user.otp_type == "hotp":
            for offset in range(3):  # look-ahead window of 2
                candidate = _hotp_code(secret, user.hotp_counter + offset)
                if _hmac_mod.compare_digest(candidate, code_clean):
                    matched = True
                    new_counter = user.hotp_counter + offset + 1
                    break
        else:
            for candidate in _totp_window(secret, self._time_step):
                if _hmac_mod.compare_digest(candidate, code_clean):
                    matched = True
                    break

        if not matched:
            failures = await user_db.record_auth_failure(
                db, user.id, self._max_attempts, self._lockout_secs
            )
            remaining = self._max_attempts - failures
            if remaining <= 0:
                mins = self._lockout_secs // 60
                return False, f"FAILED (account locked for {mins} min)"
            return False, f"FAILED ({remaining} attempt(s) remaining)"

        # Success
        await user_db.reset_auth_failures(db, user.id)
        if new_counter is not None:
            await user_db.increment_hotp_counter(db, user.id, new_counter)

        state.level = (
            AuthLevel.SYSOP
            if callsign.upper() == self._cfg.callsign.upper()
            else AuthLevel.AUTHENTICATED
        )
        state.user_id = user.id
        return True, "OK"

    # ── Helpers for plugins ───────────────────────────────────────────────────

    async def get_user(
        self, db: aiosqlite.Connection, state: AuthState
    ):
        """Return the User record for the current session, or None."""
        if state.user_id is None:
            return None
        return await user_db.get_by_id(db, state.user_id)

    def level_label(self, level: AuthLevel) -> str:
        return {
            AuthLevel.ANONYMOUS: "anon",
            AuthLevel.IDENTIFIED: "ident",
            AuthLevel.AUTHENTICATED: "auth",
            AuthLevel.SYSOP: "sysop",
        }[level]
