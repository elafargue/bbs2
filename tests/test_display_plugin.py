"""
tests/test_display_plugin.py — Unit tests for the framebuffer display plugin.

Coverage
--------
Renderer helpers
  - _fmt_uptime: seconds / minutes / hours / days formatting
  - _fmt_ts: Unix timestamp to MM-DD HH:MM
  - _abbrev_transport: transport ID short names

Renderer.draw_frame
  - Returns a PIL Image with correct dimensions when Pillow is available
  - Returns None gracefully when Pillow is unavailable (import patched out)

DisplayPlugin event handlers
  - _on_heard: populates heard_scroll deque, updates activity timestamp
  - _on_disconnected: prepends to last_conns, deduplicates callsign, ignores
    anonymous ws: addresses
  - _on_bulletin: increments count for known area, creates entry for new area

Idle / power-saving state machine
  - No dim when idle time < idle_dim_minutes
  - Dim when idle time >= idle_dim_minutes
  - Screen-off when idle time >= idle_off_minutes
  - Wake clears dim and off flags

Settings hot-reload
  - update_settings() applies changes and invalidates the renderer

Platform guard
  - Render task is NOT created on non-Linux systems (sys.platform patched)
  - Subscriptions ARE still registered even when platform guard fires
"""
from __future__ import annotations

import asyncio
import re
import tempfile
import time
from collections import deque
from pathlib import Path
from unittest.mock import patch

import pytest

from bbs.core.event_bus import PluginEventBus
from bbs.plugins.display.display import DisplayPlugin, _DEFAULTS, _clean_info
from bbs.plugins.display.renderer import (
    BulletinArea,
    DisplayState,
    LastConn,
    Renderer,
    _abbrev_transport,
    _fmt_ts,
    _fmt_uptime,
)
from bbs.transport.agwpe import _parse_info, _parse_via


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


async def _make_plugin(tmp_path: Path, cfg: dict | None = None) -> DisplayPlugin:
    """Create and initialize a DisplayPlugin backed by a fresh temp DB."""
    plugin = DisplayPlugin()
    await plugin.initialize(cfg or {}, _tmp_db(tmp_path))
    return plugin


# ── _fmt_uptime ───────────────────────────────────────────────────────────────

class TestFmtUptime:
    def test_seconds_only_shows_minutes(self):
        assert _fmt_uptime(45) == "0m"

    def test_one_minute(self):
        assert _fmt_uptime(60) == "1m"

    def test_minutes(self):
        assert _fmt_uptime(5 * 60 + 30) == "5m"

    def test_hours_and_minutes(self):
        result = _fmt_uptime(3 * 3600 + 12 * 60)
        assert result == "3h 12m"

    def test_hours_zero_minutes(self):
        assert _fmt_uptime(2 * 3600) == "2h 00m"

    def test_days(self):
        result = _fmt_uptime(3 * 86400 + 5 * 3600 + 45 * 60)
        assert result == "3d 5h"

    def test_one_day(self):
        result = _fmt_uptime(86400 + 1800)
        assert result == "1d 0h"


# ── _fmt_ts ───────────────────────────────────────────────────────────────────

def test_fmt_ts_returns_nonempty_string():
    ts  = time.time()
    out = _fmt_ts(ts)
    # MM-DD HH:MM  → 11 chars
    assert len(out) == 11
    assert "-" in out and ":" in out


def test_fmt_ts_zero():
    # Should not crash on zero
    out = _fmt_ts(0)
    assert isinstance(out, str) and len(out) == 11


# ── _abbrev_transport ─────────────────────────────────────────────────────────

class TestAbbrevTransport:
    def test_known_kiss_tcp(self):
        assert _abbrev_transport("kiss_tcp") == "KISS/TCP"

    def test_known_agwpe(self):
        assert _abbrev_transport("agwpe") == "AGWPE"

    def test_known_web(self):
        assert _abbrev_transport("web") == "WEB"

    def test_unknown_returns_upper_truncated(self):
        result = _abbrev_transport("my_very_long_transport_name")
        assert len(result) <= 8
        assert result == result.upper()

    def test_unknown_short(self):
        assert _abbrev_transport("tcp") == "TCP"


# ── Renderer.draw_frame ───────────────────────────────────────────────────────

class TestRenderer:
    def test_draw_frame_returns_image(self):
        """draw_frame() produces a PIL Image of the correct size."""
        PIL = pytest.importorskip("PIL.Image")
        renderer = Renderer(width=480, height=320)
        state    = DisplayState(
            bbs_callsign="W6TEST",
            uptime_seconds=3600.0,
            last_conns=[
                LastConn("W6OAK",  "kiss_tcp", time.time() - 100),
                LastConn("KF6ANX", "agwpe",    time.time() - 200),
            ],
            bulletins=[
                BulletinArea("GENERAL", total=12, new=3),
                BulletinArea("DX",      total=5,  new=0),
            ],
            heard_scroll=[
                {"src": "W6OAK", "dest": "BEACON", "transport": "kiss_tcp",
                 "info": "", "count": 1, "via_list": ["WOODY"], "heard_set": {0}},
                {"src": "KF6ANX", "dest": "APRS", "transport": "agwpe",
                 "info": "", "count": 1, "via_list": [], "heard_set": set()},
            ],
        )
        img = renderer.draw_frame(state)

        assert img is not None
        assert img.size == (480, 320)
        assert img.mode == "RGB"

    def test_draw_frame_empty_state(self):
        """draw_frame() must not raise when state has no data."""
        pytest.importorskip("PIL.Image")
        renderer = Renderer(width=480, height=320)
        img      = renderer.draw_frame(DisplayState())
        assert img is not None
        assert img.size == (480, 320)

    def test_draw_frame_returns_none_without_pillow(self):
        """When Pillow is unavailable draw_frame() returns None silently."""
        renderer = Renderer(width=480, height=320)
        with patch("bbs.plugins.display.renderer._PIL_OK", False):
            result = renderer.draw_frame(DisplayState())
        assert result is None

    def test_custom_dimensions(self):
        """Renderer respects non-default width/height."""
        pytest.importorskip("PIL.Image")
        renderer = Renderer(width=320, height=240)
        img      = renderer.draw_frame(DisplayState())
        assert img.size == (320, 240)


# ── DisplayPlugin._on_heard ───────────────────────────────────────────────────

class TestOnHeard:
    async def test_basic_entry_added(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        assert len(plugin._heard_scroll) == 0

        await plugin._on_heard({
            "callsign": "W6OAK", "dest": "BEACON",
            "transport": "kiss_tcp", "via": "WOODY*",
            "timestamp": int(time.time()),
        })

        assert len(plugin._heard_scroll) == 1
        entry = plugin._heard_scroll[0]
        assert entry["src"] == "W6OAK"

    async def test_newest_first(self, tmp_path):
        plugin = await _make_plugin(tmp_path)

        await plugin._on_heard({"callsign": "ALPHA", "dest": "", "transport": "", "via": ""})
        await plugin._on_heard({"callsign": "BETA",  "dest": "", "transport": "", "via": ""})

        # appendleft → newest at index 0
        assert plugin._heard_scroll[0]["src"] == "BETA"
        assert plugin._heard_scroll[1]["src"] == "ALPHA"

    async def test_updates_activity_timestamp(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        plugin._last_activity_ts = time.monotonic() - 1000  # far in the past

        await plugin._on_heard({
            "callsign": "W6TEST", "dest": "", "transport": "", "via": "",
        })

        assert time.monotonic() - plugin._last_activity_ts < 1.0

    async def test_scroll_respects_maxlen(self, tmp_path):
        max_scroll = 5
        plugin = await _make_plugin(tmp_path, {"max_heard_scroll": str(max_scroll)})

        for i in range(max_scroll + 3):
            await plugin._on_heard({
                "callsign": f"ST{i}", "dest": "", "transport": "", "via": "",
            })

        assert len(plugin._heard_scroll) == max_scroll

    async def test_via_included_in_entry(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({
            "callsign": "W1XYZ", "dest": "APRS",
            "transport": "agwpe", "via": "RELAY*",
        })
        assert "RELAY" in plugin._heard_scroll[0]["via_list"]

    async def test_transport_abbrev_included(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({
            "callsign": "W1XYZ", "dest": "", "transport": "agwpe", "via": "",
        })
        assert plugin._heard_scroll[0]["transport"] == "agwpe"

    async def test_info_field_appended(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({
            "callsign": "W6PKT-10", "dest": "BEACON",
            "transport": "agwpe", "via": "",
            "info": "Winlink RMS, PMAIL, NETROM relay",
        })
        entry = plugin._heard_scroll[0]
        assert entry["src"] == "W6PKT-10"
        assert entry["dest"] == "BEACON"
        assert "Winlink RMS" in entry["info"]

    async def test_empty_info_no_colon(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({
            "callsign": "W6OAK", "dest": "BEACON",
            "transport": "kiss_tcp", "via": "", "info": "",
        })
        entry = plugin._heard_scroll[0]
        assert entry["info"] == ""  # no info stored when absent


# ── _clean_info ───────────────────────────────────────────────────────────────

class TestCleanInfo:
    def test_strips_agwpe_hex_escapes(self):
        assert _clean_info("Hello<0x0d>") == "Hello"

    def test_strips_multiple_hex_escapes(self):
        assert _clean_info("AB<0x0a><0x0d>CD") == "ABCD"

    def test_strips_control_characters(self):
        assert _clean_info("Hello\r\nWorld") == "HelloWorld"

    def test_strips_whitespace(self):
        assert _clean_info("  hello  ") == "hello"

    def test_empty_input(self):
        assert _clean_info("") == ""

    def test_plain_text_unchanged(self):
        msg = "Winlink RMS, PMAIL, NETROM relay, Laughlin Ridge"
        assert _clean_info(msg) == msg


# ── DisplayPlugin._on_disconnected ────────────────────────────────────────────

class TestOnDisconnected:
    async def test_identified_station_added(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_disconnected({
            "callsign": "W6OAK", "transport": "kiss_tcp",
            "timestamp": time.time(), "auth_level": "IDENTIFIED",
        })
        assert len(plugin._last_conns) == 1
        assert plugin._last_conns[0].callsign == "W6OAK"

    async def test_newest_is_first(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        for call in ("W6OAK", "KF6ANX", "N6YP"):
            await plugin._on_disconnected({
                "callsign": call, "transport": "kiss_tcp", "timestamp": time.time(),
            })
        assert plugin._last_conns[0].callsign == "N6YP"

    async def test_max_three_kept(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        for call in ("A1A", "B2B", "C3C", "D4D"):
            await plugin._on_disconnected({
                "callsign": call, "transport": "tcp", "timestamp": time.time(),
            })
        assert len(plugin._last_conns) == 3

    async def test_duplicate_callsign_deduplicated(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        ts1 = time.time() - 300
        ts2 = time.time()
        await plugin._on_disconnected({
            "callsign": "W6OAK", "transport": "kiss_tcp", "timestamp": ts1,
        })
        await plugin._on_disconnected({
            "callsign": "W6OAK", "transport": "tcp", "timestamp": ts2,
        })
        # Only one entry for W6OAK, and it's the newer one at index 0
        calls = [c.callsign for c in plugin._last_conns]
        assert calls.count("W6OAK") == 1
        assert plugin._last_conns[0].callsign == "W6OAK"
        assert abs(plugin._last_conns[0].timestamp - ts2) < 1.0

    async def test_anonymous_ws_address_ignored(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_disconnected({
            "callsign": "ws:abcd1234", "transport": "web",
            "timestamp": time.time(),
        })
        assert len(plugin._last_conns) == 0

    async def test_empty_callsign_ignored(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_disconnected({
            "callsign": "", "transport": "tcp", "timestamp": time.time(),
        })
        assert len(plugin._last_conns) == 0

    async def test_updates_activity_timestamp(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        plugin._last_activity_ts = time.monotonic() - 1000

        await plugin._on_disconnected({
            "callsign": "W6OAK", "transport": "tcp", "timestamp": time.time(),
        })

        assert time.monotonic() - plugin._last_activity_ts < 1.0


# ── DisplayPlugin._on_bulletin ────────────────────────────────────────────────

class TestOnBulletin:
    async def test_increments_existing_area(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        plugin._bulletin_areas["GENERAL"] = BulletinArea("GENERAL", total=10, new=1)

        await plugin._on_bulletin({
            "area": "GENERAL", "from_call": "W6OAK",
            "to_call": "ALL", "subject": "Test", "timestamp": time.time(),
        })

        a = plugin._bulletin_areas["GENERAL"]
        assert a.total == 11
        assert a.new   == 2

    async def test_creates_new_area_entry(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        assert "NEWAREA" not in plugin._bulletin_areas

        await plugin._on_bulletin({
            "area": "NEWAREA", "from_call": "W6OAK",
            "to_call": "ALL", "subject": "Hi", "timestamp": time.time(),
        })

        a = plugin._bulletin_areas["NEWAREA"]
        assert a.total == 1
        assert a.new   == 1

    async def test_area_name_normalised_to_upper(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        await plugin._on_bulletin({
            "area": "general", "from_call": "W1XYZ", "to_call": "ALL",
            "subject": "Test", "timestamp": time.time(),
        })
        assert "GENERAL" in plugin._bulletin_areas


# ── Idle / power-saving state machine ────────────────────────────────────────

class TestIdleStateMachine:
    def _plugin_with_settings(self, plugin: DisplayPlugin, **overrides) -> DisplayPlugin:
        """Apply setting overrides to the plugin in-place and return it."""
        plugin._settings.update({k: str(v) for k, v in overrides.items()})
        return plugin

    async def test_no_dim_when_recently_active(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        self._plugin_with_settings(plugin, idle_dim_minutes=5, idle_off_minutes=30)

        plugin._last_activity_ts = time.monotonic() - 10  # 10s ago
        plugin._update_idle_state()

        assert plugin._is_dimmed is False
        assert plugin._is_off    is False

    async def test_dim_after_threshold(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        self._plugin_with_settings(plugin, idle_dim_minutes=5, idle_off_minutes=30)

        plugin._last_activity_ts = time.monotonic() - (6 * 60)  # 6 min ago
        plugin._update_idle_state()

        assert plugin._is_dimmed is True
        assert plugin._is_off    is False

    async def test_off_after_threshold(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        self._plugin_with_settings(
            plugin, idle_dim_minutes=5, idle_off_minutes=10,
            fb_device="",  # no actual fb write
        )

        plugin._last_activity_ts = time.monotonic() - (11 * 60)  # 11 min ago
        plugin._update_idle_state()

        assert plugin._is_off    is True
        assert plugin._is_dimmed is True

    async def test_zero_dim_minutes_never_dims(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        self._plugin_with_settings(plugin, idle_dim_minutes=0, idle_off_minutes=0)

        plugin._last_activity_ts = time.monotonic() - (99 * 3600)  # ancient
        plugin._update_idle_state()

        assert plugin._is_dimmed is False
        assert plugin._is_off    is False

    async def test_wake_clears_dim_and_off(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        plugin._is_dimmed = True
        plugin._is_off    = True

        plugin.wake()

        assert plugin._is_dimmed is False
        assert plugin._is_off    is False

    async def test_wake_resets_activity_timestamp(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        plugin._last_activity_ts = time.monotonic() - 9999

        plugin.wake()

        assert time.monotonic() - plugin._last_activity_ts < 1.0

    async def test_second_idle_off_call_is_idempotent(self, tmp_path):
        """_update_idle_state called repeatedly in off state must not crash."""
        plugin = await _make_plugin(tmp_path)
        self._plugin_with_settings(
            plugin, idle_dim_minutes=1, idle_off_minutes=2, fb_device="",
        )
        plugin._last_activity_ts = time.monotonic() - (5 * 60)
        plugin._is_off = True  # already off

        # Should not raise or change state unexpectedly
        plugin._update_idle_state()
        assert plugin._is_off is True


# ── Settings hot-reload ───────────────────────────────────────────────────────

class TestSettingsHotReload:
    async def test_update_settings_applies_values(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        plugin.update_settings({"refresh_interval": "2.5", "dim_level": "50"})

        assert plugin._settings["refresh_interval"] == "2.5"
        assert plugin._settings["dim_level"]        == "50"

    async def test_update_settings_ignores_unknown_keys(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        before = dict(plugin._settings)

        plugin.update_settings({"no_such_key": "value"})

        assert plugin._settings == before

    async def test_update_settings_invalidates_renderer(self, tmp_path):
        pytest.importorskip("PIL.Image")
        plugin = await _make_plugin(tmp_path)

        # Force renderer creation
        plugin._render_frame()
        assert plugin._renderer is not None

        # Changing font_path should invalidate it
        plugin.update_settings({"font_path": "/some/new/font.ttf"})
        assert plugin._renderer is None


# ── Platform guard ────────────────────────────────────────────────────────────

class TestPlatformGuard:
    async def test_render_task_not_started_on_non_linux(self, tmp_path):
        plugin = await _make_plugin(tmp_path)
        bus    = PluginEventBus()

        with patch("bbs.plugins.display.display.sys.platform", "darwin"):
            plugin.set_event_bus(bus)

        assert plugin._render_task is None

    async def test_subscriptions_registered_on_non_linux(self, tmp_path):
        """Event subscriptions must be active even when rendering is disabled."""
        plugin = await _make_plugin(tmp_path)
        bus    = PluginEventBus()

        with patch("bbs.plugins.display.display.sys.platform", "darwin"):
            plugin.set_event_bus(bus)

        # Heard events should still update the scroll buffer
        await bus.publish("heard.station", {
            "callsign": "W6OAK", "dest": "", "transport": "kiss_tcp", "via": "",
        })
        assert len(plugin._heard_scroll) == 1

    async def test_render_task_started_on_linux(self, tmp_path):
        """On 'linux', the render task IS created."""
        plugin = await _make_plugin(tmp_path)
        bus    = PluginEventBus()

        with patch("bbs.plugins.display.display.sys.platform", "linux"):
            plugin.set_event_bus(bus)

        try:
            assert plugin._render_task is not None
        finally:
            # Clean up the background task so pytest doesn't warn about it
            if plugin._render_task:
                plugin._render_task.cancel()
                try:
                    await plugin._render_task
                except (asyncio.CancelledError, Exception):
                    pass


# ── Consecutive-packet deduplication ─────────────────────────────────────────

class TestHeardDedup:
    async def test_consecutive_dedup_single_entry(self, tmp_path):
        """Two identical consecutive packets collapse to one scroll entry."""
        plugin = await _make_plugin(tmp_path)
        payload = {"callsign": "W6OAK", "dest": "BEACON", "transport": "kiss", "via": ""}
        await plugin._on_heard(payload)
        await plugin._on_heard(payload)
        assert len(plugin._heard_scroll) == 1

    async def test_consecutive_dedup_count_badge(self, tmp_path):
        """The (xN) badge appears after the second occurrence."""
        plugin = await _make_plugin(tmp_path)
        payload = {"callsign": "W6OAK", "dest": "BEACON", "transport": "kiss", "via": ""}
        await plugin._on_heard(payload)
        await plugin._on_heard(payload)
        assert plugin._heard_scroll[0]["count"] == 2
        await plugin._on_heard(payload)
        assert plugin._heard_scroll[0]["count"] == 3

    async def test_different_key_not_deduped(self, tmp_path):
        """Different callsigns produce separate scroll entries."""
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({"callsign": "W6OAK", "dest": "", "transport": "", "via": ""})
        await plugin._on_heard({"callsign": "N6YP",  "dest": "", "transport": "", "via": ""})
        assert len(plugin._heard_scroll) == 2
        assert plugin._heard_scroll[0]["count"] == 1

    async def test_dedup_resets_after_window(self, tmp_path):
        """Same key after the 60-s window starts a fresh entry (no badge)."""
        plugin = await _make_plugin(tmp_path)
        payload = {"callsign": "W6OAK", "dest": "BEACON", "transport": "kiss", "via": ""}
        await plugin._on_heard(payload)
        # Expire the window by backdating the cache timestamp
        plugin._last_heard["ts"] -= 61.0
        await plugin._on_heard(payload)
        assert len(plugin._heard_scroll) == 2
        assert plugin._heard_scroll[0]["count"] == 1

    async def test_heard_set_union(self, tmp_path):
        """H-bit set grows as more digipeaters relay the packet."""
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({"callsign": "W6OAK", "dest": "BEACON",
                                 "transport": "kiss", "via": "WOODY"})
        await plugin._on_heard({"callsign": "W6OAK", "dest": "BEACON",
                                 "transport": "kiss", "via": "WOODY*"})
        assert plugin._last_heard["heard_set"] == {0}

    async def test_via_updated_in_text(self, tmp_path):
        """After dedup, heard_set marks WOODY as confirmed relay."""
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({"callsign": "W6OAK", "dest": "BEACON",
                                 "transport": "kiss", "via": "WOODY"})
        await plugin._on_heard({"callsign": "W6OAK", "dest": "BEACON",
                                 "transport": "kiss", "via": "WOODY*"})
        entry = plugin._heard_scroll[0]
        assert "WOODY" in entry["via_list"]
        assert 0 in entry["heard_set"]  # WOODY (index 0) confirmed as relay

    async def test_no_badge_on_first_occurrence(self, tmp_path):
        """First occurrence never shows a count badge."""
        plugin = await _make_plugin(tmp_path)
        await plugin._on_heard({"callsign": "W6OAK", "dest": "", "transport": "", "via": ""})
        assert plugin._heard_scroll[0]["count"] == 1


# ─── Real-traffic dedup (fixture-based) ──────────────────────────────────────

_FIXTURE_LOG        = Path(__file__).parent / "fixtures" / "agwpe_monitor.log"
_LOG_LINE_RE        = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\t(.+)", re.DOTALL)
_MONITOR_HEADER_RE  = re.compile(r"Fm\s+(\S+)\s+To\s+(\S+)", re.IGNORECASE)


def _grep_monitor_log(callsign: str, dest: str, path: Path = _FIXTURE_LOG) -> list[str]:
    """Return monitor strings for *callsign*/*dest* from the fixture log."""
    results = []
    with open(path, "rb") as f:
        for raw in f:
            try:
                line = raw.decode("latin-1").rstrip("\n")
            except Exception:
                continue
            lm = _LOG_LINE_RE.match(line)
            if not lm:
                continue
            monitor = lm.group(1)
            hm = _MONITOR_HEADER_RE.search(monitor)
            if hm and hm.group(1).upper() == callsign.upper() \
                    and hm.group(2).upper() == dest.upper():
                results.append(monitor)
    return results


def _payload(monitor: str, transport: str = "agwpe") -> dict:
    hm = _MONITOR_HEADER_RE.search(monitor)
    return {
        "callsign":  hm.group(1) if hm else "?",
        "dest":      hm.group(2) if hm else "",
        "transport": transport,
        "via":       ",".join(_parse_via(monitor)),
        "info":      _parse_info(monitor),
    }


@pytest.mark.skipif(not _FIXTURE_LOG.exists(), reason="fixture log not present")
class TestDedupRealTraffic:
    """
    End-to-end dedup using real captured AGWPE traffic.

    W6ABJ-12 broadcast its ID beacon once; Direwolf received three copies —
    one per digipeater that relayed it (KJOHN*, KBERR*, WOODY*) — all within
    3 seconds and with identical payload.  They must collapse to a single
    scroll entry with count=3.

    N6YP's BEACON was received twice in 1 second via WOODY* then KBERR*.
    Same rule applies.
    """

    async def test_w6abj_three_hops_dedup_to_one_entry(self, tmp_path):
        frames = _grep_monitor_log("W6ABJ-12", "ID")
        assert len(frames) >= 3, "Fixture must contain ≥3 W6ABJ-12 ID frames"

        plugin = await _make_plugin(tmp_path)
        for monitor in frames[:3]:
            await plugin._on_heard(_payload(monitor))

        assert len(plugin._heard_scroll) == 1
        assert plugin._last_heard["count"] == 3
        assert plugin._heard_scroll[0]["count"] == 3

    async def test_n6yp_two_hops_dedup_to_one_entry(self, tmp_path):
        frames = _grep_monitor_log("N6YP", "BEACON")
        assert len(frames) >= 2, "Fixture must contain ≥2 N6YP BEACON frames"

        plugin = await _make_plugin(tmp_path)
        for monitor in frames[:2]:
            await plugin._on_heard(_payload(monitor))

        assert len(plugin._heard_scroll) == 1
        assert plugin._last_heard["count"] == 2
        assert plugin._heard_scroll[0]["count"] == 2

    async def test_different_stations_not_deduped(self, tmp_path):
        """W6ABJ-12 and N6YP frames must produce separate scroll entries."""
        plugin = await _make_plugin(tmp_path)
        for monitor in _grep_monitor_log("W6ABJ-12", "ID")[:1]:
            await plugin._on_heard(_payload(monitor))
        for monitor in _grep_monitor_log("N6YP", "BEACON")[:1]:
            await plugin._on_heard(_payload(monitor))

        assert len(plugin._heard_scroll) == 2
