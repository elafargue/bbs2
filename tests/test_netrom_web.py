"""
tests/test_netrom_web.py — /api/netrom/activity REST endpoint (N4c dashboard).
"""
from __future__ import annotations

import server.web_interface  # noqa: F401  — registers the route as a side effect


def _client(monkeypatch, tmp_path):
    import server.app as sapp
    from bbs.config import load_config
    from bbs.core.engine import BBSEngine
    cfgfile = tmp_path / "bbs.yaml"
    cfgfile.write_text("bbs:\n  callsign: W6ELA\n  ssid: 1\nweb:\n  secret_key: x\n")
    cfg = load_config(cfgfile)
    engine = BBSEngine(cfg, cfg_path=str(cfgfile))
    monkeypatch.setattr(sapp, "bbs_engine", engine)
    sapp.app.secret_key = "test"
    return sapp.app.test_client(), engine


def test_netrom_activity_requires_sysop(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/api/netrom/activity").status_code == 401


def test_netrom_activity_returns_snapshot(monkeypatch, tmp_path):
    client, engine = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(engine, "netrom_snapshot",
                        lambda: {"enabled": True, "sessions": [],
                                 "gateway": {"active": 0, "max": 4}})
    with client.session_transaction() as s:
        s["sysop"] = True
    r = client.get("/api/netrom/activity")
    assert r.status_code == 200
    body = r.get_json()
    assert body["enabled"] is True and body["gateway"]["max"] == 4


def test_netrom_activity_disabled_when_no_node(monkeypatch, tmp_path):
    client, engine = _client(monkeypatch, tmp_path)
    with client.session_transaction() as s:
        s["sysop"] = True
    # netrom not configured on this engine → snapshot reports disabled.
    assert client.get("/api/netrom/activity").get_json() == {"enabled": False}
