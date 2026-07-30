"""
tests/test_terminal.py — Terminal pager behaviour.

Focus: on a line-mode AX.25 terminal a response is sent as a whole line
(e.g. "Q\\r"), so the pager must swallow the trailing CR/LF after a command
character — otherwise it leaks into the next read as a stray ENTER and
triggers a spurious menu redraw.
"""
from __future__ import annotations

import asyncio


from bbs.core.terminal import Terminal


class _FakeWriter:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, b: bytes) -> None:
        self.data.extend(b)

    async def drain(self) -> None:
        pass


async def _term(reader: asyncio.StreamReader) -> Terminal:
    return await Terminal.create(reader, _FakeWriter(), color_mode="off", eol="\r")


async def test_paginate_q_with_trailing_enter_quits_and_swallows_eol():
    reader = asyncio.StreamReader()
    term = await _term(reader)
    reader.feed_data(b"Q\r\n")          # line-mode: Q submitted with CRLF
    reader.feed_eof()
    result = await term.paginate([f"line {i}" for i in range(20)], page_height=5)
    assert result is False               # Q → quit
    assert await reader.read() == b""    # trailing CR/LF fully consumed, nothing leaks


async def test_paginate_q_with_trailing_cr_only():
    reader = asyncio.StreamReader()
    term = await _term(reader)
    reader.feed_data(b"Q\r")             # bare-CR line ending
    reader.feed_eof()
    result = await term.paginate([f"line {i}" for i in range(20)], page_height=5)
    assert result is False
    assert await reader.read() == b""


async def test_paginate_bare_enter_continues_and_consumes_complement():
    reader = asyncio.StreamReader()
    term = await _term(reader)
    # 10 lines / page_height 5 → two MORE prompts; answer each with ENTER (CRLF).
    reader.feed_data(b"\r\n\r\n")
    reader.feed_eof()
    result = await term.paginate([f"line {i}" for i in range(10)], page_height=5)
    assert result is True                # paged to the end
    assert await reader.read() == b""    # both CRLFs consumed


async def test_paginate_q_char_mode_no_trailing_enter():
    reader = asyncio.StreamReader()
    term = await _term(reader)
    reader.feed_data(b"Q")               # char-at-a-time: no trailing ENTER
    result = await asyncio.wait_for(
        term.paginate([f"line {i}" for i in range(20)], page_height=5),
        timeout=5,
    )
    assert result is False               # still quits promptly, does not hang
