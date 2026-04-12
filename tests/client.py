"""
tests/client.py — TCP test client for BBS integration tests.

BbsTestClient wraps an asyncio TCP connection with helpers for:
  - IAC/Telnet sequence stripping
  - Collecting output until an expected string appears
  - Sending a line of text
  - High-level session helpers (connect, identify, navigate)
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional


# ---------------------------------------------------------------------------
# IAC stripping (copy of terminal.py logic, without the reader side-effects)
# ---------------------------------------------------------------------------

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return re.sub(r"\x1b\[[^A-Za-z]*[A-Za-z]|\x1b.", "", text)


class BbsTestClient:
    """
    Async TCP client that connects to a running BBS and provides helpers
    for writing integration tests.

    Usage (inside an async test)::

        async with BbsTestClient(host, port) as c:
            await c.wait_for("Callsign:")
            await c.sendln("W1TEST")
            await c.wait_for(">")   # main menu prompt
    """

    DEFAULT_TIMEOUT = 10.0  # seconds per wait_for call

    def __init__(self, host: str = "127.0.0.1", port: int = 6300) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Accumulated decoded text not yet consumed
        self._buf = ""

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "BbsTestClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Connection ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    # ── Low-level I/O ─────────────────────────────────────────────────────────

    async def _read_chunk(self, timeout: float = 0.5) -> str:
        """
        Read available bytes from the socket, strip ANSI + IAC, decode.
        Returns empty string on timeout (socket is still open).
        """
        assert self._reader is not None
        try:
            raw = await asyncio.wait_for(self._reader.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            return ""
        if not raw:
            return ""
        return _strip_ansi(_strip_iac(raw).decode("ascii", errors="replace"))

    async def sendln(self, text: str = "") -> None:
        """Send *text* followed by CR+LF."""
        assert self._writer is not None
        self._writer.write((text + "\r\n").encode("ascii", errors="replace"))
        await self._writer.drain()

    # ── High-level helpers ────────────────────────────────────────────────────

    async def wait_for(
        self,
        pattern: str,
        timeout: Optional[float] = None,
        *,
        regex: bool = False,
    ) -> str:
        """
        Accumulate output until *pattern* appears in the buffer.

        Returns all accumulated text up to and including the match.
        Raises AssertionError if *timeout* expires first.

        *pattern* is treated as a literal substring by default;
        set *regex=True* to use re.search semantics.
        """
        deadline = asyncio.get_event_loop().time() + (timeout or self.DEFAULT_TIMEOUT)

        while True:
            if regex:
                m = re.search(pattern, self._buf)
                found = m is not None
            else:
                found = pattern in self._buf

            if found:
                # Return everything up to end of match and clear that from buf
                if regex:
                    end = m.end()  # type: ignore[union-attr]
                else:
                    end = self._buf.index(pattern) + len(pattern)
                consumed = self._buf[:end]
                self._buf = self._buf[end:]
                return consumed

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"Timed out waiting for {pattern!r}.\n"
                    f"Buffer so far:\n{self._buf!r}"
                )

            chunk = await self._read_chunk(timeout=min(0.2, remaining))
            self._buf += chunk

    async def read_all(self, settle: float = 0.3) -> str:
        """
        Drain all pending output (wait *settle* seconds of silence).
        Useful for consuming verbose output before asserting on later lines.
        """
        while True:
            chunk = await self._read_chunk(timeout=settle)
            if not chunk:
                break
            self._buf += chunk
        result = self._buf
        self._buf = ""
        return result

    # ── Session helpers ───────────────────────────────────────────────────────

    async def do_login(self, callsign: str = "W1TEST") -> str:
        """
        Complete the initial greeting + callsign identification sequence.
        Returns the main menu text.
        """
        await self.wait_for("Callsign:")
        await self.sendln(callsign)
        return await self.wait_for(">")

    async def send_choice(self, key: str) -> None:
        """Send a single menu key."""
        await self.sendln(key)

    async def quit(self) -> None:
        """Send B to exit gracefully."""
        await self.sendln("B")


# ---------------------------------------------------------------------------
# IAC stripping helper (bytes-level, no reader state)
# ---------------------------------------------------------------------------

def _strip_iac(data: bytes) -> bytes:
    """Strip Telnet IAC sequences from raw bytes."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0xFF and i + 1 < len(data):
            cmd = data[i + 1]
            if cmd in (0xFB, 0xFC, 0xFD, 0xFE) and i + 2 < len(data):
                i += 3  # IAC + WILL/WONT/DO/DONT + option
            elif cmd == 0xFA:
                # SB ... IAC SE
                i += 2
                while i + 1 < len(data):
                    if data[i] == 0xFF and data[i + 1] == 0xF0:
                        i += 2
                        break
                    i += 1
            else:
                i += 2  # IAC + single-byte command
        else:
            out.append(b)
            i += 1
    return bytes(out)
