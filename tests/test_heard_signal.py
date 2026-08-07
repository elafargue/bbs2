"""
tests/test_heard_signal.py — RF signal-quality storage (heard schema v5).

Verifies on_heard() persists per-frame signal quality to heard_events for
directly-heard stations, skips digipeated frames, and that the v5 columns
migrate onto a pre-v5 database.
"""
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

import aiosqlite

import server.routes.heard  # noqa: F401 — registers the /api/heard routes
from bbs.config import BBSConfig
from bbs.core.engine import BBSEngine
from bbs.plugins.heard.heard import HeardPlugin


def _cfg(db_path: str) -> BBSConfig:
    return BBSConfig(
        callsign="W6ELA", ssid=1, name="T", sysop="W6ELA", location="",
        max_users=10, idle_timeout=60, write_timeout=30,
        path_length_medium_hops=1, path_length_long_hops=3,
        transports={}, database={"path": db_path}, auth={}, plugins={},
        web={}, logging={}, netrom={}, services={},
    )


def _client(monkeypatch, db_path: str, sysop: bool = True):
    import server.app as sapp
    monkeypatch.setattr(sapp, "bbs_engine", BBSEngine(_cfg(db_path)))
    sapp.app.secret_key = "test"
    c = sapp.app.test_client()
    if sysop:
        with c.session_transaction() as s:
            s["sysop"] = True
    return c

_SIG_COLS = {
    "last_level", "last_tone_mark", "last_tone_space",
    "last_copy_quality", "best_level",
}


def _tmp() -> str:
    return str(Path(tempfile.mkdtemp(prefix="bbs2_sig_")) / "t.db")


async def _plugin(db_path: str) -> HeardPlugin:
    p = HeardPlugin()
    await p.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    return p


async def _row(db_path: str, call: str, transport: str | None = None) -> dict:
    q = "SELECT * FROM heard_events WHERE callsign = ?"
    args: list = [call]
    if transport is not None:
        q += " AND transport = ?"
        args.append(transport)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, args) as cur:
            r = await cur.fetchone()
            return dict(r) if r else {}


async def _columns(db_path: str) -> set[str]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(heard_events)") as cur:
            return {r[1] for r in await cur.fetchall()}


async def test_fresh_db_has_signal_columns():
    db_path = _tmp()
    await _plugin(db_path)
    assert _SIG_COLS <= await _columns(db_path)


async def test_direct_signal_stored():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    await p.on_heard("N6ZX", "BEACON", [], now, "agwpe", info="hi",
                     signal=(28, 10, 6, 0))
    row = await _row(db_path, "N6ZX")
    assert row["last_level"] == 28
    assert row["last_tone_mark"] == 10
    assert row["last_tone_space"] == 6
    assert row["last_copy_quality"] == 0
    assert row["best_level"] == 28


async def test_best_level_tracks_max_last_tracks_latest():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    await p.on_heard("N6ZX", "BEACON", [], now, "agwpe", signal=(40, 10, 6, 0))
    await p.on_heard("N6ZX", "BEACON", [], now + 1, "agwpe", signal=(20, 8, 5, 1))
    row = await _row(db_path, "N6ZX")
    assert row["last_level"] == 20   # most recent reception
    assert row["best_level"] == 40   # strongest over time


async def test_non_afsk_tone_sentinel_stored_as_null():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    await p.on_heard("N6ZX", "BEACON", [], now, "agwpe", signal=(40, 255, 255, 1))
    row = await _row(db_path, "N6ZX")
    assert row["last_level"] == 40
    assert row["last_tone_mark"] is None
    assert row["last_tone_space"] is None


async def test_digipeated_signal_not_attributed_to_source():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    # H-bit set on WOODY → the frame was relayed, not heard direct from N6ZX,
    # so N6ZX's row must not take the (last-hop) signal.
    await p.on_heard("N6ZX", "BEACON", ["WOODY*"], now, "agwpe",
                     signal=(28, 10, 6, 0))
    row = await _row(db_path, "N6ZX", "agwpe")
    assert row["last_level"] is None


async def test_last_hop_digi_gets_signal():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    # Relayed frame: last H-bit on WOODY → WOODY is the station we heard on RF,
    # so the level attaches to WOODY (digi rows use the empty transport key).
    await p.on_heard("N6ZX", "BEACON", ["WOODY*"], now, "agwpe",
                     signal=(47, 13, 10, 0))
    woody = await _row(db_path, "WOODY", "")
    assert woody["last_level"] == 47
    assert woody["last_tone_mark"] == 13
    assert woody["best_level"] == 47
    # Source got nothing.
    assert (await _row(db_path, "N6ZX", "agwpe"))["last_level"] is None


async def test_only_last_hop_digi_gets_signal():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    # KJOHN relayed then WOODY relayed; last '*' is WOODY → only WOODY takes the
    # signal.  KJOHN (an earlier confirmed hop) must not take this copy's level.
    await p.on_heard("K7BBS", "ID", ["KJOHN", "WOODY*", "KROCK"], now, "agwpe",
                     signal=(47, 13, 10, 0))
    assert (await _row(db_path, "WOODY", ""))["last_level"] == 47
    assert (await _row(db_path, "KJOHN", "")).get("last_level") is None


async def test_digi_signal_best_and_latest_across_copies():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    # Same digi (KJOHN) heard relaying two frames at different levels.
    await p.on_heard("K7BBS", "ID", ["KJOHN*"], now, "agwpe", signal=(67, 18, 12, 0))
    await p.on_heard("WA6RPD", "BEACON", ["KJOHN*"], now + 5, "agwpe", signal=(40, 12, 9, 1))
    kjohn = await _row(db_path, "KJOHN", "")
    assert kjohn["last_level"] == 40   # most recent copy
    assert kjohn["best_level"] == 67   # strongest over time


async def test_signal_log_names_the_transmitter(caplog):
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    with caplog.at_level(logging.INFO, logger="bbs.plugins.heard.heard"):
        await p.on_heard("N6ZX", "BEACON", [], now, "agwpe", signal=(47, 13, 10, 0))
        await p.on_heard("K7BBS", "ID", ["WOODY*"], now + 1, "agwpe", signal=(31, 9, 7, 1))
    msgs = "\n".join(
        r.getMessage() for r in caplog.records if "RF signal from" in r.getMessage()
    )
    assert "RF signal from N6ZX — level 47, tone 13/10, copy 0" in msgs
    assert "RF signal from WOODY — level 31, tone 9/7, copy 1" in msgs


async def test_signal_log_shows_na_tone_for_non_afsk(caplog):
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    with caplog.at_level(logging.INFO, logger="bbs.plugins.heard.heard"):
        await p.on_heard("N6ZX", "BEACON", [], now, "agwpe", signal=(40, 0xFF, 0xFF, 0))
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "RF signal from N6ZX — level 40, tone n/a, copy 0" in msgs


async def test_digi_alias_resolves_to_owner_log_shows_both(caplog):
    db_path = _tmp()
    p = await _plugin(db_path)
    # Record KJOHN as KF6ANX-8's Ka-Node tactical alias, then refresh the map.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO stations (callsign, kanode_alias, first_seen, last_seen)"
            " VALUES ('KF6ANX-8', 'KJOHN', 0, 0)"
        )
        await db.commit()
    await p._refresh_kanode_map()

    now = int(time.time())
    with caplog.at_level(logging.INFO, logger="bbs.plugins.heard.heard"):
        await p.on_heard("K7BBS", "BEACON", ["KJOHN*"], now, "agwpe",
                         signal=(67, 18, 12, 0))
    # Signal stored on the resolved owner's digi row (transport '').
    assert (await _row(db_path, "KF6ANX-8", ""))["last_level"] == 67
    assert (await _row(db_path, "KJOHN", "")).get("last_level") is None
    # Log shows the on-air alias AND where it landed, correlating with Direwolf.
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "RF signal from KJOHN (→ KF6ANX-8) — level 67, tone 18/12, copy 0" in msgs


async def test_own_beacon_digipeated_back_updates_digi():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    # Mirrors the log: our own beacon comes back relayed by KJOHN — we learn
    # KJOHN's level even though the "source" is us.
    await p.on_heard("W6ELA", "ED", ["KJOHN*", "KBETH", "WOODY"], now, "agwpe",
                     signal=(67, 17, 12, 0))
    assert (await _row(db_path, "KJOHN", ""))["last_level"] == 67


async def test_no_signal_leaves_columns_null():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    await p.on_heard("N6ZX", "BEACON", [], now, "agwpe")  # no signal arg
    row = await _row(db_path, "N6ZX")
    assert row["last_level"] is None
    assert row["best_level"] is None


async def test_v5_migration_adds_columns_to_pre_v5_db():
    db_path = _tmp()
    # Build a pre-v5 heard_events (no signal columns) stamped at version 4.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE heard_events ("
            " callsign TEXT NOT NULL COLLATE NOCASE,"
            " transport TEXT NOT NULL DEFAULT '',"
            " source TEXT NOT NULL DEFAULT 'heard',"
            " first_heard INTEGER NOT NULL, last_heard INTEGER NOT NULL,"
            " count INTEGER NOT NULL DEFAULT 0,"
            " last_direct_heard INTEGER NOT NULL DEFAULT 0,"
            " dest TEXT NOT NULL DEFAULT '' COLLATE NOCASE,"
            " via TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (callsign, transport))"
        )
        await db.execute(
            "CREATE TABLE heard_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO heard_settings VALUES ('heard_schema_version', '4')"
        )
        await db.commit()

    await _plugin(db_path)  # runs migrations → v5 adds the columns
    assert _SIG_COLS <= await _columns(db_path)

    # And the column is actually writable end-to-end after migration.
    p = HeardPlugin()
    await p.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    await p.on_heard("K6XX", "APRS", [], int(time.time()), "agwpe",
                     signal=(33, 12, 9, 0))
    row = await _row(db_path, "K6XX")
    assert row["last_level"] == 33


# ─── /api/heard signal exposure (Part D) ──────────────────────────────────────

async def test_api_heard_exposes_aggregated_signal(monkeypatch):
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    await p.on_heard("N6ZX", "BEACON", [], now, "agwpe", signal=(47, 13, 10, 0))
    await p.on_heard("N6ZX", "BEACON", [], now + 10, "agwpe", signal=(55, 14, 11, 0))
    rows = {r["callsign"]: r
            for r in _client(monkeypatch, db_path).get("/api/heard?limit=2000").get_json()}
    n = rows["N6ZX"]
    assert n["signal_level"] == 55   # freshest reading
    assert n["signal_best"] == 55    # peak ever
    assert n["signal_tone_mark"] == 14
    assert n["signal_tone_space"] == 11
    assert n["signal_copy"] == 0


async def test_api_heard_signal_null_when_absent(monkeypatch):
    db_path = _tmp()
    p = await _plugin(db_path)
    await p.on_heard("K6XX", "APRS", [], int(time.time()), "agwpe")  # no signal
    rows = {r["callsign"]: r
            for r in _client(monkeypatch, db_path).get("/api/heard").get_json()}
    assert rows["K6XX"]["signal_level"] is None
    assert rows["K6XX"]["signal_best"] is None


async def test_api_heard_aggregates_signal_across_direct_and_digi_rows(monkeypatch):
    # The exact live-data case: KF6ANX-8 heard directly (weak) AND as its KJOHN
    # digi (strong) — the display row must take the freshest level and the peak.
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    await p.on_heard("KF6ANX-8", "BEACON", [], now, "agwpe", signal=(30, 9, 7, 0))
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE stations SET kanode_alias = 'KJOHN' WHERE callsign = 'KF6ANX-8'"
        )
        await db.commit()
    await p._refresh_kanode_map()
    await p.on_heard("K7BBS", "ID", ["KJOHN*"], now + 5, "agwpe", signal=(67, 18, 12, 0))
    rows = {r["callsign"]: r
            for r in _client(monkeypatch, db_path).get("/api/heard?limit=2000").get_json()}
    kf = rows["KF6ANX-8"]
    assert kf["signal_level"] == 67   # freshest (KJOHN digi reception)
    assert kf["signal_best"] == 67    # MAX across the direct + digi rows


# ─── BBS text-UI signal cell (Part E) ─────────────────────────────────────────

from bbs.core.terminal import Terminal, ColorMode  # noqa: E402
from bbs.plugins.heard.heard import _fmt_signal_cell, _fmt_twist  # noqa: E402


def _term(mode):
    return Terminal(None, None, color_mode=mode)


def test_fmt_signal_cell_ascii_clean():
    # level 31 → 2 of 5 segments; clean copy → trailing space; no ANSI.
    assert _fmt_signal_cell(_term(ColorMode.OFF), 31, 0, 10) == "##---  31 "


def test_fmt_signal_cell_marginal_marker():
    assert _fmt_signal_cell(_term(ColorMode.OFF), 31, 2, 10) == "##---  31~"


def test_fmt_signal_cell_none_is_blank_padded():
    assert _fmt_signal_cell(_term(ColorMode.OFF), None, None, 10) == " " * 10


def test_fmt_signal_cell_clamps_high_level():
    s = _fmt_signal_cell(_term(ColorMode.OFF), 130, 0, 10)
    assert s == "##### 130 "   # bar clamped to 5 segments


def test_fmt_signal_cell_is_ascii_only():
    # BBS output is ascii-encoded; the bar must survive ascii encoding intact.
    for lvl, copy in ((67, 0), (22, 3), (5, 0)):
        s = _fmt_signal_cell(_term(ColorMode.OFF), lvl, copy, 10)
        assert s.encode("ascii")  # raises if any non-ASCII char slipped in


def test_fmt_signal_cell_ansi_preserves_visible_width():
    # ANSI colour wrapping must not change the visible width (column alignment).
    import re
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    clean = _fmt_signal_cell(_term(ColorMode.TRUECOLOR), 67, 0, 10)
    marg  = _fmt_signal_cell(_term(ColorMode.TRUECOLOR), 22, 3, 10)
    assert len(ansi.sub("", clean)) == 10
    assert len(ansi.sub("", marg)) == 10
    assert "\x1b[" in clean and "\x1b[" in marg   # actually styled in truecolor


def test_fmt_twist_low_tone_heavy():
    assert _fmt_twist(18, 12) == "+3.5 dB mark"   # 1200 Hz tone stronger


def test_fmt_twist_high_tone_heavy():
    assert _fmt_twist(8, 14) == "-4.9 dB space"   # 2200 Hz tone stronger


def test_fmt_twist_even_within_1db():
    assert _fmt_twist(12, 12) == "+0.0 dB even"


def test_fmt_twist_na_for_non_afsk():
    assert _fmt_twist(None, None) == "n/a"
    assert _fmt_twist(40, 0) == "n/a"


# ─── NET/ROM frames: signal without double-counting (count_it=False) ───────────

async def test_count_it_false_records_signal_without_counting():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    await p.on_heard("KI6ZHD-5", "BEACON", [], now, "agwpe", signal=(50, 12, 9, 0))
    assert (await _row(db_path, "KI6ZHD-5", "agwpe"))["count"] == 1
    # NET/ROM-style reception: refresh the signal, but do NOT bump the count and
    # do NOT store the (binary) payload as beacon text.
    await p.on_heard("KI6ZHD-5", "NODES", [], now + 10, "agwpe",
                     info="", signal=(57, 11, 9, 0), count_it=False)
    r = await _row(db_path, "KI6ZHD-5", "agwpe")
    assert r["count"] == 1          # unchanged — not double-counted
    assert r["last_level"] == 57    # signal updated
    assert r["best_level"] == 57


async def test_count_it_false_new_row_starts_at_zero():
    db_path = _tmp()
    p = await _plugin(db_path)
    await p.on_heard("N0DE-5", "NODES", [], int(time.time()), "agwpe",
                     info="", signal=(55, 12, 9, 0), count_it=False)
    r = await _row(db_path, "N0DE-5", "agwpe")
    assert r["count"] == 0          # a NODES-only node isn't an observation count
    assert r["last_level"] == 55    # …but its signal is recorded


async def test_netrom_digipeated_signal_goes_to_last_hop_digi():
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    # A NODES broadcast relayed by WOODY (H-bit set) — the level is WOODY's,
    # not the originating node's (same attribution as any digipeated frame).
    await p.on_heard("KI6ZHD-5", "NODES", ["WOODY*"], now, "agwpe",
                     info="", signal=(57, 11, 9, 0), count_it=False)
    assert (await _row(db_path, "WOODY", ""))["last_level"] == 57
    assert (await _row(db_path, "KI6ZHD-5", "agwpe"))["last_level"] is None


async def test_log_signal_false_records_without_logging(caplog):
    # Connected-mode capture: record the level, but stay quiet (no per-frame log).
    db_path = _tmp()
    p = await _plugin(db_path)
    now = int(time.time())
    with caplog.at_level(logging.INFO, logger="bbs.plugins.heard.heard"):
        await p.on_heard("KK6FPP", "W6ELA-1", [], now, "agwpe",
                         info="", signal=(59, 16, 11, 0),
                         count_it=False, log_signal=False)
    assert (await _row(db_path, "KK6FPP", "agwpe"))["last_level"] == 59
    assert not any("RF signal from KK6FPP" in r.getMessage() for r in caplog.records)
