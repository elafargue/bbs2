"""
tests/test_heard_map_tag.py — Tests for the ``<MAP:lat,lon,call[,nodename]>``
location-beacon tag parser and its integration into ``HeardPlugin.on_heard``.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import aiosqlite
import pytest

from bbs.plugins.heard.heard import HeardPlugin, _parse_map_tag


# ---------------------------------------------------------------------------
# Unit tests — _parse_map_tag()
# ---------------------------------------------------------------------------

class TestParseMapTag:
    def test_three_arg_form(self):
        assert _parse_map_tag("<MAP:38.5795,-121.4934,KK6SEN>") == (
            38.5795, -121.4934, "KK6SEN", ""
        )

    def test_four_arg_form_with_nodename(self):
        assert _parse_map_tag("<MAP:38.5795,-121.4934,KK6SEN,AUBNOD>") == (
            38.5795, -121.4934, "KK6SEN", "AUBNOD"
        )

    def test_integer_coordinates(self):
        # Latitude / longitude may be plain integers
        assert _parse_map_tag("<MAP:0,0,N0CALL>") == (0.0, 0.0, "N0CALL", "")

    def test_negative_coordinates(self):
        result = _parse_map_tag("<MAP:-33.86,-151.21,VK2ABC>")
        assert result == (-33.86, -151.21, "VK2ABC", "")

    def test_explicit_positive_sign(self):
        # Allow leading '+'
        assert _parse_map_tag("<MAP:+34.05,+118.24,KX1>") == (
            34.05, 118.24, "KX1", ""
        )

    def test_tag_inside_surrounding_text(self):
        info = "Hi there! pos=<MAP:34.05,-118.24,KX1Y,LANODE> CQ CQ"
        assert _parse_map_tag(info) == (34.05, -118.24, "KX1Y", "LANODE")

    def test_case_insensitive_marker(self):
        assert _parse_map_tag("<map:1.0,2.0,n0call>") == (1.0, 2.0, "N0CALL", "")

    def test_whitespace_around_commas(self):
        assert _parse_map_tag("<MAP: 38.5 , -121.4 , KK6SEN , AUBNOD >") == (
            38.5, -121.4, "KK6SEN", "AUBNOD"
        )

    def test_callsign_with_ssid_suffix(self):
        # Callsign may contain a hyphen (e.g. APRS-style SSIDs)
        result = _parse_map_tag("<MAP:1.0,2.0,W6ELA-9,MOBILE>")
        assert result == (1.0, 2.0, "W6ELA-9", "MOBILE")

    def test_nodename_with_slash_dual_alias(self):
        # Some nodes advertise two aliases as "PRIMARY/SECONDARY"
        result = _parse_map_tag("<MAP:38.5795,-121.4934,KK6SEN,AUBNOD/SACNOD>")
        assert result == (38.5795, -121.4934, "KK6SEN", "AUBNOD/SACNOD")

    def test_first_tag_wins_when_multiple(self):
        info = "<MAP:1.0,2.0,FIRST><MAP:3.0,4.0,SECOND>"
        assert _parse_map_tag(info) == (1.0, 2.0, "FIRST", "")

    # ── Invalid / rejected inputs ────────────────────────────────────────────

    def test_no_tag_returns_none(self):
        assert _parse_map_tag("Just a regular beacon, no tag here") is None

    def test_empty_string_returns_none(self):
        assert _parse_map_tag("") is None

    def test_latitude_out_of_range_returns_none(self):
        assert _parse_map_tag("<MAP:91.0,0.0,KX1>") is None
        assert _parse_map_tag("<MAP:-91.0,0.0,KX1>") is None

    def test_longitude_out_of_range_returns_none(self):
        assert _parse_map_tag("<MAP:0.0,181.0,KX1>") is None
        assert _parse_map_tag("<MAP:0.0,-181.0,KX1>") is None

    def test_missing_callsign_returns_none(self):
        assert _parse_map_tag("<MAP:1.0,2.0>") is None

    def test_missing_comma_returns_none(self):
        assert _parse_map_tag("<MAP:1.0 2.0 KX1>") is None

    def test_unterminated_tag_returns_none(self):
        assert _parse_map_tag("<MAP:1.0,2.0,KX1") is None


# ---------------------------------------------------------------------------
# Integration tests — HeardPlugin.on_heard() with MAP tag
# ---------------------------------------------------------------------------

async def _make_plugin() -> HeardPlugin:
    tmp = tempfile.mkdtemp(prefix="bbs2_heard_maptag_test_")
    db_path = str(Path(tmp) / "test.db")
    plugin = HeardPlugin()
    await plugin.initialize({"enabled": True, "max_age_hours": 0}, db_path)
    return plugin


async def _fetch_station(plugin: HeardPlugin, callsign: str, transport: str) -> dict:
    async with aiosqlite.connect(plugin._db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT lat, lon, nodename, position_source "
            "FROM heard_stations WHERE callsign = ? AND transport = ?",
            (callsign.upper(), transport),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None, f"station {callsign} on {transport!r} not found"
    return dict(row)


class TestOnHeardMapTag:
    async def test_matching_callsign_populates_coords(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now, "agwpe",
            "<MAP:38.5795,-121.4934,KK6SEN,AUBNOD>",
        )
        row = await _fetch_station(plugin, "KK6SEN", "agwpe")
        assert row["lat"] == pytest.approx(38.5795)
        assert row["lon"] == pytest.approx(-121.4934)
        assert row["nodename"] == "AUBNOD"
        assert row["position_source"] == "beacon"

    async def test_three_arg_form_clears_nodename(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now, "agwpe",
            "<MAP:38.5795,-121.4934,KK6SEN>",
        )
        row = await _fetch_station(plugin, "KK6SEN", "agwpe")
        assert row["lat"] == pytest.approx(38.5795)
        assert row["nodename"] == ""
        assert row["position_source"] == "beacon"

    async def test_mismatched_callsign_is_ignored(self):
        """A station may not announce coordinates on behalf of another."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "W1AW", "BEACON", [], now, "agwpe",
            "<MAP:38.5795,-121.4934,KK6SEN>",   # W1AW claims KK6SEN's location
        )
        row = await _fetch_station(plugin, "W1AW", "agwpe")
        assert row["lat"] is None
        assert row["lon"] is None
        assert row["position_source"] == ""
        # No row was implicitly created for KK6SEN either
        async with aiosqlite.connect(plugin._db_path) as db:
            async with db.execute(
                "SELECT 1 FROM heard_stations WHERE callsign='KK6SEN'"
            ) as cur:
                assert await cur.fetchone() is None

    async def test_tag_inside_arbitrary_text(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KX1Y", "BEACON", [], now, "kiss",
            "Good morning! <MAP:34.05,-118.24,KX1Y> 73",
        )
        row = await _fetch_station(plugin, "KX1Y", "kiss")
        assert row["lat"] == pytest.approx(34.05)
        assert row["lon"] == pytest.approx(-118.24)

    async def test_no_tag_does_not_touch_coords(self):
        """A plain beacon must not overwrite previously stored coordinates."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now, "agwpe",
            "<MAP:38.5795,-121.4934,KK6SEN>",
        )
        # Second frame, no tag — should leave the coords alone
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now + 60, "agwpe",
            "just a normal status message",
        )
        row = await _fetch_station(plugin, "KK6SEN", "agwpe")
        assert row["lat"] == pytest.approx(38.5795)
        assert row["lon"] == pytest.approx(-121.4934)
        assert row["position_source"] == "beacon"

    async def test_new_tag_updates_coords(self):
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now, "agwpe",
            "<MAP:38.0,-121.0,KK6SEN,OLDNODE>",
        )
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now + 60, "agwpe",
            "<MAP:39.0,-122.0,KK6SEN,NEWNODE>",
        )
        row = await _fetch_station(plugin, "KK6SEN", "agwpe")
        assert row["lat"] == pytest.approx(39.0)
        assert row["lon"] == pytest.approx(-122.0)
        assert row["nodename"] == "NEWNODE"

    async def test_tag_in_digipeated_frame_updates_source(self):
        """A station whose beacon reaches us via a digi must still get coords."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KX1Y", "BEACON", ["WOODY*", "WIDE2-1"], now, "agwpe",
            "<MAP:34.05,-118.24,KX1Y,LANODE>",
        )
        row = await _fetch_station(plugin, "KX1Y", "agwpe")
        assert row["lat"] == pytest.approx(34.05)
        assert row["nodename"] == "LANODE"
        # The digi (WOODY) gets a seeded row but no coordinates
        woody = await _fetch_station(plugin, "WOODY", "")
        assert woody["lat"] is None
        assert woody["position_source"] == ""

    async def test_invalid_coordinates_do_not_touch_row(self):
        plugin = await _make_plugin()
        now = int(time.time())
        # First, establish a valid position
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now, "agwpe",
            "<MAP:38.5,-121.5,KK6SEN>",
        )
        # Garbage tag in second frame — parser rejects, prior coords kept
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now + 1, "agwpe",
            "<MAP:999,9999,KK6SEN>",
        )
        row = await _fetch_station(plugin, "KK6SEN", "agwpe")
        assert row["lat"] == pytest.approx(38.5)
        assert row["lon"] == pytest.approx(-121.5)

    async def test_src_with_ssid_matches_base_callsign_in_tag(self):
        """KC7HEX-10 may legitimately beacon <MAP:...,KC7HEX,NODE>."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KC7HEX-10", "BEACON", [], now, "agwpe",
            "<MAP:45.0,-120.0,KC7HEX,HEXNODE>",
        )
        row = await _fetch_station(plugin, "KC7HEX-10", "agwpe")
        assert row["lat"] == pytest.approx(45.0)
        assert row["lon"] == pytest.approx(-120.0)
        assert row["nodename"] == "HEXNODE"
        assert row["position_source"] == "beacon"

    async def test_src_base_matches_tag_with_ssid(self):
        """The base callsign may beacon a tag containing an SSID suffix."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KC7HEX", "BEACON", [], now, "agwpe",
            "<MAP:45.0,-120.0,KC7HEX-10,HEXNODE>",
        )
        row = await _fetch_station(plugin, "KC7HEX", "agwpe")
        assert row["lat"] == pytest.approx(45.0)
        assert row["nodename"] == "HEXNODE"

    async def test_different_ssids_same_base_match(self):
        """KC7HEX-10 may beacon for KC7HEX-15 — both belong to the same operator."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KC7HEX-10", "BEACON", [], now, "agwpe",
            "<MAP:45.0,-120.0,KC7HEX-15,HEXNODE>",
        )
        row = await _fetch_station(plugin, "KC7HEX-10", "agwpe")
        assert row["lat"] == pytest.approx(45.0)
        assert row["nodename"] == "HEXNODE"

    async def test_different_base_callsigns_still_rejected(self):
        """SSID stripping must not allow a totally different base to match."""
        plugin = await _make_plugin()
        now = int(time.time())
        await plugin.on_heard(
            "KC7HEX-10", "BEACON", [], now, "agwpe",
            "<MAP:45.0,-120.0,W1AW,FAKE>",
        )
        row = await _fetch_station(plugin, "KC7HEX-10", "agwpe")
        assert row["lat"] is None
        assert row["nodename"] == ""

    async def test_case_insensitive_callsign_match(self):
        plugin = await _make_plugin()
        now = int(time.time())
        # MAP-tag callsign in lowercase, src in uppercase — should still match
        await plugin.on_heard(
            "KK6SEN", "BEACON", [], now, "agwpe",
            "<MAP:38.5,-121.5,kk6sen,aubnod>",
        )
        row = await _fetch_station(plugin, "KK6SEN", "agwpe")
        assert row["lat"] == pytest.approx(38.5)
        assert row["nodename"] == "AUBNOD"
