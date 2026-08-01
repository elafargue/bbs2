"""
tests/test_heard_migrations.py — pin the accreted heard-schema migrations.

Builds old-shape databases and asserts the versioned migrator
(HeardPlugin._migrate_heard_schema) produces the expected result, so the
fragile migration history can't silently break under later milestones.
Also validates against the real production DB when a copy is present
(data/bbs.db), skipped otherwise.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from bbs.plugins.heard.heard import HeardPlugin, _HEARD_SCHEMA_VERSION


def _tmpdb() -> str:
    return str(Path(tempfile.mkdtemp(prefix="bbs2_heardmig_")) / "test.db")


def _exec(path: str, script: str) -> None:
    c = sqlite3.connect(path)
    try:
        c.executescript(script)
        c.commit()
    finally:
        c.close()


def _q(path: str, sql: str):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


async def _migrate(path: str) -> None:
    await HeardPlugin().initialize({"enabled": True}, path)


# Old pre-split single table (with the catch-all `nodename` column).
_OLD_HEARD_STATIONS = """
CREATE TABLE heard_stations (
    callsign          TEXT NOT NULL,
    transport         TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT 'heard',
    lat               REAL,
    lon               REAL,
    comment           TEXT NOT NULL DEFAULT '',
    position_source   TEXT NOT NULL DEFAULT '',
    nodename          TEXT NOT NULL DEFAULT '',
    first_heard       INTEGER NOT NULL DEFAULT 0,
    last_heard        INTEGER NOT NULL DEFAULT 0,
    count             INTEGER NOT NULL DEFAULT 0,
    last_direct_heard INTEGER NOT NULL DEFAULT 0,
    dest              TEXT NOT NULL DEFAULT '',
    via               TEXT NOT NULL DEFAULT ''
);
"""


async def test_split_heard_stations_to_stations_and_events():
    path = _tmpdb()
    _exec(path, _OLD_HEARD_STATIONS + """
        INSERT INTO heard_stations
            (callsign, transport, source, lat, lon, comment, nodename,
             first_heard, last_heard, count)
        VALUES ('K6XX', 'agwpe', 'heard', 37.5, -122.1, 'note', 'NODEX',
                100, 200, 5);
    """)
    await _migrate(path)
    # heard_stations gone, split into stations + heard_events.
    assert _q(path, "SELECT COUNT(*) FROM sqlite_master WHERE name='heard_stations'")[0][0] == 0
    st = _q(path, "SELECT callsign, lat, lon, comment, beacon_alias, netrom_alias "
                  "FROM stations WHERE callsign='K6XX'")
    assert st == [("K6XX", 37.5, -122.1, "note", "NODEX", "")]   # nodename → beacon_alias
    ev = _q(path, "SELECT transport, source, count FROM heard_events WHERE callsign='K6XX'")
    assert ev == [("agwpe", "heard", 5)]
    assert _q(path, "SELECT value FROM heard_settings WHERE key='heard_schema_version'")[0][0] == str(_HEARD_SCHEMA_VERSION)


async def test_orphaned_heard_stations_dropped_live_data_kept():
    # The prod case: both the stale heard_stations AND the live stations exist.
    path = _tmpdb()
    _exec(path, _OLD_HEARD_STATIONS + """
        CREATE TABLE stations (
            callsign TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
            lat REAL, lon REAL, comment TEXT NOT NULL DEFAULT '',
            position_source TEXT NOT NULL DEFAULT '',
            netrom_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            beacon_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            kanode_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            first_seen INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO heard_stations (callsign) VALUES ('STALE');
        INSERT INTO stations (callsign, beacon_alias) VALUES ('LIVE', 'REAL');
    """)
    await _migrate(path)
    assert _q(path, "SELECT COUNT(*) FROM sqlite_master WHERE name='heard_stations'")[0][0] == 0
    # Live stations table untouched; the stale STALE row is NOT resurrected.
    assert _q(path, "SELECT callsign, beacon_alias FROM stations ORDER BY callsign") == [("LIVE", "REAL")]


async def test_netrom_routes_single_pk_to_composite():
    path = _tmpdb()
    _exec(path, """
        CREATE TABLE netrom_routes (
            dest_call TEXT PRIMARY KEY NOT NULL,
            neighbor_call TEXT NOT NULL DEFAULT '',
            alias TEXT NOT NULL DEFAULT '', quality INTEGER NOT NULL DEFAULT 0,
            via_call TEXT NOT NULL DEFAULT '', via_alias TEXT NOT NULL DEFAULT '',
            last_seen INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO netrom_routes (dest_call, neighbor_call, quality, via_call, last_seen)
        VALUES ('N6ZX-5', 'W6ELA-1', 200, 'W6ELA-1', 100);
    """)
    await _migrate(path)
    pk = [r for r in _q(path, "PRAGMA table_info(netrom_routes)") if r[5] > 0]
    assert {r[1] for r in pk} == {"dest_call", "neighbor_call"}          # composite PK
    assert _q(path, "SELECT dest_call, neighbor_call, quality FROM netrom_routes") == [("N6ZX-5", "W6ELA-1", 200)]


async def test_alias_inversion_fix_moves_bogus_netrom_alias():
    # netrom_alias set but no heard_events 'netrom' row + sentinel unset →
    # the value was a migrated nodename and belongs in beacon_alias.
    path = _tmpdb()
    _exec(path, """
        CREATE TABLE stations (
            callsign TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
            lat REAL, lon REAL, comment TEXT NOT NULL DEFAULT '',
            position_source TEXT NOT NULL DEFAULT '',
            netrom_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            beacon_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            kanode_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            first_seen INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE heard_events (
            callsign TEXT NOT NULL COLLATE NOCASE, transport TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'heard', first_heard INTEGER NOT NULL DEFAULT 0,
            last_heard INTEGER NOT NULL DEFAULT 0, count INTEGER NOT NULL DEFAULT 0,
            last_direct_heard INTEGER NOT NULL DEFAULT 0, dest TEXT NOT NULL DEFAULT '',
            via TEXT NOT NULL DEFAULT '', PRIMARY KEY (callsign, transport)
        );
        CREATE TABLE heard_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO stations (callsign, netrom_alias) VALUES ('K6BOGUS', 'MISLABEL');
    """)
    await _migrate(path)
    st = _q(path, "SELECT netrom_alias, beacon_alias FROM stations WHERE callsign='K6BOGUS'")
    assert st == [("", "MISLABEL")]      # moved netrom_alias → beacon_alias
    assert _q(path, "SELECT value FROM heard_settings WHERE key='migration_alias_fix_done'")[0][0] == "1"


async def test_migration_is_idempotent_and_version_adopted():
    path = _tmpdb()
    _exec(path, _OLD_HEARD_STATIONS + "INSERT INTO heard_stations (callsign) VALUES ('K6IDEM');")
    await _migrate(path)
    first = _q(path, "SELECT callsign, beacon_alias FROM stations ORDER BY callsign")
    await _migrate(path)   # second run: version already current → no-op
    assert _q(path, "SELECT callsign, beacon_alias FROM stations ORDER BY callsign") == first
    assert _q(path, "SELECT value FROM heard_settings WHERE key='heard_schema_version'")[0][0] == str(_HEARD_SCHEMA_VERSION)


async def test_v2_adds_columns_to_existing_stations():
    # A v1-shape stations table (no service/last_beacon columns) is upgraded.
    path = _tmpdb()
    _exec(path, """
        CREATE TABLE stations (
            callsign TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
            lat REAL, lon REAL, comment TEXT NOT NULL DEFAULT '',
            position_source TEXT NOT NULL DEFAULT '',
            netrom_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            beacon_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            kanode_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            first_seen INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE heard_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO heard_settings (key, value) VALUES ('heard_schema_version', '1');
        INSERT INTO heard_settings (key, value) VALUES ('migration_alias_fix_done', '1');
        INSERT INTO stations (callsign, beacon_alias) VALUES ('K6XX', 'NODEX');
    """)
    await _migrate(path)
    cols = {r[1] for r in _q(path, "PRAGMA table_info(stations)")}
    assert {"service", "last_beacon_text", "last_beacon_ts"} <= cols
    # existing data preserved, new columns default-empty
    assert _q(path, "SELECT beacon_alias, service, last_beacon_text, last_beacon_ts "
                    "FROM stations WHERE callsign='K6XX'") == [("NODEX", "", "", 0)]
    assert _q(path, "SELECT value FROM heard_settings WHERE key='heard_schema_version'")[0][0] == str(_HEARD_SCHEMA_VERSION)


async def test_v3_creates_entities_and_backfills_base_call():
    path = _tmpdb()
    _exec(path, """
        CREATE TABLE stations (
            callsign TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
            lat REAL, lon REAL, comment TEXT NOT NULL DEFAULT '',
            position_source TEXT NOT NULL DEFAULT '',
            netrom_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            beacon_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            kanode_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            service TEXT NOT NULL DEFAULT '', last_beacon_text TEXT NOT NULL DEFAULT '',
            last_beacon_ts INTEGER NOT NULL DEFAULT 0,
            first_seen INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE heard_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO heard_settings VALUES ('heard_schema_version','2'),('migration_alias_fix_done','1');
        INSERT INTO stations (callsign, first_seen, last_seen) VALUES
            ('W6ELA-1', 100, 500), ('W6ELA-5', 150, 600), ('K6XX', 200, 300);
    """)
    await _migrate(path)
    # base_call stripped of SSID for each station.
    assert _q(path, "SELECT callsign, base_call FROM stations ORDER BY callsign") == [
        ("K6XX", "K6XX"), ("W6ELA-1", "W6ELA"), ("W6ELA-5", "W6ELA")]
    # one entity per base callsign (W6ELA groups two SSIDs), first/last aggregated.
    assert _q(path, "SELECT base_call, first_seen, last_seen FROM station_entities ORDER BY base_call") == [
        ("K6XX", 200, 300), ("W6ELA", 100, 600)]
    assert _q(path, "SELECT value FROM heard_settings WHERE key='heard_schema_version'")[0][0] == str(_HEARD_SCHEMA_VERSION)


async def test_v4_adds_position_ts_and_backfills():
    # A v3-shape DB (has base_call + station_entities, no position_ts) upgrades:
    # position_ts is added and seeded to last_seen for rows that already have a fix.
    path = _tmpdb()
    _exec(path, """
        CREATE TABLE stations (
            callsign TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
            base_call TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            lat REAL, lon REAL, comment TEXT NOT NULL DEFAULT '',
            position_source TEXT NOT NULL DEFAULT '',
            netrom_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            beacon_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            kanode_alias TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
            service TEXT NOT NULL DEFAULT '', last_beacon_text TEXT NOT NULL DEFAULT '',
            last_beacon_ts INTEGER NOT NULL DEFAULT 0,
            first_seen INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE station_entities (
            base_call TEXT PRIMARY KEY NOT NULL COLLATE NOCASE,
            canonical_nodename TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            lat REAL, lon REAL, position_source TEXT NOT NULL DEFAULT '',
            first_seen INTEGER NOT NULL DEFAULT 0, last_seen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE heard_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO heard_settings VALUES ('heard_schema_version','3'),('migration_alias_fix_done','1');
        INSERT INTO stations (callsign, base_call, lat, lon, position_source, last_seen) VALUES
            ('K6FIX', 'K6FIX', 37.5, -122.1, 'beacon', 900),   -- has a fix
            ('K6NOP', 'K6NOP', NULL, NULL, '', 800);           -- no fix
    """)
    await _migrate(path)
    cols = {r[1] for r in _q(path, "PRAGMA table_info(stations)")}
    assert "position_ts" in cols
    # row with coordinates seeded to last_seen; row without stays 0.
    assert _q(path, "SELECT callsign, position_ts FROM stations ORDER BY callsign") == [
        ("K6FIX", 900), ("K6NOP", 0)]
    assert _q(path, "SELECT value FROM heard_settings WHERE key='heard_schema_version'")[0][0] == str(_HEARD_SCHEMA_VERSION)


_PROD_DB = Path("data/bbs.db")


@pytest.mark.skipif(not _PROD_DB.exists(), reason="no production DB copy present")
async def test_prod_db_migration_preserves_live_data():
    """When a real prod DB copy is present, migrating it must drop only the
    orphaned heard_stations, keep every live row, stamp v1, and be idempotent."""
    path = _tmpdb()
    shutil.copy(_PROD_DB, path)

    # Compare the ORIGINAL columns explicitly (not SELECT *), since later
    # migrations legitimately add new columns to stations.
    _ORIG = ("callsign, lat, lon, comment, position_source, netrom_alias,"
             " beacon_alias, kanode_alias, first_seen, last_seen")

    def snapshot():
        c = sqlite3.connect(path)
        try:
            live = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("stations", "heard_events", "heard_paths", "netrom_routes")}
            live["stations_hash"] = hash(tuple(c.execute(
                f"SELECT {_ORIG} FROM stations ORDER BY callsign").fetchall()))
            return live
        finally:
            c.close()

    before = snapshot()
    await _migrate(path)
    await _migrate(path)   # idempotency
    after = snapshot()

    assert _q(path, "SELECT COUNT(*) FROM sqlite_master WHERE name='heard_stations'")[0][0] == 0
    assert _q(path, "SELECT value FROM heard_settings WHERE key='heard_schema_version'")[0][0] == str(_HEARD_SCHEMA_VERSION)
    assert after == before      # every live table count + stations content identical
