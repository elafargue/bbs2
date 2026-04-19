"""
tests/test_heard_map.py — Unit tests for the ASCII network-map feature
(_map_confirmed_edges, _render_ascii_map, _build_map_data).
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path


import pytest

from bbs.core.terminal import Terminal, ColorMode
from bbs.plugins.heard.heard import (
    HeardPlugin,
    _map_confirmed_edges,
    _render_ascii_map,
)


# ---------------------------------------------------------------------------
# _map_confirmed_edges — same logic as _confirmed_edges in the REST module
# ---------------------------------------------------------------------------

class TestMapConfirmedEdges:
    BBS = "W6ELA"

    def test_direct_gives_single_edge(self):
        assert _map_confirmed_edges("KF6ANX", "", self.BBS) == [("KF6ANX", self.BBS)]

    def test_no_star_returns_empty(self):
        assert _map_confirmed_edges("W6OAK", "WOODY,KBULN", self.BBS) == []

    def test_single_starred_digi(self):
        assert _map_confirmed_edges("W6OAK", "WOODY*", self.BBS) == [
            ("W6OAK", "WOODY"), ("WOODY", self.BBS)
        ]

    def test_two_starred_digis(self):
        assert _map_confirmed_edges("N6YP", "KJOHN*,KBULN*", self.BBS) == [
            ("N6YP", "KJOHN"), ("KJOHN", "KBULN"), ("KBULN", self.BBS)
        ]

    def test_last_star_cutoff(self):
        # KBETH (after WOODY*) must be discarded
        edges = _map_confirmed_edges("N6YP", "KJOHN*,KBULN,WOODY*,KBETH", self.BBS)
        targets = [b for _, b in edges]
        assert self.BBS in targets
        assert "KBETH" not in targets


# ---------------------------------------------------------------------------
# _render_ascii_map — pure renderer tests
# ---------------------------------------------------------------------------

def _make_data(
    children=None, digis=None, stn_count=None, stn_calls=None,
    direct_count=0, direct_calls=None
):
    return {
        "children":     children  or {},
        "digis":        digis     or set(),
        "stn_count":    stn_count or {},
        "stn_calls":    stn_calls or {},
        "direct_count": direct_count,
        "direct_calls": direct_calls or [],
    }



class _FakeWriter:
    def write(self, data: bytes) -> None:
        pass
    async def drain(self) -> None: pass
    def is_closing(self) -> bool: return False
    def close(self) -> None: pass
    async def wait_closed(self) -> None: pass


class TestRenderAsciiMap:

    BBS = "W6ELA"
    W = 80  # terminal width for tests

    

    @pytest.mark.asyncio
    async def test_empty_data_returns_no_paths_message(self):
        data = _make_data()
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        assert any("no confirmed" in ln.lower() for ln in lines)

    @pytest.mark.asyncio
    async def test_header_contains_bbs_call(self):
        data = _make_data(direct_count=1, direct_calls=["KF6ANX"])
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        assert lines[0] == f"NETMAP {self.BBS}"

    @pytest.mark.asyncio
    async def test_direct_only_digis_mode(self):
        data = _make_data(direct_count=3, direct_calls=["A", "B", "C"])
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        joined = "\n".join(lines)
        assert "[direct: 3 stn]" in joined

    @pytest.mark.asyncio
    async def test_direct_only_full_mode_lists_calls(self):
        data = _make_data(direct_count=2, direct_calls=["KF6ANX", "W6WPL"])
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=False, term=term)
        joined = "\n".join(lines)
        assert "[direct]" in joined
        assert "KF6ANX" in joined
        assert "W6WPL" in joined

    @pytest.mark.asyncio
    async def test_single_digi_with_stations(self):
        data = _make_data(
            children={self.BBS: ["WOODY"]},
            digis={"WOODY"},
            stn_count={"WOODY": 2},
            stn_calls={"WOODY": ["N6YP", "W6OAK"]},
        )
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        joined = "\n".join(lines)
        assert "WOODY [2]" in joined
        # Station callsigns should NOT appear in digi-only mode
        assert "N6YP" not in joined
        assert "W6OAK" not in joined

    @pytest.mark.asyncio
    async def test_full_mode_shows_station_callsigns(self):
        data = _make_data(
            children={self.BBS: ["WOODY"]},
            digis={"WOODY"},
            stn_count={"WOODY": 2},
            stn_calls={"WOODY": ["N6YP", "W6OAK"]},
        )
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=False, term=term)
        joined = "\n".join(lines)
        assert "N6YP" in joined
        assert "W6OAK" in joined

    @pytest.mark.asyncio
    async def test_nested_digis(self):
        data = _make_data(
            children={self.BBS: ["WOODY"], "WOODY": ["KJOHN"]},
            digis={"WOODY", "KJOHN"},
            stn_count={"KJOHN": 1},
            stn_calls={"KJOHN": ["W1ABC"]},
        )
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        joined = "\n".join(lines)
        assert "WOODY" in joined
        assert "KJOHN [1]" in joined

    @pytest.mark.asyncio
    async def test_last_item_uses_backslash_connector(self):
        """The last item at any level must use \\-- not +--."""
        data = _make_data(direct_count=1, direct_calls=["KF6ANX"])
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        # Only one item → must use \\--
        assert any("\\--" in ln for ln in lines)

    @pytest.mark.asyncio
    async def test_non_last_item_uses_plus_connector(self):
        data = _make_data(
            children={self.BBS: ["WOODY", "KRDG"]},
            digis={"WOODY", "KRDG"},
            direct_count=0,
        )
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        joined = "\n".join(lines)
        # First digi uses +--
        assert "+--WOODY" in joined or "+--KRDG" in joined

    @pytest.mark.asyncio
    async def test_callsign_wrapping(self):
        """Long station lists must wrap at terminal width."""
        calls = [f"K{i:04d}" for i in range(20)]
        data = _make_data(direct_count=len(calls), direct_calls=calls)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.OFF)
        term.width = 40  # force narrow width for test
        lines = _render_ascii_map(self.BBS, data, digis_only=False, term=term)
        # No line exceeds 40 chars
        assert all(len(ln) <= 40 for ln in lines), \
            f"Line(s) too long: {[ln for ln in lines if len(ln) > 40]}"

    @pytest.mark.asyncio
    async def test_digi_without_station_children_shows_no_count(self):
        """A digi with 0 direct-child stations shows no bracket."""
        data = _make_data(
            children={self.BBS: ["RELAY"]},
            digis={"RELAY"},
            stn_count={},
        )
        reader = asyncio.StreamReader()
        reader.feed_eof()
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        joined = "\n".join(lines)
        # Should appear as just "RELAY", not "RELAY [0]"
        assert "RELAY [0]" not in joined
        assert "RELAY" in joined


# ---------------------------------------------------------------------------
# _build_map_data integration tests
# ---------------------------------------------------------------------------

async def _make_heard_plugin() -> HeardPlugin:
    tmp = tempfile.mkdtemp(prefix="bbs2_map_test_")
    db_path = str(Path(tmp) / "test.db")
    plugin = HeardPlugin()
    await plugin.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    now = int(time.time())
    # Direct station
    await plugin.on_heard("KF6ANX", "BEACON", [], now, "agwpe")
    # Via one digi (confirmed)
    await plugin.on_heard("W6OAK", "BEACON", ["WOODY*"], now, "agwpe")
    await plugin.on_heard("N6YP",  "BEACON", ["WOODY*"], now, "agwpe")
    # Via two digis (both confirmed)
    await plugin.on_heard("W1ABC", "APRS",   ["KJOHN*", "WOODY*"], now, "agwpe")
    # Unconfirmed path (no star): should not create an edge
    await plugin.on_heard("W6WPL", "BEACON", ["WOODY", "KRDG"], now, "agwpe")
    return plugin



@pytest.mark.asyncio
class TestBuildMapData:
    BBS = "W6ELA"

    async def test_direct_station_counted(self):
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        # KF6ANX (empty via) AND W6WPL (via WOODY,KRDG — no * = heard direct)
        assert data["direct_count"] == 2
        assert "KF6ANX" in data["direct_calls"]
        assert "W6WPL"  in data["direct_calls"]

    async def test_woody_is_a_digi(self):
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        assert "WOODY" in data["digis"]

    async def test_woody_is_child_of_bbs(self):
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        assert "WOODY" in data["children"].get(self.BBS, [])

    async def test_woody_station_count(self):
        """W6OAK and N6YP directly talk to WOODY."""
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        # W6OAK and N6YP are direct children of WOODY
        assert data["stn_count"].get("WOODY", 0) == 2

    async def test_kjohn_child_of_woody(self):
        """KJOHN relays through WOODY, so KJOHN is a child of WOODY."""
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        assert "KJOHN" in data["digis"]
        assert "KJOHN" in data["children"].get("WOODY", [])

    async def test_w1abc_child_of_kjohn(self):
        """W1ABC directly talks to KJOHN (via KJOHN*,WOODY*)."""
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        assert data["stn_count"].get("KJOHN", 0) == 1
        assert "W1ABC" in data["stn_calls"].get("KJOHN", [])

    async def test_unconfirmed_path_creates_no_edge(self):
        """W6WPL via WOODY,KRDG (no *) must not appear as a digi edge."""
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        # KRDG should not be a digi (no confirmed frame via it)
        assert "KRDG" not in data["digis"]

    @pytest.mark.asyncio
    async def test_render_roundtrip(self):
        """Integration: build data, render, get valid ASCII output."""
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        from bbs.core.terminal import ColorMode
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=True, term=term)
        assert lines[0] == f"NETMAP {self.BBS}"
        assert len(lines) > 1
        # All lines must be plain ASCII (no colour escape codes)
        joined = "\n".join(lines)
        assert "\x1b" not in joined

    @pytest.mark.asyncio
    async def test_render_full_mode(self):
        plugin = await _make_heard_plugin()
        data = await plugin._build_map_data(self.BBS)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        from bbs.core.terminal import ColorMode
        term = Terminal(reader, _FakeWriter(), color_mode=ColorMode.TRUECOLOR)
        lines = _render_ascii_map(self.BBS, data, digis_only=False, term=term)
        joined = "\n".join(lines)
        # Direct stations and digi-relayed stations all appear
        assert "KF6ANX" in joined
        assert "W6OAK"  in joined
        assert "N6YP"   in joined
        assert "W1ABC"  in joined
