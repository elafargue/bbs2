"""
bbs/services/bridge.py — wire a Connection to an external program's stdio.

This is the userspace equivalent of what ax25d does after ``accept()``: it
runs the configured program with the connection's byte stream on the child's
stdin/stdout.  Differences from ax25d are deliberate and documented:

  • fd wiring   — instead of ``dup2`` of a socket onto fd0/fd1, we pump bytes
                  between ``conn.reader``/``conn.writer`` and the child's
                  ``stdin``/``stdout`` pipes.  stderr is left inherited so the
                  program's diagnostics land in the bbs2 journal.
  • environment — the child is started with a MINIMAL environment (a PATH
                  only, so shebangs resolve), close to ax25d's ``execve(...,
                  NULL)`` but practical for scripts; a route ``env:`` map is
                  layered on top.  Per-connection context is passed through
                  argv (see dispatcher.build_argv).
  • line endings— raw passthrough by default (ax25d does no translation).
                  With ``route.crlf`` we translate between the program's Unix
                  LF and the AX.25 bare-CR convention.

Teardown is symmetric: when either the radio peer disconnects or the program
exits, we close the child's stdin, terminate it (SIGTERM → SIGKILL after a
grace period), flush any remaining program output to the peer, and close the
connection.  An optional per-route ``idle_timeout`` reaps a session with no
traffic in either direction (external sessions bypass the BBS idle watchdog).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bbs.services.dispatcher import ServiceRoute
    from bbs.transport.base import Connection

logger = logging.getLogger(__name__)

_READ_CHUNK: int = 4096
_TERM_GRACE: float = 5.0    # seconds to wait after SIGTERM before SIGKILL
_FLUSH_GRACE: float = 2.0   # seconds to let the program's last output drain

# The child starts with a MINIMAL environment (not the parent's) — close to
# ax25d's NULL environ, but with a PATH so that ``#!/usr/bin/env python3`` style
# shebangs and bare-name execs resolve.  A route's ``env:`` map is layered on
# top (and can override PATH).
_MINIMAL_ENV: dict = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


def _to_program(data: bytes) -> bytes:
    """Radio → program: normalize AX.25 CR / CRLF to Unix LF."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _to_radio(data: bytes) -> bytes:
    """Program → radio: convert Unix LF to the AX.25 bare-CR convention."""
    return data.replace(b"\r\n", b"\r").replace(b"\n", b"\r")


async def run_service(conn: "Connection", route: "ServiceRoute", argv: list[str]) -> None:
    """Spawn *route* with *argv* and bridge *conn* to its stdin/stdout.

    Returns when the session ends (peer disconnect, program exit, idle
    timeout, or spawn failure).  Always closes *conn* before returning.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            argv[0],
            *argv[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,          # inherit → bbs2 journal
            env={**_MINIMAL_ENV, **route.env},   # minimal env + per-route overrides
            executable=route.exec_path,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.error(
            "services: failed to exec %s for %s→%s: %s",
            route.exec_path, conn.remote_addr, route.called, exc,
        )
        await conn.close()
        return

    logger.info(
        "services: %s → %s (pid %d) exec %s %s",
        conn.remote_addr, route.called, proc.pid, route.exec_path, argv[1:],
    )

    last_activity = time.monotonic()

    def touch() -> None:
        nonlocal last_activity
        last_activity = time.monotonic()

    async def pump_to_proc() -> None:
        """Radio peer → program stdin."""
        assert proc.stdin is not None
        try:
            while True:
                data = await conn.reader.read(_READ_CHUNK)
                if not data:
                    break  # peer closed the connection
                touch()
                proc.stdin.write(_to_program(data) if route.crlf else data)
                await proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, ConnectionError):
            pass
        finally:
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception:
                    pass

    async def pump_from_proc() -> None:
        """Program stdout → radio peer."""
        assert proc.stdout is not None
        try:
            while True:
                data = await proc.stdout.read(_READ_CHUNK)
                if not data:
                    break  # program closed stdout / exited
                touch()
                conn.writer.write(_to_radio(data) if route.crlf else data)
                await conn.writer.drain()
        except (ConnectionResetError, BrokenPipeError, ConnectionError):
            pass

    down = asyncio.create_task(pump_to_proc(), name=f"svc-in:{conn.remote_addr}")
    up = asyncio.create_task(pump_from_proc(), name=f"svc-out:{conn.remote_addr}")
    proc_wait = asyncio.create_task(proc.wait(), name=f"svc-proc:{conn.remote_addr}")
    watched = {down, up, proc_wait}

    try:
        while True:
            timeout: float | None = None
            if route.idle_timeout > 0:
                remaining = route.idle_timeout - (time.monotonic() - last_activity)
                if remaining <= 0:
                    logger.info(
                        "services: %s → %s idle %ds — closing",
                        conn.remote_addr, route.called, route.idle_timeout,
                    )
                    break
                timeout = remaining
            done, _ = await asyncio.wait(
                watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if done:
                break  # a pump finished or the process exited
            # else: idle-tick timeout elapsed — re-check inactivity next loop
    finally:
        await _teardown(conn, proc, down, up, proc_wait)

    rc = proc.returncode
    logger.info("services: %s → %s ended (exit %s)", conn.remote_addr, route.called, rc)


async def _teardown(
    conn: "Connection",
    proc: asyncio.subprocess.Process,
    down: asyncio.Task,
    up: asyncio.Task,
    proc_wait: asyncio.Task,
) -> None:
    # 1. stop feeding the program.
    if proc.stdin is not None and not proc.stdin.is_closing():
        try:
            proc.stdin.close()
        except Exception:
            pass
    # 2. end the process if it is still running.
    if proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(asyncio.shield(proc_wait), timeout=_TERM_GRACE)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc_wait
        except ProcessLookupError:
            pass
    # 3. let the upstream pump flush any last program output, then stop pumps.
    if not up.done():
        await asyncio.wait({up}, timeout=_FLUSH_GRACE)
    for t in (up, down, proc_wait):
        if not t.done():
            t.cancel()
    await asyncio.gather(up, down, proc_wait, return_exceptions=True)
    # 4. close the radio connection.
    await conn.close()
