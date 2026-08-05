"""
tests/test_netrom_gateway.py — N4a GatewayPolicy + GatewayGuard.

ACL (deny/allow/min_auth), INTERLOCK, rate limiting, and per-user + node-wide
circuit caps, in isolation (no node/transport).
"""
from __future__ import annotations

import time

import pytest

from bbs.core.auth import AuthLevel
from bbs.netrom.gateway import GatewayGuard, GatewayPolicy


def _guard(**kw) -> GatewayGuard:
    return GatewayGuard(GatewayPolicy(**kw))


def _ok(guard, *, user="KF6ANX-4", auth=AuthLevel.IDENTIFIED,
        dest="K2YE-5", neighbor="K6FB-5", via="") -> str | None:
    return guard.check(user_call=user, auth_level=auth, dest_call=dest,
                       neighbor=neighbor, arrival_via=via)


# ─── Policy parsing ───────────────────────────────────────────────────────────

class TestPolicyParsing:
    def test_defaults(self):
        p = GatewayPolicy.from_netrom_cfg({})
        assert p.min_auth is AuthLevel.IDENTIFIED
        assert p.interlock is True
        assert p.rate_limit_per_min == 6
        assert p.max_circuits == 4 and p.max_circuits_per_user == 2
        assert p.allow == frozenset() and p.deny == frozenset()

    def test_legacy_max_gateway_circuits(self):
        p = GatewayPolicy.from_netrom_cfg({"max_gateway_circuits": 9})
        assert p.max_circuits == 9                       # legacy fallback

    def test_full_block(self):
        p = GatewayPolicy.from_netrom_cfg({"gateway": {
            "min_auth": "authenticated", "allow": ["kf6anx", "K6FB"],
            "deny": ["nocall"], "interlock": False,
            "rate_limit_per_min": 3, "max_circuits_per_user": 1, "max_circuits": 2,
        }})
        assert p.min_auth is AuthLevel.AUTHENTICATED
        assert p.allow == {"KF6ANX", "K6FB"}
        assert p.deny == {"NOCALL"}
        assert p.interlock is False
        assert p.rate_limit_per_min == 3
        assert p.max_circuits_per_user == 1 and p.max_circuits == 2

    def test_unknown_min_auth_falls_back(self):
        p = GatewayPolicy.from_netrom_cfg({"gateway": {"min_auth": "bogus"}})
        assert p.min_auth is AuthLevel.IDENTIFIED


# ─── ACL ──────────────────────────────────────────────────────────────────────

class TestACL:
    def test_open_allows(self):
        assert _ok(_guard()) is None

    def test_deny_base_and_full(self):
        g = _guard(deny=frozenset({"NOCALL"}))
        assert _ok(g, user="NOCALL-7") is not None       # base match
        g2 = _guard(deny=frozenset({"W6ELA-9"}))
        assert _ok(g2, user="W6ELA-9") is not None        # full match
        assert _ok(g2, user="W6ELA-1") is None            # different SSID allowed

    def test_allow_list_is_mandatory_when_set(self):
        g = _guard(allow=frozenset({"KF6ANX"}))
        assert _ok(g, user="KF6ANX-4") is None            # on the list (base)
        assert _ok(g, user="K6FB-5") is not None          # not on the list → refused

    def test_deny_wins_over_allow(self):
        g = _guard(allow=frozenset({"KF6ANX"}), deny=frozenset({"KF6ANX"}))
        assert _ok(g, user="KF6ANX-4") is not None

    def test_min_auth(self):
        g = _guard(min_auth=AuthLevel.AUTHENTICATED)
        assert _ok(g, auth=AuthLevel.IDENTIFIED) is not None
        assert _ok(g, auth=AuthLevel.AUTHENTICATED) is None
        assert _ok(g, auth=AuthLevel.SYSOP) is None

    def test_none_auth_treated_as_identified(self):
        assert _ok(_guard(), auth=None) is None
        assert _ok(_guard(min_auth=AuthLevel.AUTHENTICATED), auth=None) is not None


# ─── INTERLOCK ────────────────────────────────────────────────────────────────

class TestInterlock:
    def test_refuses_back_out_arrival_link(self):
        g = _guard()
        assert _ok(g, neighbor="K6FB-5", via="K6FB-5") is not None    # loop
        assert "nterlock" in _ok(g, neighbor="K6FB-5", via="K6FB-5")

    def test_allows_different_neighbor(self):
        assert _ok(_guard(), neighbor="K2YE-5", via="K6FB-5") is None

    def test_direct_user_exempt(self):
        # arrival_via empty (direct RF user) → nothing to loop back to.
        assert _ok(_guard(), neighbor="K6FB-5", via="") is None

    def test_disabled(self):
        assert _ok(_guard(interlock=False), neighbor="K6FB-5", via="K6FB-5") is None


# ─── Rate limiting ────────────────────────────────────────────────────────────

class TestRateLimit:
    def test_window_and_slide(self):
        g = _guard(rate_limit_per_min=2)
        # Two accepted connects fill the window.
        assert g.acquire("KF6ANX-4") and g.acquire("KF6ANX-4")
        assert _ok(g, user="KF6ANX-4") is not None        # 3rd refused by rate
        # Age the timestamps out of the 60 s window.
        g._rate["KF6ANX"][0] -= 61
        g._rate["KF6ANX"][1] -= 61
        assert _ok(g, user="KF6ANX-4") is None            # window slid → allowed

    def test_node_wide_across_callers_independent(self):
        g = _guard(rate_limit_per_min=1)
        assert g.acquire("KF6ANX-4")
        assert _ok(g, user="KF6ANX-4") is not None        # this caller limited
        assert _ok(g, user="K6FB-5") is None              # a different caller is not

    def test_disabled_never_limits(self):
        g = _guard(rate_limit_per_min=0)
        for _ in range(10):
            g.acquire("KF6ANX-4")
        assert _ok(g, user="KF6ANX-4") is None


# ─── Circuit caps ─────────────────────────────────────────────────────────────

class TestCaps:
    def test_per_user_cap(self):
        g = _guard(max_circuits_per_user=2, max_circuits=10)
        assert g.acquire("KF6ANX-4") and g.acquire("KF6ANX-4")
        assert g.acquire("KF6ANX-4") is False             # 3rd for this user refused
        assert g.acquire("K6FB-5") is True                # a different user still ok
        assert g.active_for("KF6ANX-4") == 2

    def test_node_wide_cap(self):
        g = _guard(max_circuits=2, max_circuits_per_user=5)
        assert g.acquire("KF6ANX-4") and g.acquire("K6FB-5")
        assert g.acquire("N6ZX-5") is False               # node total full
        assert g.active_total == 2

    def test_release_frees_budget(self):
        g = _guard(max_circuits=1, max_circuits_per_user=1)
        assert g.acquire("KF6ANX-4")
        assert g.acquire("KF6ANX-4") is False
        g.release("KF6ANX-4")
        assert g.active_total == 0 and g.active_for("KF6ANX-4") == 0
        assert g.acquire("KF6ANX-4") is True              # freed


# ─── Dashboard stats + refusal ring (N4c) ─────────────────────────────────────

class TestStats:
    def test_stats_shape(self):
        g = _guard(max_circuits=4, max_circuits_per_user=2,
                   deny=frozenset({"NOCALL"}))
        g.acquire("KF6ANX-4")
        g.note_refusal("BADCALL-1", "Access denied.", "K2YE-5")
        s = g.stats()
        assert s["active"] == 1 and s["max"] == 4 and s["max_per_user"] == 2
        assert s["per_user"] == {"KF6ANX": 1}
        assert s["policy"]["deny"] == ["NOCALL"]
        assert s["policy"]["interlock"] is True
        assert s["policy"]["min_auth"] == "identified"
        r = s["recent_refusals"]
        assert len(r) == 1
        assert r[0]["user"] == "BADCALL-1" and r[0]["dest"] == "K2YE-5"
        assert "denied" in r[0]["reason"].lower() and "ts" in r[0]

    def test_refusal_ring_bounded(self):
        g = _guard()
        for i in range(GatewayGuard._REFUSAL_HISTORY + 20):
            g.note_refusal(f"CALL{i}", "x")
        assert len(g.stats()["recent_refusals"]) == GatewayGuard._REFUSAL_HISTORY
