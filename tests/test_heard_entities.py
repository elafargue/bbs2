"""
tests/test_heard_entities.py — /api/heard/entities rollup + entity edit.

Verifies the physical-station grouping (SSIDs folded by base callsign) and the
sysop entity-edit PUT, via the real Flask route against a populated heard DB.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

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


async def _setup(db_path: str) -> None:
    p = HeardPlugin()
    await p.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    now = int(time.time())
    await p.on_heard("W6ELA-1", "BEACON", [], now, "agwpe", info="BBS")
    await p.on_heard("W6ELA-5", "BEACON", [], now + 1, "agwpe", info="NODE PALO")
    await p.on_heard("K6XX", "APRS", [], now + 2, "agwpe")


def _client(monkeypatch, db_path: str, sysop: bool = True):
    import server.app as sapp
    monkeypatch.setattr(sapp, "bbs_engine", BBSEngine(_cfg(db_path)))
    sapp.app.secret_key = "test"
    c = sapp.app.test_client()
    if sysop:
        with c.session_transaction() as s:
            s["sysop"] = True
    return c


def _tmp() -> str:
    return str(Path(tempfile.mkdtemp(prefix="bbs2_ent_")) / "t.db")


async def test_entities_group_ssids_by_base(monkeypatch):
    db_path = _tmp()
    await _setup(db_path)
    ents = {e["base_call"]: e for e in _client(monkeypatch, db_path).get("/api/heard/entities").get_json()}
    assert set(ents) == {"W6ELA", "K6XX"}
    assert set(ents["W6ELA"]["ssids"]) == {"W6ELA-1", "W6ELA-5"}
    assert ents["K6XX"]["ssids"] == ["K6XX"]
    # latest beacon text rolls up to the most recent member
    assert ents["W6ELA"]["last_beacon_text"] == "NODE PALO"
    # each member carries typed-alias keys so the UI can label NET/ROM vs Ka-Node
    m = ents["W6ELA"]["members"][0]
    assert {"netrom_alias", "kanode_alias", "beacon_alias"} <= set(m)


async def test_entity_edit_notes_and_canonical(monkeypatch):
    db_path = _tmp()
    await _setup(db_path)
    client = _client(monkeypatch, db_path)
    r = client.put("/api/heard/entities/w6ela",
                   json={"notes": "club station", "canonical_nodename": "PALO"})
    assert r.status_code == 200
    ents = {e["base_call"]: e for e in client.get("/api/heard/entities").get_json()}
    assert ents["W6ELA"]["notes"] == "club station"
    assert ents["W6ELA"]["nodename"] == "PALO"       # canonical wins over rolled-up alias


async def test_entities_requires_sysop(monkeypatch):
    db_path = _tmp()
    await _setup(db_path)
    assert _client(monkeypatch, db_path, sysop=False).get("/api/heard/entities").status_code == 401


async def test_entity_position_is_freshest_beacon(monkeypatch):
    db_path = _tmp()
    p = HeardPlugin()
    await p.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    now = int(time.time())
    # Two SSIDs of one station beacon different fixes at different times.
    await p.on_heard("W6ELA-1", "BEACON", [], now,       "agwpe", info="<MAP:37.0,-122.0,W6ELA>")
    await p.on_heard("W6ELA-5", "BEACON", [], now + 100, "agwpe", info="<MAP:38.0,-121.0,W6ELA>")
    # W6ELA-1 is then heard again (no new fix): its last_seen is now newest, but
    # its position_ts is unchanged — the fresher W6ELA-5 beacon must still win.
    await p.on_heard("W6ELA-1", "DATA", [], now + 200, "agwpe")

    ents = {e["base_call"]: e for e in _client(monkeypatch, db_path).get("/api/heard/entities").get_json()}
    assert (ents["W6ELA"]["lat"], ents["W6ELA"]["lon"]) == (38.0, -121.0)
    assert ents["W6ELA"]["position_source"] == "beacon"


async def test_entity_position_override_and_clear(monkeypatch):
    db_path = _tmp()
    p = HeardPlugin()
    await p.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    now = int(time.time())
    await p.on_heard("W6ELA-1", "BEACON", [], now, "agwpe", info="<MAP:37.0,-122.0,W6ELA>")
    client = _client(monkeypatch, db_path)

    # Sysop pins a manual override — it wins over the beacon.
    assert client.put("/api/heard/entities/w6ela", json={"lat": 40.0, "lon": -120.0}).status_code == 200
    ent = {e["base_call"]: e for e in client.get("/api/heard/entities").get_json()}["W6ELA"]
    assert (ent["lat"], ent["lon"]) == (40.0, -120.0)
    assert ent["position_source"] == "manual"
    assert (ent["override_lat"], ent["override_lon"]) == (40.0, -120.0)

    # Clearing the override reverts to the freshest beacon.
    assert client.put("/api/heard/entities/w6ela", json={"lat": None, "lon": None}).status_code == 200
    ent = {e["base_call"]: e for e in client.get("/api/heard/entities").get_json()}["W6ELA"]
    assert (ent["lat"], ent["lon"]) == (37.0, -122.0)
    assert ent["position_source"] == "beacon"
    assert ent["override_lat"] is None


async def test_entity_override_requires_lat_lon_together(monkeypatch):
    db_path = _tmp()
    await _setup(db_path)
    r = _client(monkeypatch, db_path).put("/api/heard/entities/w6ela", json={"lat": 40.0})
    assert r.status_code == 400


async def test_typed_aliases_and_netrom_via_and_callsign_title(monkeypatch):
    # A dual-role station: KF6DQU-9 is heard on AGWPE *and* is a NET/ROM node
    # (alias BANNER) + a Ka-Node digi (KBANN); KF6DQU-10 carries a beacon alias.
    db_path = _tmp()
    p = HeardPlugin()
    await p.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    now = int(time.time())
    import sqlite3
    c = sqlite3.connect(db_path)
    c.executescript(f"""
        INSERT INTO stations (callsign, base_call, netrom_alias, kanode_alias, first_seen, last_seen)
            VALUES ('KF6DQU-9','KF6DQU','BANNER','KBANN',{now},{now});
        INSERT INTO stations (callsign, base_call, beacon_alias, first_seen, last_seen)
            VALUES ('KF6DQU-10','KF6DQU','DQUBCN',{now},{now});
        INSERT INTO heard_events (callsign, transport, source, first_heard, last_heard, count)
            VALUES ('KF6DQU-9','agwpe','heard',{now},{now},10),
                   ('KF6DQU-9','netrom','netrom',{now},{now},2),
                   ('KF6DQU-10','agwpe','heard',{now},{now},5);
        INSERT OR IGNORE INTO station_entities (base_call, first_seen, last_seen)
            VALUES ('KF6DQU',{now},{now});
    """)
    c.commit(); c.close()

    ent = {e["base_call"]: e for e in _client(monkeypatch, db_path).get("/api/heard/entities").get_json()}["KF6DQU"]
    # A: aliases are typed
    assert ent["netrom_aliases"] == ["BANNER"]
    assert ent["kanode_aliases"] == ["KBANN"]
    assert ent["beacon_aliases"] == ["DQUBCN"]
    assert set(ent["aliases"]) == {"BANNER", "KBANN", "DQUBCN"}   # flat union kept for search
    # B: NET/ROM surfaces in Via even though the primary transport is AGWPE
    assert "netrom" in ent["transports"] and "agwpe" in ent["transports"]
    # C: the physical station's callsign is the title, not an alias
    assert ent["nodename"] == "KF6DQU"
