"""
tests/test_config_node_ssid.py — N3 config: the optional netrom.node_ssid.

Covers the netrom_node_ssid / netrom_node_call properties (accept, out-of-range,
clash-with-bbs-ssid, unset) and the load_config warning on a rejected value.
"""
from __future__ import annotations

import logging

import pytest

from bbs.config import BBSConfig, load_config


def _cfg(*, ssid: int = 1, netrom: dict | None = None) -> BBSConfig:
    return BBSConfig(
        callsign="W6ELA", ssid=ssid, name="Test", sysop="W6ELA", location="",
        max_users=10, idle_timeout=60, write_timeout=30,
        path_length_medium_hops=1, path_length_long_hops=3,
        transports={}, database={}, auth={}, plugins={},
        web={}, logging={}, netrom=netrom or {}, services={},
    )


class TestNodeSsidProperty:
    def test_unset_is_none(self):
        c = _cfg(netrom={"alias": "PALO"})
        assert c.netrom_node_ssid is None
        assert c.netrom_node_call is None

    def test_valid_distinct_ssid(self):
        c = _cfg(ssid=1, netrom={"node_ssid": 5})
        assert c.netrom_node_ssid == 5
        assert c.netrom_node_call == "W6ELA-5"

    def test_clash_with_bbs_ssid_rejected(self):
        c = _cfg(ssid=5, netrom={"node_ssid": 5})
        assert c.netrom_node_ssid is None      # a role can't be both node and BBS
        assert c.netrom_node_call is None

    def test_out_of_range_rejected(self):
        assert _cfg(netrom={"node_ssid": 16}).netrom_node_ssid is None
        assert _cfg(netrom={"node_ssid": -1}).netrom_node_ssid is None

    def test_non_integer_rejected(self):
        assert _cfg(netrom={"node_ssid": "five"}).netrom_node_ssid is None

    def test_ssid_zero_yields_bare_callsign(self):
        c = _cfg(ssid=1, netrom={"node_ssid": 0})
        assert c.netrom_node_ssid == 0
        assert c.netrom_node_call == "W6ELA"


def _write_cfg(tmp_path, node_ssid) -> str:
    p = tmp_path / "bbs.yaml"
    p.write_text(
        "bbs:\n"
        "  callsign: W6ELA\n"
        "  ssid: 1\n"
        "netrom:\n"
        f"  node_ssid: {node_ssid}\n"
    )
    return str(p)


class TestLoadConfigValidation:
    def test_valid_node_ssid_loads(self, tmp_path):
        cfg = load_config(_write_cfg(tmp_path, 5))
        assert cfg.netrom_node_call == "W6ELA-5"

    def test_clash_warns_and_falls_back(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = load_config(_write_cfg(tmp_path, 1))   # == bbs.ssid
        assert cfg.netrom_node_call is None
        assert any("node_ssid" in r.message for r in caplog.records)

    def test_unset_no_warning(self, tmp_path, caplog):
        p = tmp_path / "bbs.yaml"
        p.write_text("bbs:\n  callsign: W6ELA\n  ssid: 1\n")
        with caplog.at_level(logging.WARNING):
            cfg = load_config(str(p))
        assert cfg.netrom_node_call is None
        assert not any("node_ssid" in r.message for r in caplog.records)
