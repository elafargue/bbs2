"""
bbs/plugins/display/renderer.py — PIL-based framebuffer renderer for the
480×320 status display.

The renderer is intentionally decoupled from the plugin so it can be
unit-tested without a physical framebuffer.  Call ``draw_frame()`` to produce
a PIL Image, then pass it to ``write_to_fb()`` to push it to /dev/fb0.

Colour palette (dark ham-radio aesthetic)
------------------------------------------
Background     : (8,  12,  24)    deep navy-black
Header bar     : (16, 32,  72)    dark blue
Callsign       : (80, 220, 255)   bright cyan
Section header : (255, 185, 30)   amber
Body text      : (185, 200, 215)  light grey-blue
Timestamp      : (120, 135, 155)  muted grey
Uptime         : (90,  215,  90)  green
New-count      : (255, 145, 40)   orange
Heard entry    : (240, 240, 190)  warm yellow
Divider        : (30,  55,  100)  dim blue
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Optional heavy deps ───────────────────────────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
    logger.warning("Pillow not installed — display rendering disabled")

try:
    import numpy as np  # type: ignore
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False
    logger.warning("NumPy not installed — framebuffer write will use slow fallback")


# ── Colour constants ──────────────────────────────────────────────────────────

C_BG         = (8,   12,  24)
C_HEADER_BG  = (16,  32,  72)
C_CALLSIGN   = (80,  220, 255)
C_SECTION    = (255, 185,  30)
C_BODY       = (185, 200, 215)
C_DIM_TEXT   = (120, 135, 155)
C_UPTIME     = (90,  215,  90)
C_NEW        = (255, 145,  40)
C_HEARD      = (255, 205,  50)
C_DIVIDER    = (30,   55, 100)
C_WHITE      = (230, 240, 255)
C_ACTIVE     = (255,  70,  70)   # red — active (currently connected) sessions

# Candidate system-font paths, tried in order.
_FONT_CANDIDATES = [
    # Terminus — pixel-perfect bitmap font (apt install fonts-terminus)
    "/usr/share/fonts/truetype/terminus/TerminusTTF.ttf",
    "/usr/share/fonts/truetype/terminus/TerminusTTFB.ttf",
    # DejaVu Mono — standard on Raspberry Pi OS
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    # Liberation Mono — common alternative
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    # Last-ditch macOS path (dev machines)
    "/System/Library/Fonts/Menlo.ttc",
]


# ── Data model passed to draw_frame() ────────────────────────────────────────

@dataclass
class LastConn:
    callsign:  str
    transport: str
    timestamp: float
    active:    bool = False   # True while the session is still connected


@dataclass
class BulletinArea:
    name:  str
    total: int
    new:   int


@dataclass
class DisplayState:
    """All data needed to render one frame."""
    bbs_callsign:   str = ""
    uptime_seconds: float = 0.0
    last_conns:     list[LastConn]    = field(default_factory=list)   # up to 3
    bulletins:      list[BulletinArea] = field(default_factory=list)  # all areas
    heard_scroll:   list            = field(default_factory=list)  # newest first; list[dict]


# ── Renderer ─────────────────────────────────────────────────────────────────

class Renderer:
    """
    Renders a ``DisplayState`` to a PIL Image.

    Parameters
    ----------
    width, height : int
        Screen dimensions in pixels (default 480×320).
    font_path : str
        Path to a TTF/TTC font file.  Empty string triggers auto-detection from
        ``_FONT_CANDIDATES``.
    """

    def __init__(
        self,
        width: int = 480,
        height: int = 320,
        font_path: str = "",
    ) -> None:
        self.width  = width
        self.height = height
        self._fonts: dict[str, any] = {}

        if _PIL_OK:
            self._load_fonts(font_path)

    # ── Font loading ──────────────────────────────────────────────────────────

    def _load_fonts(self, requested_path: str) -> None:
        """Load fonts at several sizes.  Falls back to PIL built-in on failure."""
        path = self._resolve_font(requested_path)

        sizes = {
            "large":  20,   # header callsign
            "medium": 16,   # section headers
            "normal": 13,   # body text
            "small":  11,   # timestamps, secondary info
        }

        for key, size in sizes.items():
            if path:
                try:
                    self._fonts[key] = ImageFont.truetype(path, size)
                    continue
                except Exception:
                    logger.warning("Could not load font %r at size %d", path, size)
            # PIL default fallback (fixed 8×13)
            try:
                self._fonts[key] = ImageFont.load_default()
            except Exception:
                self._fonts[key] = None

    @staticmethod
    def _resolve_font(requested: str) -> str:
        """Return a usable font path or empty string."""
        if requested and os.path.isfile(requested):
            return requested
        for candidate in _FONT_CANDIDATES:
            if os.path.isfile(candidate):
                logger.debug("Using font: %s", candidate)
                return candidate
        return ""

    def _font(self, key: str) -> Optional[any]:
        return self._fonts.get(key)

    # ── Public draw entry-point ───────────────────────────────────────────────

    def draw_frame(self, state: DisplayState) -> Optional["Image.Image"]:  # type: ignore
        """
        Render *state* to a new PIL ``Image``.  Returns ``None`` if Pillow is
        not installed.
        """
        if not _PIL_OK:
            return None

        img  = Image.new("RGB", (self.width, self.height), C_BG)
        draw = ImageDraw.Draw(img)

        self._draw_header(draw, state)
        self._draw_connections(draw, state)
        self._draw_bulletins(draw, state)
        self._draw_heard(draw, state)

        return img

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _draw_header(self, draw: "ImageDraw.ImageDraw", state: DisplayState) -> None:
        """Top bar: callsign | 'BBS STATUS' | uptime."""
        h = 34
        draw.rectangle([(0, 0), (self.width - 1, h - 1)], fill=C_HEADER_BG)

        # Left: BBS callsign
        draw.text((8, 7), state.bbs_callsign, font=self._font("large"), fill=C_CALLSIGN)

        # Centre: label
        label = "BBS STATUS"
        bbox  = draw.textbbox((0, 0), label, font=self._font("medium"))
        lw    = bbox[2] - bbox[0]
        draw.text(((self.width - lw) // 2, 9), label,
                  font=self._font("medium"), fill=C_WHITE)

        # Right: uptime
        up = _fmt_uptime(state.uptime_seconds)
        up_str = f"up {up}"
        bbox2 = draw.textbbox((0, 0), up_str, font=self._font("small"))
        rw = bbox2[2] - bbox2[0]
        draw.text((self.width - rw - 8, 11), up_str,
                  font=self._font("small"), fill=C_UPTIME)

        # Separator line
        draw.line([(0, h), (self.width, h)], fill=C_DIVIDER, width=1)

    def _draw_connections(
        self, draw: "ImageDraw.ImageDraw", state: DisplayState
    ) -> None:
        """Left panel: last 3 connections (y=35 .. y=142, x=0 .. x=249)."""
        panel_w = 249
        y0      = 36
        pad     = 6

        draw.text((pad, y0), "LAST CONNECTIONS",
                  font=self._font("small"), fill=C_SECTION)

        line_h = 20
        y = y0 + 18

        for i in range(4):
            if i < len(state.last_conns):
                c = state.last_conns[i]
                call_str = c.callsign.upper()[:9]
                trn_str  = _abbrev_transport(c.transport)
                if c.active:
                    # Active session: callsign + transport in red, "LIVE" badge
                    draw.text((pad, y + 1),       call_str,
                              font=self._font("normal"), fill=C_ACTIVE)
                    draw.text((pad + 74, y + 2),  trn_str,
                              font=self._font("small"),  fill=C_ACTIVE)
                    draw.text((pad + 160, y + 2), "\u25cf LIVE",
                              font=self._font("small"),  fill=C_ACTIVE)
                else:
                    ts_str = _fmt_ts(c.timestamp)
                    draw.text((pad, y + 1),       call_str,
                              font=self._font("normal"), fill=C_CALLSIGN)
                    draw.text((pad + 74, y + 2),  trn_str,
                              font=self._font("small"),  fill=C_DIM_TEXT)
                    draw.text((pad + 160, y + 2), ts_str,
                              font=self._font("small"),  fill=C_DIM_TEXT)
            else:
                draw.text((pad, y + 3), "—",
                          font=self._font("normal"), fill=C_DIM_TEXT)
            y += line_h

        # Vertical divider
        draw.line([(panel_w, y0), (panel_w, 142)], fill=C_DIVIDER, width=1)

    def _draw_bulletins(
        self, draw: "ImageDraw.ImageDraw", state: DisplayState
    ) -> None:
        """Right panel: bulletin area counts (y=35 .. y=142, x=252 ..)."""
        x0  = 254
        y0  = 36
        pad = 4

        draw.text((x0 + pad, y0), "BULLETINS",
                  font=self._font("small"), fill=C_SECTION)

        line_h = 20
        y = y0 + 18
        max_areas = 4   # show at most 4 areas before clipping

        for area in state.bulletins[:max_areas]:
            name_str = area.name[:8].ljust(8)
            draw.text((x0 + pad, y), name_str,
                      font=self._font("normal"), fill=C_BODY)

            # total / new counts, right-aligned in the panel
            total_str = f"{area.total}"
            if area.new > 0:
                new_str = f"+{area.new}"
                draw.text((x0 + pad + 72, y), total_str,
                          font=self._font("small"), fill=C_DIM_TEXT)
                draw.text((x0 + pad + 96, y), new_str,
                          font=self._font("small"), fill=C_NEW)
            else:
                draw.text((x0 + pad + 72, y), total_str,
                          font=self._font("small"), fill=C_DIM_TEXT)
            y += line_h

        if not state.bulletins:
            draw.text((x0 + pad, y0 + 18), "—", font=self._font("normal"),
                      fill=C_DIM_TEXT)

    def _draw_heard(
        self, draw: "ImageDraw.ImageDraw", state: DisplayState
    ) -> None:
        """Bottom section: scrolling RF heard log (y=143 ..)."""
        y_top    = 143
        line_h   = 15
        pad      = 6
        max_lines = (self.height - y_top - 4) // line_h

        # Section header with decorative line
        header = "RF HEARD"
        draw.line([(0, y_top), (self.width, y_top)], fill=C_DIVIDER, width=1)
        draw.text((pad, y_top + 3), header,
                  font=self._font("small"), fill=C_SECTION)
        hdr_bbox = draw.textbbox((0, 0), header, font=self._font("small"))
        hdr_w    = hdr_bbox[2] - hdr_bbox[0]
        draw.line([(pad + hdr_w + 6, y_top + 8),
                   (self.width - pad, y_top + 8)],
                  fill=C_DIVIDER, width=1)

        y = y_top + 18
        entries = state.heard_scroll[:max_lines]

        if not entries:
            draw.text((pad, y), "(no RF activity yet)",
                      font=self._font("small"), fill=C_DIM_TEXT)
            return

        info_line_h = 12  # height for the smaller payload sub-line
        for entry in entries:
            if y + line_h > self.height - 2:
                break

            # ── Structured dict entry — per-token via coloring ────────────────
            src       = entry.get("src", "?")
            dest      = entry.get("dest", "")
            transport = entry.get("transport", "")
            info      = entry.get("info", "")
            count     = entry.get("count", 1)
            via_list  = entry.get("via_list", [])
            heard_set = entry.get("heard_set", set())

            fnt   = self._font("normal")
            fnt_s = self._font("small")
            x     = pad

            # src>dest  (yellow)
            prefix = src
            if dest and dest not in ("UI", ""):
                prefix += f">{dest}"
            draw.text((x, y), prefix, font=fnt, fill=C_HEARD)
            x += int(draw.textlength(prefix, font=fnt))

            # [transport]  (dim grey)
            if transport:
                abbrev = f" [{_abbrev_transport(transport)}]"
                draw.text((x, y), abbrev, font=fnt_s, fill=C_DIM_TEXT)
                x += int(draw.textlength(abbrev, font=fnt_s))

            # (xN) badge  (warm orange)
            if count > 1:
                badge = f" (x{count})"
                draw.text((x, y), badge, font=fnt_s, fill=C_NEW)
                x += int(draw.textlength(badge, font=fnt_s))

            # via <tokens>  — confirmed relays yellow, others dim grey
            if via_list:
                via_label = " via "
                draw.text((x, y), via_label, font=fnt_s, fill=C_DIM_TEXT)
                x += int(draw.textlength(via_label, font=fnt_s))
                for j, name in enumerate(via_list):
                    if j > 0:
                        sep = ","
                        draw.text((x, y), sep, font=fnt_s, fill=C_DIM_TEXT)
                        x += int(draw.textlength(sep, font=fnt_s))
                    token_color = C_HEARD if j in heard_set else C_DIM_TEXT
                    if x < self.width - pad:
                        draw.text((x, y), name, font=fnt_s, fill=token_color)
                        x += int(draw.textlength(name, font=fnt_s))

            y += line_h

            # info sub-line  (body grey, small font)
            if info and y + info_line_h <= self.height - 2:
                max_i = (self.width - pad * 2 - 8) // 6
                draw.text((pad + 8, y), info[:max_i],
                          font=fnt_s, fill=C_BODY)
                y += info_line_h


# ── Framebuffer writer ────────────────────────────────────────────────────────

def write_to_fb(
    image: "Image.Image",  # type: ignore
    fb_path: str,
    dim_factor: float = 1.0,
    backlight_path: str = "",
    backlight_max: int = 255,
) -> None:
    """
    Write a PIL RGB Image to a Linux RGB565 framebuffer.

    Parameters
    ----------
    image : PIL.Image.Image
        The frame to write (must be RGB mode).
    fb_path : str
        Path to the framebuffer device (e.g. ``/dev/fb0``).
    dim_factor : float
        Brightness multiplier 0.0–1.0.  Applied in software when no
        backlight path is configured.
    backlight_path : str
        Optional sysfs backlight path.  When provided, hardware brightness
        is adjusted instead of (or in addition to) software dimming.
    backlight_max : int
        Maximum value to write to *backlight_path* (default 255).
    """
    if not _PIL_OK:
        return

    # Hardware backlight control
    if backlight_path:
        try:
            brightness = max(0, min(backlight_max, int(backlight_max * dim_factor)))
            with open(backlight_path, "w") as bl:
                bl.write(str(brightness))
        except OSError as exc:
            logger.debug("Backlight write failed: %s", exc)

    # Convert to RGB565 and write
    try:
        raw = image.convert("RGB")

        if _NUMPY_OK:
            arr = np.array(raw, dtype=np.uint16)
            if dim_factor < 1.0:
                arr = (arr * dim_factor).clip(0, 255).astype(np.uint16)
            rgb565 = (
                ((arr[..., 0] >> 3) << 11) |
                ((arr[..., 1] >> 2) <<  5) |
                ( arr[..., 2] >> 3)
            )
            frame_bytes = rgb565.astype("<u2").tobytes()
        else:
            # Pure-Python fallback (slow but correct)
            pixels = list(raw.getdata())
            buf    = bytearray(len(pixels) * 2)
            if dim_factor < 1.0:
                factor = dim_factor
                pixels = [(int(r * factor), int(g * factor), int(b * factor))
                          for r, g, b in pixels]
            import struct
            for i, (r, g, b) in enumerate(pixels):
                val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                struct.pack_into("<H", buf, i * 2, val)
            frame_bytes = bytes(buf)

        with open(fb_path, "wb") as fb:
            fb.write(frame_bytes)

    except OSError as exc:
        logger.debug("Framebuffer write failed: %s", exc)


def blank_fb(fb_path: str) -> None:
    """Fill the framebuffer with zeros (black / display off)."""
    try:
        # Write a full frame of zeros without needing numpy/PIL
        import struct
        # Read the device size (ioctl FBIOGET_VSCREENINFO is complex; use config)
        # Fallback: just write a large block of zeros. The kernel clips at EOF.
        with open(fb_path, "wb") as fb:
            chunk = b"\x00" * 4096
            # 480*320*2 = 307200 bytes
            for _ in range(307200 // 4096):
                fb.write(chunk)
            fb.write(b"\x00" * (307200 % 4096))
    except OSError as exc:
        logger.debug("Framebuffer blank failed: %s", exc)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_uptime(seconds: float) -> str:
    """Format uptime as '3d 5h', '5h 12m', or '12m'."""
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s,  3600)
    m     = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _fmt_ts(ts: float) -> str:
    """Format a Unix timestamp as 'MM-DD HH:MM'."""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _abbrev_transport(tid: str) -> str:
    """Short friendly name for a transport ID."""
    mapping = {
        "kiss_tcp":    "KISS/TCP",
        "kiss_serial": "KISS/SER",
        "agwpe":       "AGWPE",
        "kernel_ax25": "AX.25",
        "netrom":      "NETROM",
        "tcp":         "TCP",
        "web":         "WEB",
    }
    return mapping.get(tid, tid[:14].upper())
