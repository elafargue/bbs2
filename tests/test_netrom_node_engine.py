"""
tests/test_netrom_node_engine.py — engine-level N3 wiring.

Covers:
  - _on_connection routes a connect to the node SSID → native node landing,
    with precedence over service dispatch and the BBS; unset node_ssid keeps
    today's behavior (BBS SSID → BBS).
  - the local-application registry (_build_netrom_apps): BBS + each service.
  - the non-closing conn wrapper used to run sub-apps.
  - _run_node_native exit contract: BYE ⇒ the connection is closed.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from bbs.config import BBSConfig
from bbs.core.auth import AuthLevel
from bbs.core.engine import BBSEngine, _NonClosingWriter, _non_closing_conn
from bbs.netrom.gateway import GatewayPolicy
from bbs.plugins.node.node import NetromNodePlugin
from bbs.netrom.router import NetromRouter, RouteEntry
from bbs.services.dispatcher import ServiceDispatcher
from bbs.transport.base import Connection


def _cfg(*, node_ssid=None, services: dict | None = None) -> BBSConfig:
    netrom = {"alias": "PALO"}
    if node_ssid is not None:
        netrom["node_ssid"] = node_ssid
    return BBSConfig(
        callsign="W6ELA", ssid=1, name="Test", sysop="W6ELA", location="",
        max_users=10, idle_timeout=0, write_timeout=30,
        path_length_medium_hops=1, path_length_long_hops=3,
        transports={}, database={"path": ":memory:"}, auth={}, plugins={},
        web={}, logging={}, netrom=netrom, services=services or {},
    )


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, key, default=None):
        return default


def _conn(local: str, *, reader=None, remote="N0USER-1") -> Connection:
    return Connection(
        remote_addr=remote,
        reader=reader if reader is not None else asyncio.StreamReader(),
        writer=_FakeWriter(),          # type: ignore[arg-type]
        transport_id="agwpe",
        local_addr=local,
    )


_SVC = {
    "enabled": True,
    "routes": {"W6ELA-9": {"exec": "/bin/cat", "args": ["cat"]}},
}


# ─── Dispatch precedence ──────────────────────────────────────────────────────

class TestDispatch:
    async def test_node_ssid_routes_to_native_landing(self, monkeypatch):
        eng = BBSEngine(_cfg(node_ssid=5, services=_SVC))
        eng._services = ServiceDispatcher(_SVC)
        eng._netrom_node_call = "W6ELA-5"
        seen: list = []
        monkeypatch.setattr(eng, "_run_node_native",
                            lambda conn: _record(seen, "node"))
        monkeypatch.setattr(eng, "_run_external_service",
                            lambda conn, route: _record(seen, "svc"))
        await eng._on_connection(_conn("W6ELA-5"))
        assert seen == ["node"]                     # node wins, no service/BBS

    async def test_node_alias_routes_to_native_landing(self, monkeypatch):
        # connect <alias> (e.g. PALO) lands in the node prompt like the SSID.
        eng = BBSEngine(_cfg(node_ssid=5, services=_SVC))
        eng._services = ServiceDispatcher(_SVC)
        eng._netrom_node_call = "W6ELA-5"
        eng._netrom_node_alias = "PALO"
        seen: list = []
        monkeypatch.setattr(eng, "_run_node_native",
                            lambda conn: _record(seen, "node"))
        monkeypatch.setattr(eng, "_run_external_service",
                            lambda conn, route: _record(seen, "svc"))
        await eng._on_connection(_conn("PALO"))
        assert seen == ["node"]

    async def test_node_ssid_wins_over_service_on_same_ssid(self, monkeypatch):
        # A service also mapped on W6ELA-5 must NOT shadow the node landing.
        svc = {"enabled": True, "routes": {"W6ELA-5": {"exec": "/bin/cat"}}}
        eng = BBSEngine(_cfg(node_ssid=5, services=svc))
        eng._services = ServiceDispatcher(svc)
        eng._netrom_node_call = "W6ELA-5"
        seen: list = []
        monkeypatch.setattr(eng, "_run_node_native",
                            lambda conn: _record(seen, "node"))
        monkeypatch.setattr(eng, "_run_external_service",
                            lambda conn, route: _record(seen, "svc"))
        await eng._on_connection(_conn("W6ELA-5"))
        assert seen == ["node"]

    async def test_bbs_ssid_still_goes_to_bbs(self, monkeypatch):
        eng = BBSEngine(_cfg(node_ssid=5))
        eng._services = ServiceDispatcher({})
        eng._netrom_node_call = "W6ELA-5"
        seen: list = []
        monkeypatch.setattr(eng, "_run_node_native",
                            lambda conn: _record(seen, "node"))
        monkeypatch.setattr(eng, "_run_session",
                            lambda session: _record(seen, "bbs"))
        await eng._on_connection(_conn("W6ELA-1"))   # BBS SSID
        assert seen == ["bbs"]

    async def test_unset_node_ssid_keeps_bbs_behavior(self, monkeypatch):
        eng = BBSEngine(_cfg())                      # no node_ssid
        eng._services = ServiceDispatcher({})
        # _netrom_node_call stays None → no native landing at all.
        seen: list = []
        monkeypatch.setattr(eng, "_run_node_native",
                            lambda conn: _record(seen, "node"))
        monkeypatch.setattr(eng, "_run_session",
                            lambda session: _record(seen, "bbs"))
        await eng._on_connection(_conn("W6ELA-1"))
        assert seen == ["bbs"]


async def _record(seen: list, tag: str) -> None:
    seen.append(tag)


# ─── Local-application registry ───────────────────────────────────────────────

class TestAppRegistry:
    def test_registry_has_bbs_and_services(self):
        eng = BBSEngine(_cfg(node_ssid=5, services=_SVC))
        eng._services = ServiceDispatcher(_SVC)
        apps = eng._build_netrom_apps()
        assert "BBS" in apps
        assert "W6ELA-9" in apps                     # service by called SSID

    def test_registry_bbs_only_without_services(self):
        eng = BBSEngine(_cfg(node_ssid=5))
        eng._services = ServiceDispatcher({})
        apps = eng._build_netrom_apps()
        assert list(apps) == ["BBS"]


# ─── Non-closing wrapper (sub-app link protection) ────────────────────────────

class TestNonClosingConn:
    async def test_close_is_noop_but_write_delegates(self):
        inner = _FakeWriter()
        w = _NonClosingWriter(inner)
        w.write(b"hi")
        await w.drain()
        w.close()
        assert bytes(inner.buffer) == b"hi"
        assert inner.closed is False                 # underlying link untouched

    def test_is_closing_reflects_inner(self):
        inner = _FakeWriter()
        w = _NonClosingWriter(inner)
        assert w.is_closing() is False
        inner.closed = True
        assert w.is_closing() is True

    def test_non_closing_conn_preserves_addressing(self):
        c = Connection(
            remote_addr="KF6ANX-4", reader=object(),   # type: ignore[arg-type]
            writer=_FakeWriter(), transport_id="agwpe", local_addr="W6ELA-1",
        )
        wrapped = _non_closing_conn(c)
        assert wrapped.remote_addr == "KF6ANX-4"
        assert wrapped.local_addr == "W6ELA-1"
        assert wrapped.reader is c.reader
        wrapped.writer.close()
        assert c.writer.closed is False              # real writer not closed


# ─── Native landing exit contract ─────────────────────────────────────────────

def _seeded_router() -> NetromRouter:
    r = NetromRouter("W6ELA-5", "PALO")
    r._upsert_route(RouteEntry(
        dest_call="KF6ANX-4", alias="JOHN", neighbor_call="KF6ANX-4",
        quality=192, via_call="KF6ANX-4", via_alias="JOHN", last_seen=time.time(),
    ))
    return r


class TestGuardWiring:
    def test_plugin_builds_shared_guard_and_threads_arrival_via(self):
        plugin = NetromNodePlugin()
        plugin.bind(
            router=_seeded_router(), transports=[],
            node_call="W6ELA-5", node_alias="PALO", apps={},
            gateway_policy=GatewayPolicy(deny=frozenset({"BADCALL"})),
        )
        conn = Connection(
            remote_addr="KF6ANX-9", reader=object(),   # type: ignore[arg-type]
            writer=_FakeWriter(), transport_id="netrom",
            local_addr="W6ELA-5", netrom_via="K6FB-5",  # arrived via a crosslink
        )
        node = plugin._make_node(
            term=object(), conn=conn, user_call="KF6ANX-9",
            may_connect=True, idle_timeout=None, on_activity=None,
            auth_level=AuthLevel.IDENTIFIED,
        )
        assert node._guard is plugin._guard                # one shared authority
        assert node.arrival_via == "K6FB-5"                # from conn.netrom_via
        assert node._guard.policy.deny == {"BADCALL"}      # policy applied
        assert node.auth_level is AuthLevel.IDENTIFIED


class TestActivitySnapshot:
    def _bound_plugin(self):
        plugin = NetromNodePlugin()
        plugin.bind(
            router=_seeded_router(), transports=[],
            node_call="W6ELA-5", node_alias="PALO", apps={},
            gateway_policy=GatewayPolicy(),
        )
        return plugin

    def test_snapshot_idle(self):
        snap = self._bound_plugin().activity_snapshot()
        assert snap["enabled"] is True
        assert snap["node_call"] == "W6ELA-5" and snap["node_alias"] == "PALO"
        assert snap["sessions"] == []
        assert snap["gateway"]["active"] == 0 and snap["gateway"]["max"] == 4

    async def test_live_session_appears_then_clears(self):
        from bbs.core.terminal import Terminal
        plugin = self._bound_plugin()
        reader = asyncio.StreamReader()               # no input → sits at =>
        conn = _conn("W6ELA-5", reader=reader, remote="KF6ANX-9")
        conn.netrom_via = "K6FB-5"                     # arrived via a crosslink
        term = await Terminal.create(reader, conn.writer, echo=False, eol="\r")
        task = asyncio.create_task(
            plugin.run_native(term=term, conn=conn, user_call="KF6ANX-9")
        )
        await asyncio.sleep(0.05)                      # reach the => prompt
        sessions = plugin.activity_snapshot()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["user"] == "KF6ANX-9"
        assert sessions[0]["entry"] == "native"
        assert sessions[0]["via"] == "K6FB-5"
        assert sessions[0]["target"] is None          # at the prompt
        reader.feed_data(b"B\r")                       # BYE → ends the session
        await asyncio.wait_for(task, timeout=2.0)
        assert plugin.activity_snapshot()["sessions"] == []

    def test_engine_snapshot_delegates(self, monkeypatch):
        eng = BBSEngine(_cfg(node_ssid=5))
        plugin = self._bound_plugin()
        monkeypatch.setattr(eng.plugin_registry, "get",
                            lambda name: plugin if name == "node" else None)
        snap = eng.netrom_snapshot()
        assert snap["enabled"] is True and snap["node_call"] == "W6ELA-5"

    def test_engine_snapshot_disabled_without_node(self, monkeypatch):
        eng = BBSEngine(_cfg())
        monkeypatch.setattr(eng.plugin_registry, "get", lambda name: None)
        assert eng.netrom_snapshot() == {"enabled": False}


class TestNativeLanding:
    async def test_bye_closes_the_connection(self, monkeypatch):
        eng = BBSEngine(_cfg(node_ssid=5))
        eng._services = ServiceDispatcher({})
        eng._netrom_node_call = "W6ELA-5"

        plugin = NetromNodePlugin()
        plugin.bind(
            router=_seeded_router(), transports=[],
            node_call="W6ELA-5", node_alias="PALO", apps={},
        )
        monkeypatch.setattr(eng.plugin_registry, "get",
                            lambda name: plugin if name == "node" else None)

        reader = asyncio.StreamReader()
        reader.feed_data(b"B\r")            # BYE at the => prompt
        conn = _conn("W6ELA-5", reader=reader, remote="KF6ANX-9")

        await asyncio.wait_for(eng._run_node_native(conn), timeout=2.0)
        assert conn.writer.closed is True                     # exit contract
        assert b"73 from PALO" in bytes(conn.writer.buffer)    # node farewell

    async def test_max_users_rejects_node_connect(self, monkeypatch):
        eng = BBSEngine(_cfg(node_ssid=5))
        eng.cfg.max_users = 1
        eng._sessions = {"a": object()}     # already at cap
        eng._netrom_node_call = "W6ELA-5"
        plugin = NetromNodePlugin()
        plugin.bind(router=_seeded_router(), transports=[],
                    node_call="W6ELA-5", node_alias="PALO", apps={})
        monkeypatch.setattr(eng.plugin_registry, "get",
                            lambda name: plugin if name == "node" else None)
        conn = _conn("W6ELA-5")
        await asyncio.wait_for(eng._run_node_native(conn), timeout=2.0)
        assert conn.writer.closed is True
        assert b"full" in bytes(conn.writer.buffer)
