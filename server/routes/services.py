"""
server/routes/services.py — external-service (ax25d) config REST API.

All endpoints require sysop login.

GET /api/services  — current services config (the bbs.yaml `services:` block)
PUT /api/services  — replace it; validates, persists to bbs.yaml, and
                     hot-reloads the dispatcher.
                     Response: {"ok": true, "restart_required": bool}
                     (restart_required = a new service SSID was added and needs
                      radio registration on the next reconnect/restart)
"""
from __future__ import annotations

import os

from flask import jsonify, request, session

from bbs.config import update_yaml_setting
from server.app import app

_VALID_MIN_AUTH = {"none", "identified", "authenticated", "sysop"}


def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _validate(data):
    """Normalize and validate a services config. Returns (dict, error|None)."""
    if not isinstance(data, dict):
        return None, "services must be an object"
    raw_ms = data.get("max_sessions", 10)
    if raw_ms in (None, ""):
        raw_ms = 10
    try:
        max_sessions = int(raw_ms)
    except (TypeError, ValueError):
        return None, "max_sessions must be an integer"
    if max_sessions < 1:
        return None, "max_sessions must be >= 1"

    lockout = data.get("lockout", [])
    if not isinstance(lockout, list):
        return None, "lockout must be a list"

    routes_in = data.get("routes", {})
    if not isinstance(routes_in, dict):
        return None, "routes must be an object"

    routes_out: dict = {}
    for called, spec in routes_in.items():
        called = str(called).upper().strip()
        if not called:
            return None, "route callsign cannot be empty"
        if not isinstance(spec, dict):
            return None, f"route {called} must be an object"
        exec_path = str(spec.get("exec", "")).strip()
        if not exec_path:
            return None, f"route {called}: exec is required"
        if not os.path.isabs(exec_path):
            return None, f"route {called}: exec must be an absolute path"
        args = spec.get("args") or []
        if not isinstance(args, list):
            return None, f"route {called}: args must be a list"
        min_auth = str(spec.get("min_auth", "identified")).lower().strip()
        if min_auth not in _VALID_MIN_AUTH:
            return None, f"route {called}: invalid min_auth {min_auth!r}"
        try:
            idle_timeout = int(spec.get("idle_timeout", 0) or 0)
        except (TypeError, ValueError):
            return None, f"route {called}: idle_timeout must be an integer"
        raw_env = spec.get("env") or {}
        if raw_env and not isinstance(raw_env, dict):
            return None, f"route {called}: env must be an object"
        env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
        route_out = {
            "exec": exec_path,
            "args": [str(a) for a in args],
            "min_auth": min_auth,
            "no_digi": bool(spec.get("no_digi", False)),
            "quiet": bool(spec.get("quiet", False)),
            "crlf": bool(spec.get("crlf", False)),
            "idle_timeout": idle_timeout,
        }
        if env:
            route_out["env"] = env
        routes_out[called] = route_out

    return {
        "enabled": bool(data.get("enabled", False)),
        "max_sessions": max_sessions,
        "lockout": [str(c).upper().strip() for c in lockout if str(c).strip()],
        "routes": routes_out,
    }, None


def _reserved_ssids(cfg) -> list:
    """SSIDs already claimed by the BBS and the NET/ROM node — read-only, so the
    admin sees what is 'taken' and does not add a service route that would shadow
    (or be shadowed by) them.  Derived from config; managed in bbs/netrom, not
    here.  When no distinct node SSID is set, the node shares the BBS SSID."""
    netrom = cfg.netrom or {}
    alias = str(netrom.get("alias", "")).strip().upper()
    node_call = cfg.netrom_node_call            # None unless a distinct node SSID
    out: list = []
    if node_call:
        out.append({"ssid": cfg.full_callsign, "role": "BBS", "detail": cfg.name})
        out.append({
            "ssid": node_call, "role": "Node",
            "detail": f"NET/ROM node ({alias})" if alias else "NET/ROM node",
        })
    else:
        detail = f"{cfg.name} + NET/ROM node (@)" if netrom else cfg.name
        out.append({"ssid": cfg.full_callsign, "role": "BBS", "detail": detail})
    return out


@app.route("/api/services", methods=["GET"])
def get_services():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify({"error": "BBS engine not running"}), 503
    return jsonify(bbs_engine.cfg.services or {})


@app.route("/api/services/reserved", methods=["GET"])
def get_reserved_ssids():
    """Read-only list of SSIDs claimed by the BBS / NET/ROM node (see
    :func:`_reserved_ssids`)."""
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify({"error": "BBS engine not running"}), 503
    return jsonify({"reserved": _reserved_ssids(bbs_engine.cfg)})


@app.route("/api/services", methods=["PUT"])
def update_services():
    err = _require_sysop()
    if err:
        return err
    from server.app import bbs_engine
    if bbs_engine is None:
        return jsonify({"error": "BBS engine not running"}), 503

    normalized, verr = _validate(request.get_json(silent=True) or {})
    if verr:
        return jsonify({"error": verr}), 400

    # A service route on the BBS or node SSID would shadow (or be shadowed by)
    # them at dispatch — reject so the admin can't foot-gun the BBS/node away.
    reserved = {r["ssid"].upper() for r in _reserved_ssids(bbs_engine.cfg)}
    clash = sorted(reserved & set(normalized["routes"].keys()))
    if clash:
        return jsonify({"error":
            f"SSID(s) {', '.join(clash)} are reserved for the BBS/node and "
            f"cannot be a service route."}), 400

    old_ssids = {str(k).upper() for k in (bbs_engine.cfg.services or {}).get("routes", {})}
    new_ssids = set(normalized["routes"].keys())

    try:
        update_yaml_setting(bbs_engine.cfg_path, ["services"], normalized)
    except Exception as exc:  # noqa: BLE001 — surface any write error to the UI
        return jsonify({"error": f"failed to write config: {exc}"}), 500

    bbs_engine.cfg.services = normalized
    bbs_engine.reload_services()

    return jsonify({"ok": True, "restart_required": bool(new_ssids - old_ssids)})
