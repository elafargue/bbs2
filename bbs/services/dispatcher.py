"""
bbs/services/dispatcher.py — ax25d-style external-service routing.

Reproduces the job of the Linux ``ax25d`` daemon in userspace (no kernel
AX.25 needed): route an inbound connection to an external program based on
the *called* callsign-SSID (AX.25) or NET/ROM destination, with the
connection wired to the program's stdin/stdout (see ``bbs.services.bridge``).

Each connection is classified into one outcome:
  • EXEC   — a route matched; run the configured program.
  • REFUSE — a route matched but access is denied (lockout / arrived via a
             digipeater when the route forbids it / caller auth below the
             route's ``min_auth``).
  • PASS   — no route for this called address; hand off to the internal BBS.

Config (bbs.yaml ``services:`` block)::

    services:
      enabled: true
      lockout: [NOCALL, N0CALL]      # ax25d 'L' idiom — always refuse these
      max_sessions: 10               # concurrent external-session cap
      routes:
        "W6ELA-2":
          exec: /usr/bin/xfbbd       # absolute path required
          args: ["xfbbd", "%U"]      # argv incl. argv[0]; %-substituted
          min_auth: identified       # none | identified   (v1)
          no_digi: false             # ax25d 'D' — refuse if via a digipeater
          quiet: false               # ax25d 'Q' — suppress connection logging
          crlf: false                # false = raw passthrough (ax25d default)
          idle_timeout: 600          # reap bridge after N s idle (0 = off)

argv ``%``-substitution (ax25d-compatible):
  ``%u``/``%U``  caller callsign without SSID (lower/upper)
  ``%s``/``%S``  caller callsign with SSID (lower/upper)
  ``%d``         transport/port label
  ``%%``         literal ``%``
(``%p``/``%P``/``%r``/``%R`` NET/ROM node tokens are accepted for
compatibility but expand to empty strings in v1.)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from bbs.core.auth import AuthLevel
from bbs.transport.base import Connection

logger = logging.getLogger(__name__)

# AX.25 callsign with optional SSID, e.g. "W6ELA" or "W6ELA-1".
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,6}(-\d{1,2})?$")
# Tokens we substitute in a route's argv.
_SUBST_RE = re.compile(r"%[uUsSdpPrR%]")

_MIN_AUTH_NAMES = {
    "none":          AuthLevel.ANONYMOUS,
    "anonymous":     AuthLevel.ANONYMOUS,
    "identified":    AuthLevel.IDENTIFIED,
    "authenticated": AuthLevel.AUTHENTICATED,
    "sysop":         AuthLevel.SYSOP,
}


class ServiceAction(Enum):
    PASS = "pass"      # no route — fall through to the internal BBS
    EXEC = "exec"      # matched — run the external program
    REFUSE = "refuse"  # matched but denied — close the connection


@dataclass
class ServiceRoute:
    """One called-address → external-program mapping."""
    called:       str            # called callsign-SSID / NET/ROM dest (UPPER)
    exec_path:    str            # absolute path of the program
    args:         list[str]      # argv template (incl. argv[0]); %-substituted
    min_auth:     AuthLevel = AuthLevel.IDENTIFIED
    no_digi:      bool = False
    quiet:        bool = False
    crlf:         bool = False
    idle_timeout: int = 0
    env:          dict = field(default_factory=dict)  # extra/override env vars


@dataclass
class ServiceDecision:
    action: ServiceAction
    route:  Optional[ServiceRoute] = None
    reason: str = ""


class ServiceDispatcher:
    """Holds the parsed service table and classifies inbound connections."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self._routes: dict[str, ServiceRoute] = {}
        self._lockout: set[str] = set()
        self._enabled = False
        self.max_sessions = 10
        self.reload(cfg or {})

    # ── Config ────────────────────────────────────────────────────────────────

    def reload(self, cfg: dict[str, Any]) -> None:
        """(Re)build the route table from a ``services`` config dict."""
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", bool(cfg.get("routes"))))
        self.max_sessions = int(cfg.get("max_sessions", 10))
        self._lockout = {
            str(c).upper().strip() for c in (cfg.get("lockout") or []) if c
        }
        routes: dict[str, ServiceRoute] = {}
        for called, spec in (cfg.get("routes") or {}).items():
            route = self._parse_route(str(called), spec or {})
            if route is not None:
                routes[route.called] = route
        self._routes = routes
        logger.info(
            "services: %s — %d route(s), %d lockout entr(ies), max_sessions=%d",
            "enabled" if self._enabled else "disabled",
            len(self._routes), len(self._lockout), self.max_sessions,
        )

    def _parse_route(self, called: str, spec: dict[str, Any]) -> Optional[ServiceRoute]:
        called = called.upper().strip()
        exec_path = str(spec.get("exec", "")).strip()
        if not exec_path:
            logger.error("services: route %s has no 'exec' — skipped", called)
            return None
        if not os.path.isabs(exec_path):
            logger.error(
                "services: route %s exec %r is not an absolute path — skipped",
                called, exec_path,
            )
            return None
        args = spec.get("args")
        if args:
            args = [str(a) for a in args]
        else:
            # ax25d convention: argv[0] is explicit.  Default it to the basename.
            args = [os.path.basename(exec_path)]
        min_auth_name = str(spec.get("min_auth", "identified")).lower().strip()
        min_auth = _MIN_AUTH_NAMES.get(min_auth_name)
        if min_auth is None:
            logger.warning(
                "services: route %s unknown min_auth %r — defaulting to identified",
                called, min_auth_name,
            )
            min_auth = AuthLevel.IDENTIFIED
        if min_auth > AuthLevel.IDENTIFIED:
            logger.warning(
                "services: route %s min_auth=%s needs OTP, not supported for "
                "external services yet — connections will be REFUSED until "
                "min_auth is lowered to 'identified'.",
                called, min_auth_name,
            )
        raw_env = spec.get("env") or {}
        env = (
            {str(k): str(v) for k, v in raw_env.items()}
            if isinstance(raw_env, dict) else {}
        )
        return ServiceRoute(
            called       = called,
            exec_path    = exec_path,
            args         = args,
            min_auth     = min_auth,
            no_digi      = bool(spec.get("no_digi", False)),
            quiet        = bool(spec.get("quiet", False)),
            crlf         = bool(spec.get("crlf", False)),
            idle_timeout = int(spec.get("idle_timeout", 0)),
            env          = env,
        )

    # ── Accessors ───────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._routes)

    def route_callsigns(self) -> list[str]:
        """Called callsign-SSIDs to register/accept so callers can reach their
        mapped programs.  (NET/ROM destinations that aren't real AX.25 SSIDs
        are harmless to include — transports ignore ones they can't register.)"""
        return list(self._routes.keys())

    def routes(self) -> list[ServiceRoute]:
        """All configured service routes — used by the NET/ROM node to expose
        each service as a local application (``C <svc>``)."""
        return list(self._routes.values())

    # ── Classification ────────────────────────────────────────────────────────

    def match(self, conn: Connection) -> ServiceDecision:
        """Classify *conn* into PASS / EXEC / REFUSE."""
        if not self.enabled:
            return ServiceDecision(ServiceAction.PASS)
        called = (conn.local_addr or "").upper().strip()
        route = self._routes.get(called)
        if route is None:
            return ServiceDecision(ServiceAction.PASS)

        caller = (conn.remote_addr or "").upper().strip()
        base = caller.split("-", 1)[0]
        # A called-SSID route requires a syntactically valid caller (radio
        # transports always supply one; this guards against junk).
        if not _CALLSIGN_RE.match(caller):
            return ServiceDecision(
                ServiceAction.REFUSE, route, f"invalid caller callsign {caller!r}"
            )
        if caller in self._lockout or base in self._lockout:
            return ServiceDecision(ServiceAction.REFUSE, route, "locked out")
        if route.no_digi and conn.hop_count > 0:
            return ServiceDecision(ServiceAction.REFUSE, route, "arrived via digipeater")
        # Radio callers are trusted as IDENTIFIED (callsign from the AX.25
        # header).  We cannot reach AUTHENTICATED/SYSOP before bridging, so
        # routes demanding those are refused (see _parse_route warning).
        caller_level = AuthLevel.IDENTIFIED
        if caller_level < route.min_auth:
            return ServiceDecision(ServiceAction.REFUSE, route, "auth level too low")
        return ServiceDecision(ServiceAction.EXEC, route)

    # ── argv substitution ─────────────────────────────────────────────────────

    def build_argv(self, route: ServiceRoute, conn: Connection, port: str = "") -> list[str]:
        """Expand a route's argv template with ax25d ``%`` tokens.

        The caller callsign is inserted as a discrete argv element (never a
        shell string), so there is no injection surface.
        """
        caller = (conn.remote_addr or "").upper().strip()
        base = caller.split("-", 1)[0]
        label = port or conn.transport_id
        subst = {
            "%u": base.lower(),   "%U": base.upper(),
            "%s": caller.lower(), "%S": caller.upper(),
            "%d": label,
            "%p": "", "%P": "", "%r": "", "%R": "",
            "%%": "%",
        }
        return [_SUBST_RE.sub(lambda m: subst.get(m.group(0), m.group(0)), a)
                for a in route.args]
