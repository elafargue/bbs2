"""
bbs/plugins/display/display.py — Framebuffer status display plugin.

Runs a background asyncio task that renders the BBS status to a small
framebuffer screen (default: 480×320 @ /dev/fb0).

Subscribes to the plugin event bus:
  heard.station         — updates the scrolling RF log
  session.disconnected  — updates the "last 3 connections" section
  bulletin.new_message  — increments area new-message counter

Bulletin area totals and last-connection history are pre-loaded from the
SQLite database on startup so the display is useful immediately.

Configuration (bbs.yaml plugins.display section or web panel)
--------------------------------------------------------------
  fb_device          : /dev/fb0
  width              : 480
  height             : 320
  refresh_interval   : 1.0        # seconds between renders
  idle_dim_minutes   : 5          # dim after N minutes idle (0 = never)
  idle_off_minutes   : 30         # blank after N minutes idle (0 = never)
  dim_level          : 20         # brightness % when dimmed (1–100)
  backlight_path     :            # /sys/class/backlight/.../brightness (optional)
  backlight_max      : 255        # max value written to backlight_path
  font_path          :            # path to TTF/TTC font (empty = auto-detect)
  bulletin_new_hours : 24         # messages posted within this window are "new"
  max_heard_scroll   : 20         # length of the scrolling RF heard deque

The plugin registers in the PluginRegistry but has no BBS menu entry
(menu_key = ""), so it never appears in the terminal menu.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from typing import Any, Optional, TYPE_CHECKING

import aiosqlite

from bbs.core.plugin_registry import BBSPlugin
from bbs.plugins.display.renderer import (
    DisplayState, LastConn, BulletinArea,
    Renderer, write_to_fb, blank_fb, _abbrev_transport,
)

if TYPE_CHECKING:
    from bbs.core.session import BBSSession
    from bbs.core.event_bus import PluginEventBus

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS display_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_DEFAULTS: dict[str, str] = {
    "fb_device":          "/dev/fb0",
    "width":              "480",
    "height":             "320",
    "refresh_interval":   "1.0",
    "idle_dim_minutes":   "5",
    "idle_off_minutes":   "30",
    "dim_level":          "20",
    "backlight_path":     "",
    "backlight_max":      "255",
    "font_path":          "",
    "bulletin_new_hours": "24",
    "max_heard_scroll":   "20",
}

# Consecutive-packet deduplication (mirrors Paracon _last_unproto logic)
_HEARD_DEDUP_WINDOW = 60.0  # seconds


def _starred_via_indices(via_str: str) -> set:
    """Return 0-based indices of via entries whose H-bit is set ('*' suffix)."""
    if not via_str:
        return set()
    return {
        i for i, v in enumerate(via_str.split(","))
        if v.strip().endswith("*")
    }


def _build_heard_entry(
    src: str, dest: str, via_str: str, transport: str, info: str,
    count: int, heard_set: set,
) -> dict:
    """Build a structured scroll-entry dict for one (possibly deduped) packet."""
    via_list = [v.rstrip("*") for v in via_str.split(",") if v.strip()]
    return {
        "src":       src,
        "dest":      dest,
        "transport": transport,
        "info":      info,
        "count":     count,
        "via_list":  via_list,
        "heard_set": heard_set,
    }


class DisplayPlugin(BBSPlugin):
    # ── Plugin identity ───────────────────────────────────────────────────────
    name         = "display"
    display_name = "Framebuffer Display"
    menu_key     = ""           # headless: no BBS menu entry
    min_auth_level_name = "SYSOP"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        await super().initialize(cfg, db_path)

        # Settings: start from bbs.yaml, override from DB
        self._settings: dict[str, str] = dict(_DEFAULTS)
        for k, v in cfg.items():
            if k in _DEFAULTS:
                self._settings[k] = str(v)

        await self._ensure_schema()
        await self._load_db_settings()

        # Display state
        max_scroll = int(self._settings["max_heard_scroll"])
        self._last_conns:      deque[LastConn]  = deque(maxlen=4)
        self._active_sessions: dict[str, LastConn] = {}   # key: remote_addr
        self._heard_scroll: deque[dict]       = deque(maxlen=max_scroll)
        self._bulletin_areas: dict[str, BulletinArea] = {}

        self._last_activity_ts: float = time.monotonic()
        self._is_dimmed: bool = False
        self._is_off:    bool = False
        self._last_bulletin_refresh: float = 0.0
        self._last_conns_refresh:   float = 0.0   # set after _preload_last_conns
        # Single-slot consecutive-dedup cache (Paracon _last_unproto pattern)
        self._last_heard: Optional[dict] = None

        # Renderer (initialised lazily once we know font_path from DB)
        self._renderer: Optional[Renderer] = None

        # Pre-load historical data
        await self._preload_last_conns()
        self._last_conns_refresh = time.monotonic()   # don't re-query immediately
        await self._refresh_bulletin_counts()

        # Background render task (started after set_event_bus)
        self._render_task: Optional[asyncio.Task[None]] = None

    def set_event_bus(self, bus: "PluginEventBus") -> None:
        super().set_event_bus(bus)
        bus.subscribe("heard.station",           self._on_heard)
        bus.subscribe("session.connected",       self._on_connected)
        bus.subscribe("session.authenticated",   self._on_authenticated)
        bus.subscribe("session.disconnected",    self._on_disconnected)
        bus.subscribe("bulletin.new_message",    self._on_bulletin)

        if not self.enabled:
            return

        if sys.platform != "linux":
            logger.warning(
                "Display plugin: framebuffer rendering requires Linux "
                "(running on %r) — render loop not started.",
                sys.platform,
            )
            return

        # Start the render loop now that we have the bus
        self._render_task = asyncio.create_task(
            self._render_loop(), name="display:render"
        )

    async def shutdown(self) -> None:
        if self._render_task is not None:
            self._render_task.cancel()
            try:
                await self._render_task
            except asyncio.CancelledError:
                pass
        # Blank the screen on clean shutdown
        fb = self._settings["fb_device"]
        if fb:
            blank_fb(fb)

    async def handle_session(self, session: "BBSSession") -> None:
        """
        The display plugin has no interactive session.  If somehow called
        (e.g. from a future admin command), just return immediately.
        """
        return

    def get_stats(self) -> dict[str, Any]:
        base = super().get_stats()
        base["display_name"] = self.display_name
        base["fb_device"]    = self._settings.get("fb_device", "")
        base["is_dimmed"]    = self._is_dimmed
        base["is_off"]       = self._is_off
        base["last_activity_ago"] = round(time.monotonic() - self._last_activity_ts)
        base["heard_scroll_len"]  = len(self._heard_scroll)
        base["bulletin_areas"]    = [
            {"name": a.name, "total": a.total, "new": a.new}
            for a in self._bulletin_areas.values()
        ]
        base["last_conns"] = [
            {"callsign": c.callsign, "transport": c.transport,
             "timestamp": c.timestamp}
            for c in self._last_conns
        ]
        return base

    # ── Runtime settings update (called from web route) ───────────────────────

    def update_settings(self, new_settings: dict[str, str]) -> None:
        """
        Hot-reload display settings without restarting the plugin.
        Called from the Flask thread — only writes to self._settings (safe
        under CPython GIL for simple dict updates).
        """
        for k, v in new_settings.items():
            if k in _DEFAULTS:
                self._settings[k] = str(v)

        # Rebuild the renderer if font or dimensions changed
        self._renderer = None

    # ── Event bus callbacks ───────────────────────────────────────────────────

    async def _on_heard(self, payload: dict[str, Any]) -> None:
        callsign  = payload.get("callsign", "?")
        dest      = payload.get("dest", "")
        via_str   = payload.get("via", "")
        transport = payload.get("transport", "")
        info      = _clean_info(payload.get("info", ""))

        key = (callsign, dest, transport, info)
        now = time.time()
        last = self._last_heard

        if (
            last is not None
            and last["key"] == key
            and now - last["ts"] < _HEARD_DEDUP_WINDOW
        ):
            # Consecutive duplicate — update in-place at the top of the scroll
            last["count"] += 1
            last["ts"]     = now
            new_via_list = [v.rstrip("*") for v in via_str.split(",") if v.strip()]
            if new_via_list:
                last["via_list"] = new_via_list
            last["heard_set"] |= _starred_via_indices(via_str)
            self._heard_scroll[0] = _build_heard_entry(
                last["src"], last["dest"], ",".join(last["via_list"]),
                last["transport"], last["info"], last["count"],
                last["heard_set"],
            )
        else:
            # New (or expired) packet — prepend a fresh entry
            heard_set = _starred_via_indices(via_str)
            entry = _build_heard_entry(callsign, dest, via_str, transport, info, 1, heard_set)
            self._heard_scroll.appendleft(entry)
            self._last_heard = {
                "key":       key,
                "ts":        now,
                "count":     1,
                "heard_set": heard_set,
                "src":       callsign,
                "dest":      dest,
                "transport": transport,
                "via_list":  entry["via_list"],
                "info":      info,
            }

        self._last_activity_ts = time.monotonic()

    async def _on_connected(self, payload: dict[str, Any]) -> None:
        remote_addr = payload.get("remote_addr", "?")
        transport   = payload.get("transport", "")
        ts          = payload.get("timestamp", time.time())
        self._active_sessions[remote_addr] = LastConn(
            callsign=remote_addr, transport=transport,
            timestamp=ts, active=True,
        )
        self._last_activity_ts = time.monotonic()

    async def _on_authenticated(self, payload: dict[str, Any]) -> None:
        """Update the active-session entry with the now-known callsign."""
        callsign    = payload.get("callsign", "")
        remote_addr = payload.get("remote_addr", "?")
        if not callsign:
            return
        existing = self._active_sessions.pop(remote_addr, None)
        if existing is not None:
            self._active_sessions[remote_addr] = LastConn(
                callsign=callsign,
                transport=existing.transport,
                timestamp=existing.timestamp,
                active=True,
            )
        self._last_activity_ts = time.monotonic()

    async def _on_disconnected(self, payload: dict[str, Any]) -> None:
        callsign  = payload.get("callsign", "")
        remote_addr = payload.get("remote_addr", callsign)
        transport = payload.get("transport", "")
        ts        = payload.get("timestamp", time.time())
        # Remove from active sessions (try remote_addr first, then callsign)
        self._active_sessions.pop(remote_addr, None)
        if callsign != remote_addr:
            self._active_sessions.pop(callsign, None)
        # Only record identified stations (skip anonymous/unresolved addrs)
        if callsign and not callsign.startswith("ws:"):
            conn = LastConn(callsign=callsign, transport=transport, timestamp=ts)
            # Move to front; remove older entry for the same station if present
            existing = [c for c in self._last_conns if c.callsign != callsign]
            maxlen = self._last_conns.maxlen or 4
            self._last_conns = deque([conn] + existing, maxlen=maxlen)
        self._last_activity_ts = time.monotonic()

    async def _on_bulletin(self, payload: dict[str, Any]) -> None:
        area_name = payload.get("area", "").upper()
        if area_name in self._bulletin_areas:
            a = self._bulletin_areas[area_name]
            self._bulletin_areas[area_name] = BulletinArea(
                name=a.name, total=a.total + 1, new=a.new + 1
            )
        else:
            self._bulletin_areas[area_name] = BulletinArea(
                name=area_name, total=1, new=1
            )
        self._last_activity_ts = time.monotonic()

    # ── Render loop ───────────────────────────────────────────────────────────

    async def _render_loop(self) -> None:
        """Background task: render one frame per refresh_interval."""
        logger.info("Display render loop started")
        try:
            while True:
                interval = float(self._settings.get("refresh_interval", "1.0"))
                await asyncio.sleep(interval)

                await self._maybe_refresh_bulletins()
                await self._maybe_refresh_last_conns()
                self._update_idle_state()

                if self._is_off:
                    # Already blanked; nothing to do until woken
                    continue

                frame = self._render_frame()
                if frame is not None:
                    fb        = self._settings["fb_device"]
                    dim       = (float(self._settings["dim_level"]) / 100.0
                                 if self._is_dimmed else 1.0)
                    bl_path   = self._settings["backlight_path"]
                    bl_max    = int(self._settings.get("backlight_max", "255"))
                    write_to_fb(frame, fb, dim_factor=dim,
                                backlight_path=bl_path, backlight_max=bl_max)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Display render loop crashed")

    def _update_idle_state(self) -> None:
        idle_s = time.monotonic() - self._last_activity_ts

        dim_min = float(self._settings.get("idle_dim_minutes", "5"))
        off_min = float(self._settings.get("idle_off_minutes", "30"))

        was_off = self._is_off

        # Screen off
        if off_min > 0 and idle_s >= off_min * 60:
            if not self._is_off:
                self._is_off    = True
                self._is_dimmed = True
                fb = self._settings["fb_device"]
                # Also kill backlight if configured
                bl_path = self._settings["backlight_path"]
                bl_max  = int(self._settings.get("backlight_max", "255"))
                write_to_fb(
                    _black_image(
                        int(self._settings["width"]),
                        int(self._settings["height"]),
                    ),
                    fb, dim_factor=0.0,
                    backlight_path=bl_path, backlight_max=bl_max,
                )
            return

        # Dim
        if dim_min > 0 and idle_s >= dim_min * 60:
            self._is_dimmed = True
        else:
            self._is_dimmed = False

        # Wake from off state
        if was_off:
            self._is_off = False

    def _render_frame(self) -> Optional[Any]:
        """Build renderer lazily and draw one frame."""
        if self._renderer is None:
            self._renderer = Renderer(
                width=int(self._settings["width"]),
                height=int(self._settings["height"]),
                font_path=self._settings.get("font_path", ""),
            )

        # Gather uptime from /proc/uptime (Linux)
        uptime = _read_uptime()

        # Active sessions shown first (in red), then recent disconnects
        active = list(self._active_sessions.values())
        combined_conns = (active + list(self._last_conns))[:4]

        state = DisplayState(
            bbs_callsign=self._cfg.get("callsign", "BBS"),
            uptime_seconds=uptime,
            last_conns=combined_conns,
            bulletins=sorted(
                self._bulletin_areas.values(), key=lambda a: a.name
            ),
            heard_scroll=list(self._heard_scroll),
        )
        return self._renderer.draw_frame(state)

    # ── Database helpers ──────────────────────────────────────────────────────

    async def _ensure_schema(self) -> None:
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def _load_db_settings(self) -> None:
        """Override in-memory settings with any values stored in the DB."""
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            try:
                async with db.execute(
                    "SELECT key, value FROM display_settings"
                ) as cur:
                    rows = await cur.fetchall()
                for key, value in rows:
                    if key in _DEFAULTS:
                        self._settings[key] = value
            except Exception:
                pass  # table may not exist yet on first run

    async def _save_settings_to_db(self) -> None:
        async with aiosqlite.connect(self._db_path, timeout=30) as db:
            for k, v in self._settings.items():
                await db.execute(
                    "INSERT OR REPLACE INTO display_settings (key, value) VALUES (?,?)",
                    (k, v),
                )
            await db.commit()

    async def _preload_last_conns(self) -> None:
        """Populate last-connections from the connection journal on startup."""
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT callsign, transport, last_seen
                    FROM   connection_log
                    WHERE  callsign != ''
                    ORDER  BY last_seen DESC
                    LIMIT  4
                    """
                ) as cur:
                    rows = await cur.fetchall()
                for row in rows:
                    self._last_conns.append(LastConn(
                        callsign=row["callsign"],
                        transport=row["transport"],
                        timestamp=float(row["last_seen"]),
                    ))
        except Exception as exc:
            logger.debug("Could not preload last connections: %s", exc)

    async def _refresh_bulletin_counts(self) -> None:
        """Query total + new message counts for all bulletin areas."""
        hours = float(self._settings.get("bulletin_new_hours", "24"))
        cutoff = time.time() - hours * 3600
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT
                        a.name,
                        COUNT(m.id)                                        AS total,
                        SUM(CASE WHEN m.created_at > ? THEN 1 ELSE 0 END) AS new_count
                    FROM   bulletin_areas   a
                    LEFT JOIN bulletin_messages m
                           ON m.area_id = a.id AND m.deleted = 0
                    GROUP  BY a.id, a.name
                    ORDER  BY a.name
                    """,
                    (cutoff,),
                ) as cur:
                    rows = await cur.fetchall()
                self._bulletin_areas = {
                    row["name"].upper(): BulletinArea(
                        name=row["name"].upper(),
                        total=int(row["total"] or 0),
                        new=int(row["new_count"] or 0),
                    )
                    for row in rows
                }
        except Exception as exc:
            logger.debug("Could not refresh bulletin counts: %s", exc)
        self._last_bulletin_refresh = time.monotonic()

    async def _maybe_refresh_bulletins(self) -> None:
        """Re-query bulletin counts every 60 seconds."""
        if time.monotonic() - self._last_bulletin_refresh > 60:
            await self._refresh_bulletin_counts()

    async def _maybe_refresh_last_conns(self) -> None:
        """Re-sync last-connections from the DB every 60 seconds.

        This keeps the display accurate if sessions disconnect while the
        plugin isn't running, or if connections happened before the plugin
        subscribed to events.
        """
        if time.monotonic() - self._last_conns_refresh <= 60:
            return
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT callsign, transport, last_seen
                    FROM   connection_log
                    WHERE  callsign != ''
                    ORDER  BY last_seen DESC
                    LIMIT  4
                    """
                ) as cur:
                    rows = await cur.fetchall()
            # Rebuild _last_conns, but skip any callsign that is still live in
            # _active_sessions (those are shown separately at render time).
            active_calls = {lc.callsign for lc in self._active_sessions.values()}
            self._last_conns.clear()
            for row in rows:
                if row["callsign"] not in active_calls:
                    self._last_conns.append(LastConn(
                        callsign=row["callsign"],
                        transport=row["transport"],
                        timestamp=float(row["last_seen"]),
                    ))
        except Exception as exc:
            logger.debug("Could not refresh last connections: %s", exc)
        self._last_conns_refresh = time.monotonic()

    # ── Web API helpers (called from Flask thread via run_coroutine_threadsafe) ─

    async def async_save_settings(self, new_settings: dict[str, str]) -> None:
        """Update settings in memory and persist to DB.  Must run in event loop."""
        self.update_settings(new_settings)
        await self._save_settings_to_db()

    def wake(self) -> None:
        """Reset the idle timer (wakes from dim/off).  Thread-safe."""
        self._last_activity_ts = time.monotonic()
        self._is_dimmed = False
        self._is_off    = False


# ── Module-level helpers ──────────────────────────────────────────────────────

def _read_uptime() -> float:
    """Read system uptime from /proc/uptime (Linux).  Returns 0.0 on error."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _black_image(width: int, height: int) -> Any:
    """Return a solid-black PIL Image, or None if Pillow unavailable."""
    try:
        from PIL import Image
        return Image.new("RGB", (width, height), (0, 0, 0))
    except ImportError:
        return None


_HEX_ESCAPE_RE = __import__("re").compile(r"<0x[0-9a-fA-F]{2}>")


def _clean_info(text: str) -> str:
    """
    Sanitise an AX.25 information field for single-line display.

    Strips AGWPE hex-escape sequences (``<0x0d>``), control characters,
    and leading/trailing whitespace.
    """
    text = _HEX_ESCAPE_RE.sub("", text)
    return "".join(c for c in text if c >= " ").strip()
