"""
tests/test_radio_rig.py — Integration test for the multi-Direwolf rig.

Skipped automatically when the ``direwolf`` binary isn't on PATH.

This test spawns the full rig (4 Direwolf processes wired through the Python
audio bus), registers an AGWPE listener at the BBS node, and triggers an AX.25
SABM from the station node through both digipeaters. The test passes when both
ends print ``Stream 0: Connected to ...`` — exercising the audio path, the
sample-paced mixer, AX.25 digipeating, and the UA return trip.
"""
from __future__ import annotations

import random
import shutil
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from queue import Empty, Queue

import pytest

_DIREWOLF = shutil.which("direwolf")
_RIG_SCRIPT = Path(__file__).resolve().parent / "rig" / "radio_rig.py"

pytestmark = pytest.mark.skipif(
    _DIREWOLF is None, reason="direwolf binary not on PATH",
)


def _free_tcp_port() -> int:
    """Find a free TCP port in Direwolf's accepted AGWPORT range [1024, 49151].

    On macOS, bind(0) hands out ephemeral ports above 49152 which Direwolf
    rejects, so we have to pick within range explicitly.
    """
    for _ in range(200):
        port = random.randint(20000, 49000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free TCP port found in 20000-49000")


def _free_udp_range(count: int, start: int = 30000) -> int:
    """Find a contiguous range of ``count`` free UDP ports."""
    for base in range(start, 60000, 100):
        socks: list[socket.socket] = []
        try:
            ok = True
            for i in range(count):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.bind(("127.0.0.1", base + i))
                    socks.append(s)
                except OSError:
                    ok = False
                    break
            if ok:
                return base
        finally:
            for s in socks:
                s.close()
    raise RuntimeError(f"no free UDP range of {count} starting from {start}")


def _build_yaml(agw_station: int, agw_bbs: int, udp_base: int) -> str:
    return textwrap.dedent(f"""\
        rig:
          audio: {{ sample_rate: 44100, channels: 1, sample_bits: 16 }}
          udp_base_port: {udp_base}
          direwolf: {{ maxframe: 1, emaxframe: 1 }}
          nodes:
            - {{ callsign: N6TEST,  type: station, agwpe_port: {agw_station} }}
            - {{ callsign: HMKR,    type: digi }}
            - {{ callsign: KRDG,    type: digi }}
            - {{ callsign: W6ELA-1, type: bbs,     agwpe_port: {agw_bbs} }}
          links:
            - {{ between: [N6TEST,  HMKR],    delay_ms: 5, loss_rate: 0.0 }}
            - {{ between: [HMKR,    KRDG],    delay_ms: 5, loss_rate: 0.0 }}
            - {{ between: [KRDG,    W6ELA-1], delay_ms: 5, loss_rate: 0.0 }}
    """)


def _agw_header(datakind: str, call_from: str, call_to: str,
                data: bytes = b"", pid: int = 0) -> bytes:
    return struct.pack(
        "<BBBBBBBB10s10sII",
        0, 0, 0, 0,
        ord(datakind), 0, pid, 0,
        call_from.upper().encode().ljust(10, b"\x00")[:10],
        call_to.upper().encode().ljust(10, b"\x00")[:10],
        len(data), 0,
    )


def _wait_for_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"AGWPE port {port} did not open within {timeout:.0f}s")


def _reader_thread(stream, q: Queue) -> None:
    for line in iter(stream.readline, ""):
        q.put(line)
    q.put(None)


def test_sabm_round_trip_through_digi_chain(tmp_path: Path) -> None:
    """End-to-end: N6TEST → HMKR → KRDG → W6ELA-1, UA returns, both ends Connected."""
    agw_station = _free_tcp_port()
    agw_bbs = _free_tcp_port()
    udp_base = _free_udp_range(8)

    cfg_path = tmp_path / "rig.yaml"
    cfg_path.write_text(_build_yaml(agw_station, agw_bbs, udp_base))

    # --verbose multiplexes per-Direwolf stdout into the rig's stdout, which is
    # how we observe AX.25 connect events from this test.
    proc = subprocess.Popen(
        [sys.executable, str(_RIG_SCRIPT), "--config", str(cfg_path), "--verbose"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    q: Queue = Queue()
    reader = threading.Thread(target=_reader_thread, args=(proc.stdout, q), daemon=True)
    reader.start()

    listener = client = None
    captured: list[str] = []
    try:
        _wait_for_port(agw_station)
        _wait_for_port(agw_bbs)

        # Register W6ELA-1 as a listener so its Direwolf returns UA rather than DM.
        listener = socket.create_connection(("127.0.0.1", agw_bbs))
        listener.sendall(_agw_header('P', "W6ELA-1", ""))
        listener.sendall(_agw_header('X', "W6ELA-1", ""))

        # Trigger SABM from N6TEST with the digi via-path.
        client = socket.create_connection(("127.0.0.1", agw_station))
        client.sendall(_agw_header('P', "N6TEST", ""))
        via = ["HMKR", "KRDG"]
        data = bytes([len(via)]) + b"".join(
            v.encode().ljust(10, b"\x00")[:10] for v in via
        )
        client.sendall(_agw_header('v', "N6TEST", "W6ELA-1", data, pid=0xF0) + data)

        # Both ends must announce a Connected stream within the deadline.
        need_bbs = "Stream 0: Connected to N6TEST"      # logged by W6ELA-1
        need_sta = "Stream 0: Connected to W6ELA-1"     # logged by N6TEST
        deadline = time.monotonic() + 30.0
        seen_bbs = seen_sta = False
        while time.monotonic() < deadline and not (seen_bbs and seen_sta):
            try:
                line = q.get(timeout=0.5)
            except Empty:
                continue
            if line is None:
                break
            captured.append(line.rstrip())
            if need_bbs in line:
                seen_bbs = True
            if need_sta in line:
                seen_sta = True

        if not (seen_bbs and seen_sta):
            tail = "\n".join(captured[-60:])
            pytest.fail(
                "chain did not connect end-to-end within 30s\n"
                f"  W6ELA-1 saw Connected: {seen_bbs}\n"
                f"  N6TEST  saw Connected: {seen_sta}\n"
                f"  --- last 60 lines of rig output ---\n{tail}"
            )

    finally:
        for sock in (client, listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
