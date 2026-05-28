#!/usr/bin/env python3
"""
Multi-Direwolf audio chain test rig.

Reads a YAML config, generates per-node Direwolf configs, starts all Direwolf
processes with stdin/stdout as raw PCM audio pipes, then routes audio between
adjacent nodes with configurable per-link propagation delay and packet loss.

Usage:
    python tests/rig/radio_rig.py [--config tests/rig/rig.yaml] [--verbose]

The rig stays running until you press Ctrl+C.  Start bbs2 separately with
its AGWPE port pointed at the BBS node's agwpe_port.

AGWPE test client connection
-----------------------------
To connect from the station node through digipeaters to the BBS, use the
AGWPE 'v' (Connect VIA) frame from your test client:

    import struct, asyncio
    HEADER_FMT = "<BBBBBB2sxxx10s10sII"   # 36 bytes
    def agwpe_connect_via(call_from, call_to, via_path):
        # via_path: list of callsign strings, e.g. ["HMKR", "KRDG"]
        num_digi = len(via_path)
        data = bytes([num_digi])
        for cs in via_path:
            data += cs.encode().ljust(10, b"\\x00")
        hdr = struct.pack(
            "<BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            0, 0, 0, 0,          # portx, reserved x3
            ord('v'), 0, 0, 0,   # datakind, reserved x3
            *call_from.encode().ljust(10, b"\\x00"),
            *call_to.encode().ljust(10, b"\\x00"),
            len(data), 0, 0, 0,  # data_len (little-endian 4 bytes)
            0, 0, 0, 0,          # user_reserved
        )
        return hdr + data

See also: connect_via() helper at the bottom of this file.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import random
import shutil
import signal
import struct
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import yaml

log = logging.getLogger("rig")


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AudioConfig:
    sample_rate: int = 44100
    channels: int = 1
    sample_bits: int = 16

    @property
    def bytes_per_sample(self) -> int:
        return self.channels * (self.sample_bits // 8)

    def chunk_bytes(self, duration_ms: int = 20) -> int:
        """Number of raw PCM bytes for `duration_ms` milliseconds of audio."""
        samples = int(self.sample_rate * duration_ms / 1000)
        return samples * self.bytes_per_sample


@dataclass
class NodeConfig:
    callsign: str
    type: str               # "station" | "digi" | "bbs"
    agwpe_port: Optional[int] = None
    udp_in_port: int = 0    # Direwolf listens here (Python → Direwolf audio)
    udp_out_port: int = 0   # Direwolf sends here  (Direwolf → Python audio)


@dataclass
class LinkConfig:
    between: tuple[str, str]    # callsigns of the two endpoints
    delay_ms: int = 0
    loss_rate: float = 0.0      # 0.0 = no loss, 1.0 = all dropped


@dataclass
class RigConfig:
    audio: AudioConfig
    nodes: list[NodeConfig]
    links: list[LinkConfig]
    direwolf_params: dict[str, object] = field(default_factory=dict)
    udp_base_port: int = 11000
    send_silence: bool = True


def _auto_frack(nodes: list[NodeConfig], links: list[LinkConfig]) -> int:
    """Compute T1 (FRACK) in seconds large enough to cover an end-to-end RTT.

    Per-hop budget assumes one short frame (~1.3s burst) + DCD + slot avg
    ≈ link_delay + 1726ms. Uses node count as a worst-case hop count.
    Clamped to [4, 15] (Direwolf compile-time limits).
    """
    n_hops = max(len(nodes) - 1, 1)
    avg_delay_ms = sum(lk.delay_ms for lk in links) / max(len(links), 1)
    one_way_ms = n_hops * (avg_delay_ms + 1726)
    rtt_s = 2 * one_way_ms / 1000
    return max(4, min(15, int(rtt_s) + 2))


def load_config(path: str) -> RigConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    rig = raw["rig"]

    audio_raw = rig.get("audio", {})
    audio = AudioConfig(
        sample_rate=audio_raw.get("sample_rate", 44100),
        channels=audio_raw.get("channels", 1),
        sample_bits=audio_raw.get("sample_bits", 16),
    )

    udp_base = rig.get("udp_base_port", 11000)
    nodes = []
    for i, n in enumerate(rig["nodes"]):
        nodes.append(NodeConfig(
            callsign=n["callsign"],
            type=n["type"],
            agwpe_port=n.get("agwpe_port"),
            udp_in_port=udp_base + i * 2,
            udp_out_port=udp_base + i * 2 + 1,
        ))

    callsigns = {n.callsign for n in nodes}
    links = []
    for idx, lk in enumerate(rig.get("links", [])):
        if "between" not in lk:
            raise ValueError(
                f"links[{idx}] is missing 'between: [A, B]' (the rig now uses "
                f"explicit pairs instead of positional adjacency)"
            )
        pair = lk["between"]
        if not (isinstance(pair, list) and len(pair) == 2):
            raise ValueError(f"links[{idx}].between must be a list of two callsigns")
        a, b = pair
        if a == b:
            raise ValueError(f"links[{idx}].between has the same callsign on both ends")
        for cs in (a, b):
            if cs not in callsigns:
                raise ValueError(f"links[{idx}].between refers to unknown callsign {cs!r}")
        links.append(LinkConfig(
            between=(a, b),
            delay_ms=lk.get("delay_ms", 0),
            loss_rate=float(lk.get("loss_rate", 0.0)),
        ))

    direwolf_params: dict[str, object] = {}
    for k, v in (rig.get("direwolf") or {}).items():
        direwolf_params[k.upper()] = v

    return RigConfig(
        audio=audio, nodes=nodes, links=links,
        direwolf_params=direwolf_params, udp_base_port=udp_base,
    )


# ---------------------------------------------------------------------------
# Direwolf config generation
# ---------------------------------------------------------------------------

_DW_CONF_TEMPLATE = textwrap.dedent("""\
    # Auto-generated by radio_rig.py — do not edit
    MYCALL {callsign}
    ADEVICE udp:{udp_in} udp:127.0.0.1:{udp_out}
    ARATE {sample_rate}
    ACHANNELS {channels}
    CHANNEL 0
    MODEM 1200
    {extra}
""")


def generate_dw_conf(
    node: NodeConfig,
    audio: AudioConfig,
    direwolf_params: dict[str, object],
    auto_frack: int,
) -> str:
    extra_lines: list[str] = []

    # Direwolf AX.25 / CSMA tuning from rig.direwolf (uppercased keys).
    # FRACK is special: if not in the user's dict, we auto-derive it from the
    # topology so default configs cover end-to-end RTT.
    params = dict(direwolf_params)
    params.setdefault("FRACK", auto_frack)
    for key, value in params.items():
        extra_lines.append(f"{key} {value}")

    if node.type in ("station", "bbs"):
        if node.agwpe_port is None:
            raise ValueError(
                f"Node {node.callsign!r} has type={node.type!r} but no agwpe_port"
            )
        extra_lines.append(f"AGWPORT {node.agwpe_port}")
        extra_lines.append("KISSPORT 0")   # disable default KISS TCP server

    if node.type == "digi":
        # CDIGIPEAT enables connected-mode (AX.25 layer-2) digipeating.
        # "CDIGIPEAT 0 0" = channel 0 receives, channel 0 retransmits.
        # No filtering alias is set — repeat anything with our callsign as
        # the next unused digipeater in the via path.
        extra_lines.append("CDIGIPEAT 0 0")
        extra_lines.append("AGWPORT 0")    # disable default AGWPE server
        extra_lines.append("KISSPORT 0")   # disable default KISS TCP server

    return _DW_CONF_TEMPLATE.format(
        callsign=node.callsign,
        sample_rate=audio.sample_rate,
        channels=audio.channels,
        udp_in=node.udp_in_port,
        udp_out=node.udp_out_port,
        extra="\n".join(extra_lines) if extra_lines else "# (no extra config)",
    )


def write_dw_configs(cfg: RigConfig, tmpdir: str) -> dict[str, str]:
    """Write a Direwolf .conf for each node into tmpdir.
    Returns {callsign: conf_path}."""
    paths = {}
    auto_frack = _auto_frack(cfg.nodes, cfg.links)
    for node in cfg.nodes:
        conf_text = generate_dw_conf(node, cfg.audio, cfg.direwolf_params, auto_frack)
        conf_path = os.path.join(tmpdir, f"dw-{node.callsign}.conf")
        with open(conf_path, "w") as f:
            f.write(conf_text)
        log.debug("Wrote %s", conf_path)
        paths[node.callsign] = conf_path
    return paths


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

_running_procs: list[asyncio.subprocess.Process] = []
_tmpdir: Optional[str] = None


def _cleanup() -> None:
    for proc in _running_procs:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
    if _tmpdir and os.path.isdir(_tmpdir):
        shutil.rmtree(_tmpdir, ignore_errors=True)


atexit.register(_cleanup)


async def start_direwolf(
    conf_path: str,
    log_path: str,
) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        "direwolf", "-c", conf_path, "-t", "0",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _running_procs.append(proc)
    log.debug("Started direwolf pid=%d conf=%s log=%s", proc.pid, conf_path, log_path)
    return proc


async def _stream_stdout(
    proc: asyncio.subprocess.Process,
    callsign: str,
    log_path: str,
    verbose: bool,
) -> None:
    """Read Direwolf's stdout (text log), write to log file, and optionally echo."""
    assert proc.stdout is not None
    with open(log_path, "w") as log_file:
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            log_file.write(line + "\n")
            log_file.flush()
            if verbose:
                print(f"[{callsign}] {line}", flush=True)


# ---------------------------------------------------------------------------
# RadioBus — UDP audio routing between adjacent Direwolf processes
# ---------------------------------------------------------------------------

async def _edge_relay(
    src_q: asyncio.Queue,
    delay_s: float,
    loss_rate: float,
    dest_q: asyncio.Queue,
    label: str = "",
) -> None:
    """Apply delay + loss, then deposit audio into the destination node's rx queue.

    Each packet is delayed concurrently so packet N+1 does not wait for packet N's
    sleep to finish — all in-flight packets sleep in parallel, preserving the
    inter-packet timing of the original audio stream.
    """
    async def _deliver(chunk: bytes) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        if loss_rate > 0.0 and random.random() < loss_rate:
            return
        try:
            dest_q.put_nowait(chunk)
        except asyncio.QueueFull:
            log.warning("edge_relay %s: dest_q FULL, dropping (qsize=%d)", label, dest_q.qsize())

    while True:
        chunk = await src_q.get()
        if chunk is None:
            return
        asyncio.create_task(_deliver(chunk))


async def _per_destination_mixer(
    dest_port: int,
    audio: AudioConfig,
    source_queues: dict[int, asyncio.Queue],
    stop_event: asyncio.Event,
    send_silence: bool = True,
) -> None:
    """Per-destination audio mixer feeding one Direwolf udp_in_port.

    Drains incoming chunks into per-source byte buffers, then emits fixed-size
    output chunks paced at real-time audio rate. Direwolf bursts its TX audio
    (the whole frame may arrive in milliseconds), so pacing here is what makes
    the receiver process it at the original wall-clock cadence.

    When multiple sources have buffered audio at the same moment, their int16
    samples are summed (saturating) to model additive RF collision. Otherwise
    a source's buffer is forwarded verbatim.

    Silence is *never* injected mid-burst on chunk-arrival jitter — only after
    a true idle gap (IDLE_THRESHOLD_S), then at a steady cadence so the
    receiving Direwolf's DCD can settle to "no carrier".
    """
    # Direwolf's UDP TX uses ~2000-byte chunks (1000 samples at 44100Hz/16/mono
    # = 22.68ms). Emit at the same size and cadence so the receiver sees a
    # real-time audio stream.
    CHUNK_BYTES = 2000
    CHUNK_PERIOD_S = CHUNK_BYTES / (audio.sample_rate * audio.bytes_per_sample)
    IDLE_THRESHOLD_S = 0.08     # grace period after last real audio
    SILENCE_INTERVAL_S = 0.04   # silence cadence once idle

    loop = asyncio.get_event_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol,
        remote_addr=("127.0.0.1", dest_port),
    )

    bps = audio.bytes_per_sample
    silence_chunk = b"\x00" * CHUNK_BYTES

    source_bufs: dict[int, bytearray] = {i: bytearray() for i in source_queues}

    # Drain task per source pushes chunks into a single merged signal queue.
    # We only use merged as a wakeup signal — actual bytes live in source_bufs.
    merged: asyncio.Queue = asyncio.Queue(maxsize=2048)

    async def _drain(src_idx: int, q: asyncio.Queue) -> None:
        while True:
            chunk = await q.get()
            if chunk is None:
                return
            try:
                merged.put_nowait((src_idx, chunk))
            except asyncio.QueueFull:
                log.warning("mixer[%d] merged FULL, dropping chunk from src=%d",
                            dest_port, src_idx)

    drain_tasks = [
        asyncio.create_task(_drain(i, q), name=f"drain-mix-{dest_port}-{i}")
        for i, q in source_queues.items()
    ]

    def _drain_merged_nowait() -> None:
        """Pull everything currently in merged into source_bufs (non-blocking)."""
        while True:
            try:
                src, chunk = merged.get_nowait()
            except asyncio.QueueEmpty:
                return
            source_bufs[src].extend(chunk)

    def _emit_one_chunk() -> bool:
        """Emit at most one CHUNK_BYTES packet from source_bufs.
        Returns True if a real-audio packet was emitted, False if no source had data."""
        active = [(i, buf) for i, buf in source_bufs.items() if len(buf) > 0]
        if not active:
            return False
        if len(active) == 1:
            src_idx, buf = active[0]
            n = min(CHUNK_BYTES, len(buf))
            transport.sendto(bytes(buf[:n]))
            del buf[:n]
        else:
            min_bytes = min(len(buf) for _, buf in active)
            min_bytes = min(min_bytes, CHUNK_BYTES)
            min_bytes -= min_bytes % bps
            if min_bytes == 0:
                return False
            arrays = [
                np.frombuffer(bytes(buf[:min_bytes]), dtype=np.int16).astype(np.int32)
                for _, buf in active
            ]
            mix32 = np.sum(np.stack(arrays), axis=0)
            mix16 = np.clip(mix32, -32768, 32767).astype(np.int16)
            transport.sendto(mix16.tobytes())
            for _, buf in active:
                del buf[:min_bytes]
        return True

    # Initialize as "no real audio seen yet" so silence starts immediately.
    last_real_audio = 0.0
    last_emit = time.monotonic()

    try:
        while not stop_event.is_set():
            _drain_merged_nowait()
            has_audio = any(len(buf) > 0 for buf in source_bufs.values())

            if has_audio:
                # Pace: wait until the next chunk slot before emitting
                now = time.monotonic()
                next_slot = last_emit + CHUNK_PERIOD_S
                if next_slot > now:
                    await asyncio.sleep(next_slot - now)
                _drain_merged_nowait()  # pick up anything that arrived during sleep
                if _emit_one_chunk():
                    last_emit = time.monotonic()
                    last_real_audio = last_emit
                continue

            # No real audio buffered — wait for arrival or silence-keepalive tick
            now = time.monotonic()
            quiet_for = now - last_real_audio
            if quiet_for < IDLE_THRESHOLD_S:
                timeout = IDLE_THRESHOLD_S - quiet_for
            else:
                timeout = SILENCE_INTERVAL_S

            try:
                src, chunk = await asyncio.wait_for(merged.get(), timeout=timeout)
                source_bufs[src].extend(chunk)
                # Loop back — has_audio is now True so we'll pace+emit next iteration
            except asyncio.TimeoutError:
                if send_silence and (time.monotonic() - last_real_audio) >= IDLE_THRESHOLD_S:
                    transport.sendto(silence_chunk)
                    last_emit = time.monotonic()
    finally:
        for t in drain_tasks:
            t.cancel()
        transport.close()


# ---------------------------------------------------------------------------

class _UDPReceiver(asyncio.DatagramProtocol):
    """Receives UDP datagrams from Direwolf's audio output and queues them."""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._q = queue

    def datagram_received(self, data: bytes, addr: tuple) -> None:  # type: ignore[override]
        try:
            self._q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def error_received(self, exc: Exception) -> None:
        log.debug("UDP receiver error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


async def _fan_out_from_queue(
    src_q: asyncio.Queue,
    callsign: str,
    edge_queues: list[asyncio.Queue],
) -> None:
    """Read audio datagrams from src_q and copy to each edge queue."""
    while True:
        chunk = await src_q.get()
        if chunk is None:
            for q in edge_queues:
                await q.put(None)
            return
        for idx, q in enumerate(edge_queues):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                log.debug("%s: edge queue %d full, dropping datagram", callsign, idx)


async def run_radio_bus(cfg: RigConfig) -> None:
    """
    Build and run the RadioBus.

    Topology is defined by the explicit `links` in the YAML (each link is a
    bidirectional pair). For each directed edge i→j:
      - The fan-out copies node i's TX audio into a per-edge queue.
      - _edge_relay applies the link's delay + loss.
      - _per_destination_mixer at j sums concurrently-arriving samples from
        every source that can reach j, then emits one audio stream to
        Direwolf's udp_in_port.

    Unlisted pairs are out of RF range (no audio delivery in either direction)
    — that's how the hidden-transmitter scenario is modelled.
    """
    n = len(cfg.nodes)
    loop = asyncio.get_event_loop()
    name_to_idx = {node.callsign: i for i, node in enumerate(cfg.nodes)}

    # Build directed-edge map: (src_idx, dst_idx) -> (delay_s, loss_rate)
    edges: dict[tuple[int, int], tuple[float, float]] = {}
    for lk in cfg.links:
        a, b = lk.between
        i, j = name_to_idx[a], name_to_idx[b]
        edges[(i, j)] = (lk.delay_ms / 1000.0, lk.loss_rate)
        edges[(j, i)] = (lk.delay_ms / 1000.0, lk.loss_rate)

    # Per-node TX capture queues
    recv_queues: list[asyncio.Queue] = [asyncio.Queue(maxsize=256) for _ in range(n)]

    # Per-edge queues feeding _edge_relay
    edge_in: dict[tuple[int, int], asyncio.Queue] = {
        e: asyncio.Queue(maxsize=128) for e in edges
    }

    # Per-destination mixer input queues, keyed by source idx
    mixer_in: list[dict[int, asyncio.Queue]] = [{} for _ in range(n)]
    for (i, j) in edges:
        mixer_in[j][i] = asyncio.Queue(maxsize=128)

    # UDP receivers (Direwolf TX out → recv_queues)
    recv_transports: list[asyncio.DatagramTransport] = []
    for i, node in enumerate(cfg.nodes):
        transport, _ = await loop.create_datagram_endpoint(
            lambda q=recv_queues[i]: _UDPReceiver(q),
            local_addr=("127.0.0.1", node.udp_out_port),
        )
        recv_transports.append(transport)  # type: ignore[arg-type]

    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    # Fan-out per source node into each reachable destination's edge_in queue
    for i, node in enumerate(cfg.nodes):
        out_qs = [edge_in[(i, j)] for j in range(n) if (i, j) in edges]
        if out_qs:
            tasks.append(asyncio.create_task(
                _fan_out_from_queue(recv_queues[i], node.callsign, out_qs),
                name=f"fanout-{node.callsign}",
            ))

    # Edge relays: per-link delay + loss; delivers into the mixer's per-source queue
    for (i, j), (delay_s, loss) in edges.items():
        tasks.append(asyncio.create_task(
            _edge_relay(edge_in[(i, j)], delay_s, loss, mixer_in[j][i],
                        label=f"{cfg.nodes[i].callsign}->{cfg.nodes[j].callsign}"),
            name=f"edge-{cfg.nodes[i].callsign}->{cfg.nodes[j].callsign}",
        ))

    # Per-destination mixers
    mixer_tasks: list[asyncio.Task] = []
    for j, node in enumerate(cfg.nodes):
        if mixer_in[j]:
            mixer_tasks.append(asyncio.create_task(
                _per_destination_mixer(
                    node.udp_in_port, cfg.audio, mixer_in[j], stop_event, cfg.send_silence,
                ),
                name=f"mixer-{node.callsign}",
            ))

    all_tasks = tasks + mixer_tasks
    log.info(
        "RadioBus running — %d nodes, %d directed edges, %d mixer(s)",
        n, len(edges), len(mixer_tasks),
    )
    try:
        await asyncio.gather(*all_tasks)
    except asyncio.CancelledError:
        for t in all_tasks:
            t.cancel()
        raise
    finally:
        stop_event.set()
        for t in mixer_tasks:
            t.cancel()
        for t in recv_transports:
            t.close()


# ---------------------------------------------------------------------------
# Readiness check
# ---------------------------------------------------------------------------

async def wait_for_agwpe(host: str, port: int, timeout: float = 30.0) -> None:
    """Poll until the AGWPE port accepts a TCP connection."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except (ConnectionRefusedError, OSError):
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(
                    f"AGWPE port {port} not ready after {timeout:.0f}s"
                )
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# AGWPE helper — outgoing connect-via frame builder
# ---------------------------------------------------------------------------

def build_agwpe_connect_via_frame(
    call_from: str,
    call_to: str,
    via_path: list[str],
    agw_port: int = 0,
) -> bytes:
    """
    Build a 36-byte AGWPE header + via-path data for an outgoing connected-mode
    connect request through digipeaters (AGWPE datakind = 'v').

    Direwolf server.c (cases 'C'/'v'/'c') expects data as:
        struct via_info {
            uint8_t  num_digi;      // number of digipeaters (1..7)
            char     dcall[7][10];  // digipeater callsigns, null-padded to 10 bytes
        };
    """
    if not (1 <= len(via_path) <= 7):
        raise ValueError(f"via_path must have 1–7 entries, got {len(via_path)}")

    # Build data payload
    data = bytes([len(via_path)])
    for cs in via_path:
        data += cs.upper().encode("ascii").ljust(10, b"\x00")[:10]

    # Build 36-byte header (little-endian layout):
    #   portx(1) reserved(3) datakind(1) reserved(1) pid(1) reserved(1)
    #   call_from(10) call_to(10) data_len(4) user_reserved(4)
    hdr = struct.pack(
        "<BBBBBBBB10s10sII",
        agw_port, 0, 0, 0,          # portx + 3 reserved
        ord('v'), 0, 0xF0, 0,       # datakind='v', reserved, pid=0xF0, reserved
        call_from.upper().encode("ascii").ljust(10, b"\x00")[:10],
        call_to.upper().encode("ascii").ljust(10, b"\x00")[:10],
        len(data),                  # data_len
        0,                          # user_reserved
    )
    return hdr + data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    global _tmpdir

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg_path = Path(args.config).resolve()
    log.info("Loading config: %s", cfg_path)
    cfg = load_config(str(cfg_path))
    cfg.send_silence = not args.no_silence

    _tmpdir = tempfile.mkdtemp(prefix="bbs2_rig_")
    log.info("Temp dir: %s", _tmpdir)

    # Generate Direwolf configs
    conf_paths = write_dw_configs(cfg, _tmpdir)

    # Start Direwolf processes
    procs: list[asyncio.subprocess.Process] = []
    stdout_tasks: list[asyncio.Task] = []
    for node in cfg.nodes:
        log_path = os.path.join(_tmpdir, f"dw-{node.callsign}.log")
        proc = await start_direwolf(conf_paths[node.callsign], log_path)
        procs.append(proc)
        stdout_tasks.append(asyncio.create_task(
            _stream_stdout(proc, node.callsign, log_path, args.verbose),
            name=f"stdout-{node.callsign}",
        ))
        log.info(
            "Started %-12s (pid %d)%s",
            node.callsign,
            proc.pid,
            f"  AGWPE :{node.agwpe_port}" if node.agwpe_port else "",
        )
        log.debug("  log: %s  UDP in:%d out:%d",
                  log_path, node.udp_in_port, node.udp_out_port)

    # Start RadioBus
    bus_task = asyncio.create_task(
        run_radio_bus(cfg),
        name="radio-bus",
    )

    # Wait for AGWPE ports to become ready
    agwpe_nodes = [(n, p) for n, p in zip(cfg.nodes, procs) if n.agwpe_port]
    for node, _proc in agwpe_nodes:
        log.info("Waiting for %s AGWPE port %d ...", node.callsign, node.agwpe_port)
        await wait_for_agwpe("127.0.0.1", node.agwpe_port)  # type: ignore[arg-type]
        log.info("  %s ready", node.callsign)

    station = next(n for n in cfg.nodes if n.type == "station")
    bbs     = next(n for n in cfg.nodes if n.type == "bbs")
    digi_calls = [n.callsign for n in cfg.nodes if n.type == "digi"]

    print()
    print("=" * 60)
    print("Rig ready")
    print(f"  Station AGWPE : localhost:{station.agwpe_port}")
    print(f"  BBS     AGWPE : localhost:{bbs.agwpe_port}")
    if digi_calls:
        print(f"  Via path      : {','.join(digi_calls)}")
    print()
    print("To connect from station to BBS (AGWPE 'v' frame):")
    frame = build_agwpe_connect_via_frame(
        station.callsign, bbs.callsign, digi_calls
    )
    print(f"  call_from={station.callsign!r}  call_to={bbs.callsign!r}"
          f"  via={digi_calls}")
    print(f"  frame ({len(frame)} bytes): {frame.hex()}")
    print()
    print("Press Ctrl+C to stop the rig.")
    print("=" * 60)

    # Block until shutdown
    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Shutdown signal received")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop.wait()

    log.info("Shutting down ...")
    for t in [bus_task] + stdout_tasks:
        t.cancel()
    await asyncio.gather(bus_task, *stdout_tasks, return_exceptions=True)

    for proc in procs:
        if proc.returncode is None:
            proc.terminate()
    await asyncio.gather(*(proc.wait() for proc in procs), return_exceptions=True)

    if _tmpdir:
        shutil.rmtree(_tmpdir, ignore_errors=True)
        _tmpdir = None

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Multi-Direwolf audio chain test rig for bbs2"
    )
    parser.add_argument(
        "--config", "-c",
        default=str(Path(__file__).parent / "rig.yaml"),
        help="Path to rig YAML config (default: rig.yaml next to this script)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show DEBUG logging and Direwolf log file paths",
    )
    parser.add_argument(
        "--no-silence",
        action="store_true",
        help="Do not send silence on idle links (for debugging only, breaks channel state detection)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
