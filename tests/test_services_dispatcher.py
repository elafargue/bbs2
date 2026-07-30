"""
tests/test_services_dispatcher.py — unit tests for the ax25d-style
ServiceDispatcher (route matching, lockout, no_digi, min_auth, argv %-subst).
"""
from __future__ import annotations

from bbs.services.dispatcher import ServiceAction, ServiceDispatcher
from bbs.transport.base import Connection


def _conn(remote: str, local: str, *, hop: int = 0, transport: str = "agwpe") -> Connection:
    """Build a Connection with dummy streams (dispatcher only reads addresses)."""
    return Connection(
        remote_addr=remote,
        reader=object(),  # type: ignore[arg-type]  — match() never touches streams
        writer=object(),  # type: ignore[arg-type]
        transport_id=transport,
        hop_count=hop,
        local_addr=local,
    )


_CFG = {
    "enabled": True,
    "lockout": ["NOCALL", "N0CALL"],
    "max_sessions": 5,
    "routes": {
        "W6ELA-2": {"exec": "/usr/bin/xfbbd", "args": ["xfbbd", "%U"]},
        "W6ELA-3": {"exec": "/usr/sbin/node", "args": ["node"]},
        "W6ELA-4": {"exec": "/usr/bin/game", "args": ["game"], "no_digi": True},
        "W6ELA-5": {"exec": "/usr/bin/secret", "args": ["secret"], "min_auth": "authenticated"},
        "W6ELA-6": {"exec": "/usr/bin/open", "args": ["open"], "min_auth": "none"},
    },
}


class TestMatch:
    def setup_method(self):
        self.d = ServiceDispatcher(_CFG)

    def test_no_route_passes_to_bbs(self):
        d = self.d.match(_conn("KN6PE-7", "W6ELA-1"))
        assert d.action is ServiceAction.PASS

    def test_matched_route_execs(self):
        d = self.d.match(_conn("KN6PE-7", "W6ELA-2"))
        assert d.action is ServiceAction.EXEC
        assert d.route is not None and d.route.exec_path == "/usr/bin/xfbbd"

    def test_called_match_is_case_insensitive(self):
        d = self.d.match(_conn("KN6PE-7", "w6ela-2"))
        assert d.action is ServiceAction.EXEC

    def test_lockout_by_base_callsign(self):
        d = self.d.match(_conn("NOCALL-5", "W6ELA-2"))
        assert d.action is ServiceAction.REFUSE
        assert d.reason == "locked out"

    def test_lockout_by_full_callsign(self):
        d = self.d.match(_conn("N0CALL", "W6ELA-2"))
        assert d.action is ServiceAction.REFUSE

    def test_no_digi_refuses_digipeated(self):
        assert self.d.match(_conn("KN6PE-7", "W6ELA-4", hop=1)).action is ServiceAction.REFUSE
        assert self.d.match(_conn("KN6PE-7", "W6ELA-4", hop=0)).action is ServiceAction.EXEC

    def test_min_auth_authenticated_is_refused(self):
        # We can't reach AUTHENTICATED before bridging, so OTP-gated routes refuse.
        d = self.d.match(_conn("KN6PE-7", "W6ELA-5"))
        assert d.action is ServiceAction.REFUSE
        assert d.reason == "auth level too low"

    def test_min_auth_none_execs(self):
        assert self.d.match(_conn("KN6PE-7", "W6ELA-6")).action is ServiceAction.EXEC

    def test_invalid_caller_callsign_refused(self):
        d = self.d.match(_conn("!!bogus!!", "W6ELA-2"))
        assert d.action is ServiceAction.REFUSE
        assert "invalid caller" in d.reason


class TestConfig:
    def test_disabled_when_no_routes(self):
        d = ServiceDispatcher({"enabled": True, "routes": {}})
        assert d.enabled is False
        assert d.match(_conn("KN6PE-7", "W6ELA-2")).action is ServiceAction.PASS

    def test_explicit_disable(self):
        d = ServiceDispatcher({"enabled": False, "routes": {"W6ELA-2": {"exec": "/usr/bin/x"}}})
        assert d.enabled is False

    def test_relative_exec_path_skipped(self):
        d = ServiceDispatcher({"routes": {"W6ELA-2": {"exec": "xfbbd", "args": ["xfbbd"]}}})
        assert "W6ELA-2" not in d.route_callsigns()
        assert d.match(_conn("KN6PE-7", "W6ELA-2")).action is ServiceAction.PASS

    def test_missing_exec_skipped(self):
        d = ServiceDispatcher({"routes": {"W6ELA-2": {"args": ["x"]}}})
        assert d.route_callsigns() == []

    def test_default_argv0_is_basename(self):
        d = ServiceDispatcher({"routes": {"W6ELA-2": {"exec": "/usr/sbin/node"}}})
        route = d.match(_conn("KN6PE-7", "W6ELA-2")).route
        assert route is not None and route.args == ["node"]

    def test_route_callsigns(self):
        d = ServiceDispatcher(_CFG)
        assert set(d.route_callsigns()) == {"W6ELA-2", "W6ELA-3", "W6ELA-4", "W6ELA-5", "W6ELA-6"}

    def test_max_sessions_parsed(self):
        assert ServiceDispatcher(_CFG).max_sessions == 5


class TestBuildArgv:
    def setup_method(self):
        self.d = ServiceDispatcher(_CFG)

    def test_substitutions(self):
        route = self.d.match(_conn("KN6PE-7", "W6ELA-2")).route
        assert route is not None
        route.args = ["prog", "%u", "%U", "%s", "%S", "%d", "%%"]
        argv = self.d.build_argv(route, _conn("KN6PE-7", "W6ELA-2", transport="agwpe"), port="0")
        assert argv == ["prog", "kn6pe", "KN6PE", "kn6pe-7", "KN6PE-7", "0", "%"]

    def test_port_defaults_to_transport_id(self):
        route = self.d.match(_conn("KN6PE-7", "W6ELA-2")).route
        assert route is not None
        route.args = ["prog", "%d"]
        argv = self.d.build_argv(route, _conn("KN6PE-7", "W6ELA-2", transport="netrom"))
        assert argv == ["prog", "netrom"]
