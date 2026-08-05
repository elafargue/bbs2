"""
bbs/netrom/gateway.py — the single gateway-safety authority (N4a).

A live NET/ROM node with an outbound gateway (``C <node>``) is abusable: loops,
relay abuse, connect floods, resource exhaustion.  :class:`GatewayGuard` is the
one authority every node session consults before switching a user onward — it
owns the ACL, the INTERLOCK loop guard, per-caller rate limiting, and the
per-user + node-wide circuit caps, plus the node-wide runtime state those need
(which cannot live on a per-session :class:`~bbs.netrom.node.NetromNode`).

The node plugin builds ONE guard at bind time and injects it into every
NetromNode, so all sessions share the same accounting — the same "single
authority" shape N0.5 gave the router for adjacency.

Design:
  • :meth:`check` is a read-mostly predicate returning the first failing gate's
    user-facing reason (or None to allow).  It performs only lazy pruning of
    expired rate-window timestamps — no accounting mutation.
  • :meth:`acquire` / :meth:`release` do the circuit accounting; ``acquire``
    also records the accepted connect in the rate window.  Pair them in a
    ``try/finally`` around the gateway bridge.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from bbs.core.auth import AuthLevel

_MIN_AUTH_NAMES: dict[str, AuthLevel] = {
    "none":          AuthLevel.ANONYMOUS,
    "anonymous":     AuthLevel.ANONYMOUS,
    "identified":    AuthLevel.IDENTIFIED,
    "authenticated": AuthLevel.AUTHENTICATED,
    "sysop":         AuthLevel.SYSOP,
}


def _norm(call: Any) -> str:
    return str(call).upper().strip()


def _base(call: str) -> str:
    """Base callsign without SSID (accounting + ACL match are SSID-insensitive,
    like the service dispatcher)."""
    return _norm(call).split("-", 1)[0]


@dataclass(frozen=True)
class GatewayPolicy:
    """Parsed ``netrom.gateway`` config (immutable)."""
    min_auth: AuthLevel = AuthLevel.IDENTIFIED
    allow: frozenset[str] = frozenset()          # non-empty ⇒ only these may C out
    deny: frozenset[str] = frozenset()           # always refused
    interlock: bool = True                        # refuse routing back the arrival link
    rate_limit_per_min: int = 6                   # 0 = disabled
    max_circuits_per_user: int = 2
    max_circuits: int = 4                         # node-wide total

    @classmethod
    def permissive(cls, max_circuits: int) -> "GatewayPolicy":
        """A policy with no ACL / INTERLOCK / rate limiting — only a circuit
        budget.  Used when a :class:`~bbs.netrom.node.NetromNode` is built
        without a shared guard (direct use / tests), preserving pre-N4a
        behavior (gating then done solely by the node's ``may_connect``)."""
        n = max(1, int(max_circuits))
        return cls(min_auth=AuthLevel.ANONYMOUS, interlock=False,
                   rate_limit_per_min=0, max_circuits=n, max_circuits_per_user=n)

    @classmethod
    def from_netrom_cfg(cls, netrom_cfg: dict | None) -> "GatewayPolicy":
        """Build from the ``netrom:`` config dict's ``gateway`` sub-block, with
        back-compat for the legacy ``netrom.max_gateway_circuits`` node cap."""
        netrom_cfg = netrom_cfg or {}
        g = netrom_cfg.get("gateway") or {}
        legacy_max = int(netrom_cfg.get("max_gateway_circuits", 4))
        min_auth = _MIN_AUTH_NAMES.get(
            str(g.get("min_auth", "identified")).lower().strip(),
            AuthLevel.IDENTIFIED,
        )
        return cls(
            min_auth=min_auth,
            allow=frozenset(_norm(c) for c in (g.get("allow") or []) if str(c).strip()),
            deny=frozenset(_norm(c) for c in (g.get("deny") or []) if str(c).strip()),
            interlock=bool(g.get("interlock", True)),
            rate_limit_per_min=max(0, int(g.get("rate_limit_per_min", 6))),
            max_circuits_per_user=max(1, int(g.get("max_circuits_per_user", 2))),
            max_circuits=max(1, int(g.get("max_circuits", legacy_max))),
        )


class GatewayGuard:
    """Node-wide gateway policy + accounting.  One per node, shared by sessions."""

    #: Recent gateway refusals kept for the web dashboard (observability).
    _REFUSAL_HISTORY = 50

    def __init__(self, policy: Optional[GatewayPolicy] = None) -> None:
        self.policy = policy or GatewayPolicy()
        self._rate: dict[str, deque[float]] = {}     # base call → accepted-connect ts
        self._active_total: int = 0
        self._active_per_user: dict[str, int] = {}   # base call → live gateway circuits
        self._refusals: deque[dict] = deque(maxlen=self._REFUSAL_HISTORY)

    # ── Gate check (read-mostly) ──────────────────────────────────────────────

    def check(
        self,
        *,
        user_call: str,
        auth_level: Optional[AuthLevel],
        dest_call: str,
        neighbor: str,
        arrival_via: str,
    ) -> Optional[str]:
        """Return None if *user_call* may open a gateway circuit to *dest_call*
        via *neighbor*, else the first failing gate's user-facing reason.

        Gate order: deny → allow → min_auth → rate limit → INTERLOCK.  Circuit
        caps are enforced separately by :meth:`acquire` (they must reserve, not
        just test)."""
        p = self.policy
        full = _norm(user_call)
        base = _base(user_call)

        # 1. deny-list (base or full match) wins first.
        if full in p.deny or base in p.deny:
            return "Access denied."
        # 2. allow-list, when configured, is mandatory (closed node).
        if p.allow and not (full in p.allow or base in p.allow):
            return "Not authorized to use this gateway."
        # 3. minimum auth level.
        level = auth_level if auth_level is not None else AuthLevel.IDENTIFIED
        if level < p.min_auth:
            return f"Authentication required ({p.min_auth.name.lower()})."
        # 4. per-caller connect rate.
        if p.rate_limit_per_min > 0 and self._rate_exceeded(base):
            return "Rate limit — try again shortly."
        # 5. INTERLOCK: never route a circuit back out the link it arrived on.
        if p.interlock and arrival_via and _norm(neighbor) == _norm(arrival_via):
            return "Interlock: won't route back the way you came."
        return None

    def _rate_exceeded(self, base: str) -> bool:
        dq = self._rate.get(base)
        if not dq:
            return False
        cutoff = time.time() - 60.0
        while dq and dq[0] < cutoff:      # lazy prune of the 60 s window
            dq.popleft()
        if not dq:
            self._rate.pop(base, None)
            return False
        return len(dq) >= self.policy.rate_limit_per_min

    # ── Circuit accounting ────────────────────────────────────────────────────

    def acquire(self, user_call: str) -> bool:
        """Reserve a gateway-circuit slot for *user_call* (node-wide + per-user
        caps).  Returns False if either ceiling is hit (caller then refuses).
        On success, records the accepted connect in the rate window."""
        base = _base(user_call)
        if self._active_total >= self.policy.max_circuits:
            return False
        if self._active_per_user.get(base, 0) >= self.policy.max_circuits_per_user:
            return False
        self._active_total += 1
        self._active_per_user[base] = self._active_per_user.get(base, 0) + 1
        if self.policy.rate_limit_per_min > 0:
            self._rate.setdefault(base, deque()).append(time.time())
        return True

    def release(self, user_call: str) -> None:
        """Free a slot previously reserved by :meth:`acquire`."""
        base = _base(user_call)
        if self._active_total > 0:
            self._active_total -= 1
        n = self._active_per_user.get(base, 0)
        if n <= 1:
            self._active_per_user.pop(base, None)
        else:
            self._active_per_user[base] = n - 1

    # ── Introspection (U listing / dashboard) ─────────────────────────────────

    @property
    def active_total(self) -> int:
        return self._active_total

    def active_for(self, user_call: str) -> int:
        return self._active_per_user.get(_base(user_call), 0)

    def note_refusal(self, user_call: str, reason: str, dest: str = "") -> None:
        """Record a refused gateway connect for the dashboard (bounded ring)."""
        self._refusals.append({
            "ts": time.time(),
            "user": _norm(user_call),
            "dest": _norm(dest),
            "reason": reason,
        })

    def stats(self) -> dict:
        """JSON-serializable snapshot of policy + live counters + recent
        refusals, for the web node dashboard.  Safe to call from any thread
        (reads only; refusals ring is copied)."""
        p = self.policy
        return {
            "active": self._active_total,
            "max": p.max_circuits,
            "max_per_user": p.max_circuits_per_user,
            "per_user": dict(self._active_per_user),
            "policy": {
                "min_auth": p.min_auth.name.lower(),
                "interlock": p.interlock,
                "rate_limit_per_min": p.rate_limit_per_min,
                "allow": sorted(p.allow),
                "deny": sorted(p.deny),
            },
            "recent_refusals": list(self._refusals),
        }
