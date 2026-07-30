#!/usr/bin/env python3
"""
netrom_monitor.py — Live NETROM NODES broadcast decoder.

Connects to an AGWPE-compatible server (Direwolf, UZ7HO Soundmodem, …),
registers a passive callsign, enables frame monitoring, and decodes every
NETROM NODES broadcast it receives.  Prints a running routing table.

Usage:
    source .v/bin/activate
    python netrom_monitor.py [host [port]]

    python netrom_monitor.py radiostation2.elcajon 8000

Ctrl-C to stop and print the final routing table.
"""
import asyncio
import struct
import sys
import time

from bbs.ax25.netrom_frame import PID_NETROM, decode_nodes_broadcast
from bbs.transport.agwpe import _HEADER_FMT, _HEADER_SIZE, _extract_binary_info

# Accumulated routing table: dest_call -> RouteInfo
_routes: dict[str, dict] = {}
_frames_total  = 0
_frames_netrom = 0
_start_time    = 0.0


def _decode_call(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()


def _build_frame(kind: str, call_from: str = "", call_to: str = "",
                 data: bytes = b"") -> bytes:
    return struct.pack(
        _HEADER_FMT,
        0, 0, 0, 0,
        ord(kind), 0, 0, 0,
        call_from.encode("ascii")[:9].ljust(10, b"\x00"),
        call_to.encode("ascii")[:9].ljust(10, b"\x00"),
        len(data), 0,
    ) + data


def _print_table() -> None:
    if not _routes:
        return
    elapsed = int(time.time() - _start_time)
    print(f"\n  {'─'*68}")
    print(f"  {'DEST':<10} {'ALIAS':<8} {'NEIGHBOR':<10} {'Q':>3}  ADVERTISED BY")
    print(f"  {'─'*68}")
    for info in sorted(_routes.values(), key=lambda r: r["alias"] or r["dest"]):
        age = int(time.time() - info["ts"])
        print(
            f"  {info['dest']:<10} ({info['alias']:<6}) {info['neighbor']:<10} "
            f"{info['quality']:>3}  {info['via_call']} ({info['via_alias']})  {age}s ago"
        )
    print(
        f"  {'─'*68}  {len(_routes)} nodes  |  "
        f"{_frames_netrom} NETROM / {_frames_total} frames  |  "
        f"up {elapsed}s\n"
    )


def _print_final() -> None:
    print(f"\n{'═'*70}")
    print("  Final routing table")
    _print_table()


async def monitor(host: str, port: int, verbose: bool = False) -> None:
    global _frames_total, _frames_netrom, _start_time
    _start_time = time.time()

    print(f"Connecting to AGWPE at {host}:{port} …")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10.0
        )
    except asyncio.TimeoutError:
        print(f"Connection timed out after 10s — host unreachable or port blocked.")
        print(f"If the BBS is running, it may already hold the AGWPE connection.")
        print(f"Try stopping the BBS first, or check that {host}:{port} is reachable.")
        return
    except OSError as exc:
        print(f"Connection failed: {exc}")
        return
    print("Connected.")

    # Register a passive monitoring callsign then enable monitoring.
    # Direwolf requires registration before it delivers monitored frames.
    mon_call = "W6ELA-9"
    writer.write(_build_frame("X", call_from=mon_call))
    await writer.drain()
    print(f"Sent registration for {mon_call}, waiting for ack …")

    registered = False
    last_heartbeat = time.time()

    try:
        while True:
            try:
                raw = await asyncio.wait_for(reader.readexactly(_HEADER_SIZE), timeout=5.0)
            except asyncio.TimeoutError:
                # Heartbeat: show we're alive even when the band is quiet
                if time.time() - last_heartbeat >= 30:
                    elapsed = int(time.time() - _start_time)
                    print(
                        f"  … {elapsed}s elapsed, "
                        f"{_frames_total} frames / {_frames_netrom} NETROM, "
                        f"{len(_routes)} nodes known"
                    )
                    last_heartbeat = time.time()
                continue
            except asyncio.IncompleteReadError:
                print("Connection closed by server.")
                break

            (
                _agw_port, _, _, _,
                kind_byte, _, pid, _,
                call_from_raw, call_to_raw,
                data_len, _,
            ) = struct.unpack(_HEADER_FMT, raw)

            payload = b""
            if data_len > 0:
                try:
                    payload = await asyncio.wait_for(
                        reader.readexactly(data_len), timeout=10.0
                    )
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    print("Read error on payload — reconnect needed.")
                    break

            kind      = chr(kind_byte)
            call_from = _decode_call(call_from_raw)
            call_to   = _decode_call(call_to_raw)
            _frames_total += 1

            # ── Registration ack ──────────────────────────────────────────
            if kind == "X":
                ok = bool(payload and payload[0] == 1)
                if ok:
                    registered = True
                    print(f"Registration OK.  Enabling monitoring …")
                    writer.write(_build_frame("m"))
                    await writer.drain()
                    print("Monitoring enabled.  Waiting for NETROM NODES broadcasts …\n")
                else:
                    print(f"Registration FAILED (already in use?).  "
                          f"Trying to enable monitoring anyway …")
                    writer.write(_build_frame("m"))
                    await writer.drain()
                continue

            # ── Only process UI frames ────────────────────────────────────
            if kind != "U":
                if verbose:
                    print(f"  [{time.strftime('%H:%M:%S')}] {kind!r:4s} pid=0x{pid:02X}  "
                          f"{call_from} -> {call_to}  len={data_len}")
                continue

            if verbose:
                preview = payload[:50].decode("ascii", errors="replace").replace("\r", " ")
                print(f"  [{time.strftime('%H:%M:%S')}] UI   pid=0x{pid:02X}  "
                      f"{call_from} -> {call_to}  {preview!r}")

            # Detect NETROM frames by destination ("NODES" broadcast) or PID.
            # Some AGWPE implementations (including Direwolf) report PID=0x00
            # in the AGWPE header for monitored frames, so pid alone is unreliable.
            is_nodes = call_to.upper() == "NODES"
            is_netrom_pid = pid == PID_NETROM

            if not is_nodes and not is_netrom_pid:
                # Plain non-NETROM UI frame — beacon, APRS, etc.
                if not verbose:
                    preview = payload[:60].decode("ascii", errors="replace").replace("\r", " ")
                    print(f"  [{time.strftime('%H:%M:%S')}] UI  pid=0x{pid:02X}  "
                          f"{call_from} -> {call_to}  {preview!r}")
                continue

            # ── NETROM frame ──────────────────────────────────────────────
            _frames_netrom += 1
            binary_info = _extract_binary_info(payload)

            if binary_info is None:
                print(f"  [{time.strftime('%H:%M:%S')}] NETROM from {call_from} "
                      f"— could not extract binary payload (pid=0x{pid:02X})")
                print(f"    raw payload hex: {payload[:32].hex()}")
                continue

            if not is_nodes:
                # L3/L4 data frame addressed to a specific node
                print(f"  [{time.strftime('%H:%M:%S')}] NETROM L3  {call_from} -> {call_to}  "
                      f"{len(binary_info)} bytes: {binary_info[:8].hex()}")
                continue

            frame = decode_nodes_broadcast(call_from, binary_info)
            if frame is None:
                print(f"  [{time.strftime('%H:%M:%S')}] NODES from {call_from} "
                      f"— decode failed  pid=0x{pid:02X}  {len(binary_info)} bytes: "
                      f"{binary_info[:16].hex()}")
                continue

            ts = time.time()
            print(
                f"  [{time.strftime('%H:%M:%S')}] NODES from {call_from:10s} "
                f"({frame.source_alias})  —  {len(frame.entries)} entries  "
                f"[pid=0x{pid:02X}]"
            )
            for e in frame.entries:
                _routes[e.dest_call.upper()] = dict(
                    dest=e.dest_call, alias=e.alias,
                    neighbor=e.neighbor_call, quality=e.quality,
                    via_call=frame.source_call, via_alias=frame.source_alias,
                    ts=ts,
                )
            _print_table()
            last_heartbeat = time.time()

    except asyncio.CancelledError:
        pass
    finally:
        writer.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    _verbose = "-v" in sys.argv or "--verbose" in sys.argv
    _host = args[0] if len(args) > 0 else "127.0.0.1"
    _port = int(args[1]) if len(args) > 1 else 8000
    try:
        asyncio.run(monitor(_host, _port, verbose=_verbose))
    except KeyboardInterrupt:
        pass
    finally:
        _print_final()
