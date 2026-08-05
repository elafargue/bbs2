"""
tests/test_services_web.py — validation for the /api/services config route.
"""
from __future__ import annotations

from server.routes.services import _validate


def test_validate_accepts_and_normalizes():
    out, err = _validate({
        "enabled": True,
        "max_sessions": 5,
        "lockout": ["nocall", " n0call "],
        "routes": {
            "w6ela-2": {"exec": "/usr/bin/xfbbd", "args": ["xfbbd", "%U"], "min_auth": "identified"},
        },
    })
    assert err is None
    assert out["enabled"] is True
    assert out["max_sessions"] == 5
    assert out["lockout"] == ["NOCALL", "N0CALL"]
    assert "W6ELA-2" in out["routes"]                      # SSID upper-cased
    assert out["routes"]["W6ELA-2"]["exec"] == "/usr/bin/xfbbd"
    assert out["routes"]["W6ELA-2"]["args"] == ["xfbbd", "%U"]


def test_validate_defaults_for_empty():
    out, err = _validate({})
    assert err is None
    assert out == {"enabled": False, "max_sessions": 10, "lockout": [], "routes": {}}


def test_validate_rejects_relative_exec():
    out, err = _validate({"routes": {"W6ELA-2": {"exec": "xfbbd"}}})
    assert out is None and "absolute" in err


def test_validate_rejects_missing_exec():
    out, err = _validate({"routes": {"W6ELA-2": {"args": ["x"]}}})
    assert out is None and "exec is required" in err


def test_validate_rejects_bad_min_auth():
    out, err = _validate({"routes": {"W6ELA-2": {"exec": "/bin/x", "min_auth": "bogus"}}})
    assert out is None and "min_auth" in err


def test_validate_rejects_bad_max_sessions():
    out, err = _validate({"max_sessions": 0})
    assert out is None and "max_sessions" in err


# ── Route-level round-trip (Flask test client) ────────────────────────────────

def _client(monkeypatch, cfgfile):
    import server.app as sapp
    from bbs.config import load_config
    from bbs.core.engine import BBSEngine
    cfg = load_config(cfgfile)
    engine = BBSEngine(cfg, cfg_path=str(cfgfile))
    monkeypatch.setattr(sapp, "bbs_engine", engine)
    sapp.app.secret_key = "test"
    return sapp.app.test_client(), engine


def test_services_route_requires_sysop(monkeypatch, tmp_path):
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text("bbs:\n  callsign: W6ELA\n  ssid: 1\nweb:\n  secret_key: x\n")
    client, _ = _client(monkeypatch, cfgfile)
    assert client.get("/api/services").status_code == 401   # no sysop session


def test_services_route_roundtrip(monkeypatch, tmp_path):
    import yaml as _yaml
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text("bbs:\n  callsign: W6ELA\n  ssid: 1\nweb:\n  secret_key: x\n")
    client, engine = _client(monkeypatch, cfgfile)
    with client.session_transaction() as s:
        s["sysop"] = True

    payload = {
        "enabled": True, "max_sessions": 3, "lockout": ["NOCALL"],
        "routes": {"W6ELA-2": {"exec": "/bin/cat", "args": ["cat"]}},
    }
    r = client.put("/api/services", json=payload)
    assert r.status_code == 200
    assert r.get_json()["restart_required"] is True         # new SSID added

    # Persisted to bbs.yaml…
    written = _yaml.safe_load(cfgfile.read_text())
    assert written["services"]["routes"]["W6ELA-2"]["exec"] == "/bin/cat"
    # …and hot-reloaded into the live dispatcher.
    assert engine._services.enabled
    assert "W6ELA-2" in engine._services.route_callsigns()

    # GET returns the saved config.
    g = client.get("/api/services")
    assert g.status_code == 200 and g.get_json()["enabled"] is True


def test_services_route_rejects_invalid(monkeypatch, tmp_path):
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text("bbs:\n  callsign: W6ELA\n  ssid: 1\nweb:\n  secret_key: x\n")
    client, _ = _client(monkeypatch, cfgfile)
    with client.session_transaction() as s:
        s["sysop"] = True
    r = client.put("/api/services", json={"routes": {"W6ELA-2": {"exec": "relative"}}})
    assert r.status_code == 400 and "absolute" in r.get_json()["error"]


# ── Reserved SSIDs (read-only map + PUT collision guard) ──────────────────────

def test_reserved_ssids_bbs_only(monkeypatch, tmp_path):
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text(
        "bbs:\n  callsign: W6ELA\n  ssid: 1\n  name: Test BBS\nweb:\n  secret_key: x\n"
    )
    client, _ = _client(monkeypatch, cfgfile)
    with client.session_transaction() as s:
        s["sysop"] = True
    r = client.get("/api/services/reserved")
    assert r.status_code == 200
    reserved = r.get_json()["reserved"]
    assert [x["ssid"] for x in reserved] == ["W6ELA-1"]         # node shares BBS SSID
    assert reserved[0]["role"] == "BBS"


def test_reserved_ssids_with_node(monkeypatch, tmp_path):
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text(
        "bbs:\n  callsign: W6ELA\n  ssid: 1\nweb:\n  secret_key: x\n"
        "netrom:\n  alias: PALO\n  node_ssid: 5\n"
    )
    client, _ = _client(monkeypatch, cfgfile)
    with client.session_transaction() as s:
        s["sysop"] = True
    reserved = client.get("/api/services/reserved").get_json()["reserved"]
    by_ssid = {x["ssid"]: x for x in reserved}
    assert set(by_ssid) == {"W6ELA-1", "W6ELA-5"}
    assert by_ssid["W6ELA-5"]["role"] == "Node"
    assert "PALO" in by_ssid["W6ELA-5"]["detail"]


def test_reserved_ssids_requires_sysop(monkeypatch, tmp_path):
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text("bbs:\n  callsign: W6ELA\n  ssid: 1\nweb:\n  secret_key: x\n")
    client, _ = _client(monkeypatch, cfgfile)
    assert client.get("/api/services/reserved").status_code == 401


def test_put_rejects_route_on_reserved_ssid(monkeypatch, tmp_path):
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text(
        "bbs:\n  callsign: W6ELA\n  ssid: 1\nweb:\n  secret_key: x\n"
        "netrom:\n  alias: PALO\n  node_ssid: 5\n"
    )
    client, _ = _client(monkeypatch, cfgfile)
    with client.session_transaction() as s:
        s["sysop"] = True
    # A route on the node SSID must be refused (would shadow the node).
    r = client.put("/api/services", json={
        "routes": {"W6ELA-5": {"exec": "/bin/cat", "args": ["cat"]}},
    })
    assert r.status_code == 400
    assert "reserved" in r.get_json()["error"].lower()
