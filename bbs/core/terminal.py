"""
bbs/core/terminal.py — Lightweight terminal renderer for network sessions.

Design goals
------------
• No curses — curses requires a local TTY; BBS sessions come over radio or TCP.
• Explicit color modes: ASCII-only, classic ANSI 16-color, or truecolor.
• Buffered output: writes are accumulated and flushed in chunks ≤ MAX_CHUNK
    bytes so the radio layer isn't flooded with tiny packets.
• Paging: paginate() inserts a "[MORE]" prompt every page_height lines and
    waits for SPACE/ENTER/Q.
• Line-at-a-time input: readline() handles both CR and LF line endings,
    echoes characters (for TCP; AX.25 connected-mode handles its own echo),
    and respects a maximum line length.
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# Maximum bytes per write flush — keeps RF frames manageable
MAX_CHUNK = 256

# ── ANSI escape helpers ────────────────────────────────────────────────────────

ESC = "\x1b"
CSI = f"{ESC}["

# Colours (foreground: 30-37/90-97, background: 40-47/100-107)
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

RESET = f"{CSI}0m"
BOLD = f"{CSI}1m"
UNDERLINE = f"{CSI}4m"
REVERSE = f"{CSI}7m"


class ColorMode(str, Enum):
    OFF = "off"
    ANSI16 = "ansi16"
    TRUECOLOR = "truecolor"


def normalize_color_mode(color_mode: str | ColorMode | None) -> ColorMode:
    if isinstance(color_mode, ColorMode):
        return color_mode
    value = str(color_mode or ColorMode.OFF.value).strip().lower()
    try:
        return ColorMode(value)
    except ValueError:
        return ColorMode.OFF


def fg(color: int) -> str:
    if not 0 <= color <= 15:
        raise ValueError("ANSI color index must be between 0 and 15")
    base = 30 if color < 8 else 90
    return f"{CSI}{base + (color % 8)}m"


def bg(color: int) -> str:
    if not 0 <= color <= 15:
        raise ValueError("ANSI color index must be between 0 and 15")
    base = 40 if color < 8 else 100
    return f"{CSI}{base + (color % 8)}m"


def fg_rgb(red: int, green: int, blue: int) -> str:
    return f"{CSI}38;2;{red};{green};{blue}m"


def bg_rgb(red: int, green: int, blue: int) -> str:
    return f"{CSI}48;2;{red};{green};{blue}m"


def move_to(row: int, col: int) -> str:
    return f"{CSI}{row};{col}H"


def clear_screen() -> str:
    return f"{CSI}2J{CSI}H"


def clear_line() -> str:
    return f"{CSI}2K"


class Terminal:
    """
    Manages one user's terminal interaction over an asyncio reader/writer pair.

    Instantiate once per session.  The BBS session and plugins call the
    methods below; they never write to the writer directly.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer,  # asyncio.StreamWriter or duck-typed equivalent
        color_mode: str | ColorMode = ColorMode.OFF,
        width: int = 80,
        height: int = 24,
        echo: bool = True,
        must_echo: bool = False,
        eol: str = "\r\n",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.color_mode = normalize_color_mode(color_mode)
        self.width = width
        self.height = height
        self._echo = echo
        self._must_echo = must_echo  # True for web sessions: can't be suppressed by callers
        self._eol = eol
        self._buf = bytearray()

    @property
    def ansi(self) -> bool:
        return self.color_mode is not ColorMode.OFF

    @property
    def supports_truecolor(self) -> bool:
        return self.color_mode is ColorMode.TRUECOLOR

    def set_color_mode(self, color_mode: str | ColorMode) -> None:
        self.color_mode = normalize_color_mode(color_mode)

    # ── Detection ─────────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        reader: asyncio.StreamReader,
        writer,
        color_mode: str | ColorMode = ColorMode.OFF,
        width: int = 80,
        height: int = 24,
        echo: bool = True,
        must_echo: bool = False,
        eol: str = "\r\n",
    ) -> "Terminal":
        """Return a Terminal using the requested color mode."""
        return cls(
            reader,
            writer,
            color_mode=color_mode,
            width=width,
            height=height,
            echo=echo,
            must_echo=must_echo,
            eol=eol,
        )

    # ── Output ────────────────────────────────────────────────────────────────

    def _encode(self, text: str) -> bytes:
        """Encode text to bytes, stripping ANSI codes if in ASCII mode."""
        if self.color_mode is ColorMode.OFF:
            # Strip ESC sequences
            import re
            text = re.sub(r"\x1b\[[^A-Za-z]*[A-Za-z]", "", text)
            text = re.sub(r"\x1b.", "", text)
        return text.encode("ascii", errors="replace")

    def _key_style(self, key: str) -> str:
        if self.supports_truecolor:
            return f"[{BOLD}{fg_rgb(110, 223, 255)}{key}{RESET}]"
        if self.ansi:
            return f"[{BOLD}{fg(14)}{key}{RESET}]"
        return f"[{key}]"

    @staticmethod
    def _visible_len(text: str) -> int:
        """Return the printable length of *text*, ignoring ANSI escape codes."""
        import re
        plain = re.sub(r"\x1b\[[^A-Za-z]*[A-Za-z]|\x1b.", "", text)
        return len(plain)

    def style(self, text: str, tone: str = "accent", *, bold: bool = False) -> str:
        if not text or not self.ansi:
            return text

        if self.supports_truecolor:
            palette = {
                "accent": fg_rgb(110, 223, 255),
                "meta": fg_rgb(170, 195, 220),
                "success": fg_rgb(118, 214, 130),
                "warning": fg_rgb(241, 198, 92),
                "orange": fg_rgb(210, 140, 60),
                "error": fg_rgb(239, 122, 122),
            }
        else:
            palette = {
                "accent": fg(14),
                "meta": fg(13),
                "success": fg(10),
                "warning": fg(11),
                "orange": fg(3),   # ANSI dark yellow — reads as orange on most terminals
                "warning": fg(11),
                "orange": fg(3),   # ANSI dark yellow — reads as orange on most terminals
                "error": fg(9),
            }

        prefix = palette.get(tone, "")
        if bold:
            prefix += BOLD
        return f"{prefix}{text}{RESET}" if prefix else text

    def label(self, text: str, tone: str = "accent") -> str:
        return self.style(text, tone, bold=True)

    def prompt(self, text: str) -> str:
        return self.style(text, "accent", bold=True)

    def note(self, text: str) -> str:
        return self.style(text, "meta")

    def ok(self, text: str) -> str:
        return self.style(text, "success", bold=True)

    def warn(self, text: str) -> str:
        return self.style(text, "warning", bold=True)

    def error(self, text: str) -> str:
        return self.style(text, "error", bold=True)

    def field(self, label: str, value: str, tone: str = "accent") -> str:
        return f"{self.label(label, tone)} {value}"

    def _more_style(self) -> str:
        if self.supports_truecolor:
            return f"{BOLD}{bg_rgb(34, 47, 62)}{fg_rgb(245, 248, 252)}[MORE]{RESET} "
        if self.ansi:
            return f"{BOLD}{bg(BLUE)}{fg(15)}[MORE]{RESET} "
        return "[MORE] "

    def write(self, text: str) -> None:
        """Buffer *text* for transmission (does not flush immediately)."""
        self._buf.extend(self._encode(text))

    def writeln(self, text: str = "") -> None:
        """Buffer *text* followed by the session line terminator (CR+LF or CR)."""
        self.write(text + self._eol)

    async def flush(self) -> None:
        """Send all buffered output in MAX_CHUNK-byte chunks."""
        while self._buf:
            chunk = bytes(self._buf[:MAX_CHUNK])
            self._buf = self._buf[MAX_CHUNK:]
            self._writer.write(chunk)
            await self._writer.drain()

    async def send(self, text: str) -> None:
        """Write and immediately flush *text*."""
        self.write(text)
        await self.flush()

    async def sendln(self, text: str = "") -> None:
        """Write *text* + CR+LF and flush."""
        self.writeln(text)
        await self.flush()

    # ── Styled shortcuts (ANSI only; fall through to plain on ASCII) ──────────

    async def send_bold(self, text: str) -> None:
        await self.send(f"{BOLD}{text}{RESET}")

    async def send_header(self, text: str) -> None:
        """Styled header line, padded to width."""
        padded = text.center(self.width)
        if self.supports_truecolor:
            await self.send(
                f"{BOLD}{bg_rgb(24, 48, 78)}{fg_rgb(245, 248, 252)}{padded}{RESET}{self._eol}"
            )
            return
        if self.ansi:
            await self.send(f"{BOLD}{bg(BLUE)}{fg(15)}{padded}{RESET}{self._eol}")
            return
        await self.sendln(padded)

    async def send_separator(self, char: str = "-") -> None:
        await self.sendln(char * self.width)

    # ── Menus ─────────────────────────────────────────────────────────────────

    async def send_menu(
        self, title: str, items: list[tuple[str, str]], prompt: str = "Enter choice: "
    ) -> None:
        """
        Display a compact menu and prompt.

        items: list of (key, description) e.g. [("B", "Bulletins"), ("C", "Chat")]
        """
        await self.sendln()
        if self.ansi:
            await self.send_header(f" {title} ")
        else:
            await self.sendln(f"=== {title} ===")

        # Two-column layout to save screen lines at 1200 bps
        half = (len(items) + 1) // 2
        left = items[:half]
        right = items[half:]
        col_w = self.width // 2 - 2

        for i, (litem, ritem) in enumerate(
            zip(left, right + [("", "")] * (half - len(right)))
        ):
            lkey, ldesc = litem
            rkey, rdesc = ritem
            if self.ansi:
                lcell = f"{self._key_style(lkey)} {ldesc}"
                rcell = f"{self._key_style(rkey)} {rdesc}" if rkey else ""
            else:
                lcell = f"[{lkey}] {ldesc}"
                rcell = f"[{rkey}] {rdesc}" if rkey else ""
            # Pad lcell to col_w *visible* characters; ANSI codes must not count.
            pad = max(0, col_w - self._visible_len(lcell))
            self.writeln(f"  {lcell}{' ' * pad}  {rcell}")

        await self.sendln()
        await self.send(prompt)

    # ── Paging ────────────────────────────────────────────────────────────────

    async def paginate(self, lines: list[str], page_height: Optional[int] = None) -> bool:
        """
        Send *lines* with paging.  After every page_height lines, display
        a [MORE] prompt and wait for SPACE/ENTER (continue) or Q (quit).
        Returns False if the user aborted with Q.
        """
        ph = page_height or (self.height - 2)
        count = 0
        for line in lines:
            self.writeln(line)
            count += 1
            if count >= ph:
                await self.flush()
                await self.send(self._more_style())
                ch = await self.readchar()
                await self.sendln()
                if ch.upper() == "Q":
                    return False
                count = 0
        await self.flush()
        return True

    # ── Input ─────────────────────────────────────────────────────────────────

    async def readchar(self, timeout: Optional[float] = None) -> str:
        """Read a single character, stripping Telnet IAC sequences.  Returns '' on timeout or EOF."""
        while True:
            try:
                coro = self._reader.read(1)
                data = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            except asyncio.TimeoutError:
                return ""
            except Exception:
                return ""

            if not data:
                return ""  # EOF

            byte = data[0]

            # Telnet IAC (0xFF) sequence — consume and discard
            if byte == 0xFF:
                try:
                    cmd = (await asyncio.wait_for(self._reader.read(1), timeout=1.0))[0]
                except Exception:
                    return ""
                if cmd in (0xFB, 0xFC, 0xFD, 0xFE):
                    # WILL / WONT / DO / DONT — one option byte follows
                    try:
                        await asyncio.wait_for(self._reader.read(1), timeout=1.0)
                    except Exception:
                        pass
                elif cmd == 0xFA:
                    # SB subnegotiation — read until IAC SE (0xFF 0xF0)
                    try:
                        prev = 0
                        while True:
                            b = (await asyncio.wait_for(self._reader.read(1), timeout=1.0))[0]
                            if prev == 0xFF and b == 0xF0:
                                break
                            prev = b
                    except Exception:
                        pass
                # else: single-byte IAC command — already consumed
                continue  # loop back for the next real character

            return data.decode("ascii", errors="replace")

    async def readline(
        self, max_len: int = 80, echo: Optional[bool] = None, timeout: Optional[float] = None
    ) -> str:
        """
        Read a line of input (terminated by CR or LF).

        *echo*: if True, echo printable characters back (needed for TCP/Telnet
        clients; AX.25 connected-mode clients typically handle echo at TNC level).
        Pass None (default) to use the terminal's echo setting (set at creation).
        *max_len*: discard characters beyond this count.
        *timeout*: seconds; returns partial input on timeout.
        Returns the line without the line terminator.
        """
        if echo is None:
            echo = self._echo
        if self._must_echo:
            echo = True
        buf = []
        deadline = asyncio.get_event_loop().time() + timeout if timeout else None

        while True:
            remaining: Optional[float] = None
            if deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
            ch = await self.readchar(timeout=remaining)
            if not ch:
                break
            if ch in ("\r", "\n"):
                if ch == "\r":
                    # Consume a trailing \n (telnet sends \r\n)
                    try:
                        peek = await asyncio.wait_for(self._reader.read(1), timeout=0.05)
                        if peek and peek != b"\n":
                            # Not a \n — put it back by prepending to the internal buffer
                            self._reader.feed_data(peek)
                    except asyncio.TimeoutError:
                        pass
                if echo:
                    await self.send("\r\n")
                break
            if ch == "\x08" or ch == "\x7f":  # Backspace / DEL
                if buf:
                    buf.pop()
                    if echo:
                        await self.send("\x08 \x08")
                continue
            if ch == "\x03":  # Ctrl-C
                buf = []
                break
            if len(buf) < max_len and ch.isprintable():
                buf.append(ch)
                if echo:
                    await self.send(ch)

        return "".join(buf)

    async def readline_password(self, prompt: str = "Secret: ") -> str:
        """Read a line without echoing — used for AUTH response."""
        await self.send(prompt)
        result = await self.readline(max_len=128, echo=False)
        await self.sendln()
        return result
