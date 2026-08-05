# N4a — Gateway safety: ACL, INTERLOCK, rate limits, circuit caps

Status: **IMPLEMENTED (873 tests green); INTERLOCK live-validated 2026-08-04.**
On radiostation2, a user arriving via the K6FB-5 crosslink was refused when the
`C` next-hop was K6FB-5 ("Interlock: won't route back the way you came" — with
NO `originate_circuit`, i.e. the guard short-circuited before touching the wire),
and allowed when the next-hop was a different neighbor (N6ZX-5). The other gates
(ACL deny/allow, rate limit, caps) are offline-tested and default-active; not yet
individually exercised on the air. Compaction-proof capture. Roadmap milestone
**N4** (first slice;
`~/.claude/plans/netrom-node-w6ela5.md`). Branch: `netrom-node`. Builds on N1
(`connect_out`), N2 (`NetromNode`), N3 (node identity + BBS-as-app). Companion
specs: `CONNECT_OUT_SPEC.md`, `ADJACENCY_SPEC.md`, `NODE_IDENTITY_SPEC.md`.

## Goal
N3 made `W6ELA-5` a first-class, publicly-reachable node with an **outbound
gateway** (`C <node>` routes a user onward; a NET/ROM user can arrive via a
neighbor crosslink and be switched back out). Today that gateway is protected by
exactly one check — "is the caller identified" (`may_connect`) — plus a
per-session circuit budget and the local-loop guard. That is not enough for an
open node on a shared RF network. N4a adds the four classic node protections:

1. **ACL** — allow/deny `C` by callsign and auth level (not just "identified").
2. **INTERLOCK** — never route a circuit back out the link it arrived on (the
   canonical NET/ROM loop guard; also blocks trivial relay abuse).
3. **Rate limits** — cap connect attempts per caller per minute.
4. **Circuit caps** — per-user AND node-wide ceilings on gateway circuits.

Guiding principle: **fail closed with a clear reason**, and keep every listing
verb (`N`/`R`/`MH`/`I`/`U`/`P`) open — only `C <onward>` is gated. Local
applications (`C BBS` / `C <svc>`) are NOT gateway connects and keep their own
auth (the BBS re-identifies; a service has its `min_auth`); they bypass the
gateway ACL but still count toward node-wide load where noted.

## What exists today (the starting point)
- `NetromNode.cmd_connect`: local-app resolution (N3) → `may_connect` gate →
  local-loop guard (refuse `C` to our own node) → per-session cap
  (`self._active_gateways >= self.max_gateway_circuits`) → resolve → connect.
- `_active_gateways` is a **per-NetromNode (per-session) counter** — it is
  effectively a per-user-session cap, NOT node-wide (the N2 comment overstates
  it). One user opening two sessions gets two independent budgets.
- `may_connect` is a single bool the plugin sets to `session.auth.is_identified`
  (`@` entry) or `True` (native landing, N3).
- `Connection` carries `remote_addr` (user), `local_addr` (dialed SSID),
  `hop_count`, `transport_id` — but **no arrival-neighbor** (the crosslink
  `via_node` that carried an inbound NET/ROM circuit). INTERLOCK needs it.

## The shared authority: a `GatewayGuard`
Node-wide state (per-caller rate windows, node-wide circuit count, per-user
counts across sessions) cannot live on a per-session `NetromNode`. Introduce one
**`GatewayGuard`** owned by the node plugin (a singleton, bound once) and
injected into every `NetromNode`. It centralizes policy + node-wide counters so
each session consults the same authority — same shape as N0.5 making the router
the single adjacency authority.

```
class GatewayGuard:                       # bbs/netrom/gateway.py
    def __init__(self, cfg: GatewayPolicy) -> None: ...

    # Returns None if the connect is allowed, else a short refusal reason
    # (shown to the user).  Pure check — no side effects.
    def check(self, *, user_call: str, auth_level: int,
              dest_call: str, neighbor: str, arrival_via: str) -> str | None

    # Circuit accounting (node-wide + per-user).  acquire() returns False if a
    # cap is hit; the node then refuses.  Always release() in a finally.
    def acquire(self, user_call: str) -> bool
    def release(self, user_call: str) -> None

    # Introspection for `U` (users) / the future dashboard.
    @property
    def active_total(self) -> int
    def active_for(self, user_call: str) -> int
```

`check()` runs the ACL + rate-limit + INTERLOCK gates (below) in order and
returns the first failure's reason. `acquire()/release()` replace the node's
local `_active_gateways` bump with node-wide + per-user accounting; the node
keeps a tiny local mirror only for the `U` listing if convenient.

## 1. ACL — allow/deny by callsign + auth level
Config under `netrom.gateway`. Checks, in order, against the caller's **base
callsign** and **full callsign** (SSID-insensitive match like the service
dispatcher: `caller` and `caller.split("-")[0]`):

1. **deny** wins first — `deny: [NOCALL, N0CALL, …]` (reuse the ax25d `lockout`
   idiom). Base-or-full match ⇒ refuse.
2. **allow** — if a non-empty `allow:` list is configured, the caller MUST be on
   it (base-or-full); otherwise refuse. Empty/unset `allow` ⇒ allowlist not
   enforced (open to all who pass the other gates). This is the "closed node"
   switch.
3. **min_auth** — `min_auth: identified|authenticated|sysop` (default
   `identified`, preserving N2/N3). Compares `auth_level` to the threshold.
   A native-landing RF user is `IDENTIFIED` (callsign from AX.25). Reaching
   AUTHENTICATED/SYSOP over a native landing needs the node to expose `A` (OTP)
   — out of scope for N4a; document that `min_auth > identified` effectively
   closes `C` to native-landing users until an in-node `A` verb lands (N4
   follow-up), mirroring the services `min_auth` limitation.

ACL is evaluated for **`C <onward node>` only** — local apps and listing verbs
skip it.

## 2. INTERLOCK — don't route back out the arrival link
The loop guard. A NET/ROM user who reached us over a crosslink from neighbor `X`
must not be switched to a destination whose **next-hop neighbor is also `X`** —
that ping-pongs the circuit back to where it came from (loop / relay abuse).

Plumbing (the missing piece): thread the arrival neighbor to the node.
- Add `Connection.netrom_via: str = ""` — the crosslink `via_node` that carried
  an inbound NET/ROM circuit; empty for a direct RF user or non-NET/ROM path.
- Set it in `circuit.py::_handle_connect_request` when building the per-user
  `Connection` (`netrom_via = self._via_node`).
- The node plugin passes `conn.netrom_via` into the `NetromNode` (new
  `arrival_via` ctor arg); `cmd_connect` hands it to `guard.check(...)`.

Rule (in `check()`): if `interlock` is on and `arrival_via` is non-empty and
`neighbor.upper() == arrival_via.upper()`, refuse — "Interlock: won't route back
the way you came." Direct RF users (`arrival_via == ""`) are exempt (nothing to
loop back to). N4a scope is **next-hop == arrival neighbor**; refusing when the
*destination itself* is the arrival neighbor's node, and true per-port INTERLOCK
across multiple transports, are N4d/multi-transport concerns.

## 3. Rate limits — connects per caller per minute
`rate_limit_per_min` (default e.g. 6; 0 = disabled). A sliding-window count of
**accepted** `C <onward>` attempts per **base callsign**, kept node-wide in the
guard (a `dict[str, deque[float]]`, pruned to the last 60 s on each check). Over
the limit ⇒ refuse "Rate limit — try again shortly." Node-wide (not per-session)
so a caller can't reset the window by reconnecting. Local apps don't count.

## 4. Circuit caps — per-user + node-wide
- `max_circuits_per_user` (default 2) — a single caller's concurrent gateway
  circuits, counted node-wide by base callsign (subsumes the per-session
  `_active_gateways`).
- `max_circuits` (node-wide total, default = existing `max_gateway_circuits`,
  4) — the whole node's gateway budget. Each gateway = an inbound circuit + an
  outbound circuit, so budget ≈ 2× expected concurrent gateway users.

`acquire(user_call)` bumps both counters iff neither ceiling is hit (else
returns False → node refuses "Node busy — too many circuits."); `release` decrements.
Wrap the existing `try/finally` around `_bridge` so a crash always releases.

## cmd_connect order (updated)
```
target = …
if not target: usage; return
app = self._apps.get(target.upper())          # N3 local app — no gateway ACL
if app: run local app; return
# local-loop guard (N2): refuse C to our own node call/alias
route = router.get_route(target); if None: "Unknown node"; return
dest_call = route.dest_call
if dest_call == self.node_call: "That's this node."; return
neighbor = router.best_neighbor_for(target, min_quality=…)
if neighbor is None: "No route"; return
# ── N4a gateway gates (single authority) ──
reason = self.guard.check(user_call=self.user_call, auth_level=self.auth_level,
                          dest_call=dest_call, neighbor=neighbor,
                          arrival_via=self.arrival_via)
if reason: term(reason); return
if not self.guard.acquire(self.user_call): term("Node busy…"); return
try:
    … connect_netrom → originate_circuit → _bridge …
finally:
    self.guard.release(self.user_call)
```
`may_connect` collapses into the guard's ACL (`min_auth`); keep the ctor arg for
back-compat but derive it from `auth_level` vs `min_auth` inside the guard.

## Config keys (under `netrom.gateway`)
```yaml
netrom:
  gateway:
    min_auth: identified          # none|identified|authenticated|sysop
    allow: []                     # non-empty ⇒ only these callsigns may C out
    deny: [NOCALL, N0CALL]        # always refuse (base-or-full match)
    interlock: true               # refuse routing back out the arrival link
    rate_limit_per_min: 6         # 0 = disabled
    max_circuits_per_user: 2
    max_circuits: 4               # node-wide (was netrom.max_gateway_circuits)
```
Back-compat: read legacy `netrom.connect_min_quality` /
`netrom.max_gateway_circuits` / `netrom.connect_timeout` as today; the new block
supersedes where both are present. Document in `config/bbs.yaml.example`.

## Edge cases
- **Direct RF user** (`arrival_via` empty) — INTERLOCK exempt; ACL + rate + caps
  still apply.
- **`allow` set + caller absent** — refuse even if identified (closed node).
- **deny vs allow overlap** — deny wins (checked first).
- **min_auth > identified on a native landing** — no in-node OTP yet ⇒ refuse
  with a clear "authentication required (connect to the BBS to identify)"; note
  the follow-up.
- **Reuse of an existing crosslink** (N1 coalesce) still counts as one gateway
  circuit for the acquiring user (accounting is on circuits, not crosslinks).
- **Cap hit mid-`try`** — `acquire` returned False before connect, so nothing to
  release; the finally only runs after a successful acquire (structure the code
  so release pairs with a True acquire).
- **Guard disabled / not wired** (no `netrom.gateway` block) — default policy =
  today's behavior (identified, no allow/deny, interlock ON by default is a
  safe default but flag it; rate limit off, caps = legacy values).
- **Local app + node-wide load** — `C BBS`/`C <svc>` bypass the gateway ACL but
  MAY be counted toward a separate app-load cap later (N4 polish); N4a leaves
  local apps uncapped beyond the existing service `max_sessions`.

## Tests (offline)
- ACL truth table: deny (base + full), allow-list enforced/empty, min_auth
  below/at/above; listing verbs + local apps bypass ACL.
- INTERLOCK: `arrival_via == neighbor` refused; different neighbor allowed;
  direct user (empty `arrival_via`) exempt; `Connection.netrom_via` set from the
  circuit's `via_node` (circuit test).
- Rate limit: N accepted within the window, N+1 refused; window slides;
  node-wide (two sessions, same caller, share the window).
- Caps: per-user ceiling refuses the 3rd concurrent; node-wide ceiling refuses
  across users; release on bridge exit frees budget; crash path releases.
- `GatewayGuard.check` returns the FIRST failing reason (ordering: deny → allow
  → min_auth → rate → interlock → caps via acquire).
- Regression: existing node/agwpe/circuit suites stay green; `may_connect`
  back-compat path still refuses an unidentified user.

## Live validation (radiostation2)
1. `deny: [<a test call>]` → that call's `C` refused with reason; others work.
2. `allow: [KF6ANX]` → only JOHN can `C`; a second known node user refused.
3. INTERLOCK: arrive via K6FB-5, `C` a destination whose next-hop is K6FB-5 →
   refused; `C` a destination via a different neighbor → works. Watch Direwolf:
   no back-out SABM to the arrival neighbor on the refused attempt.
4. Rate limit: rapid repeated `C` from one caller → refused after the Nth.
5. Caps: open `max_circuits_per_user`+1 gateways from one user → last refused;
   node-wide ceiling across two users.

## Files (anticipated)
- **New `bbs/netrom/gateway.py`** — `GatewayPolicy` (parsed config) +
  `GatewayGuard` (ACL/INTERLOCK/rate/caps + accounting).
- `bbs/netrom/node.py` — consult the guard in `cmd_connect`; `arrival_via` ctor
  arg; drop the local `_active_gateways` cap in favor of `guard.acquire/release`
  (keep a mirror for `U` if handy).
- `bbs/plugins/node/node.py` — build/hold the `GatewayGuard` (shared across
  sessions); pass it + `conn.netrom_via` through `_make_node` / `run_native` /
  `handle_session`.
- `bbs/transport/base.py` — `Connection.netrom_via: str = ""`.
- `bbs/netrom/circuit.py` — set `netrom_via = self._via_node` on the per-user
  `Connection` in `_handle_connect_request`.
- `bbs/core/engine.py` — parse `netrom.gateway`; construct the guard; wire it
  into the node bind; pass `conn.netrom_via` on the native landing.
- `bbs/config.py` + `config/bbs.yaml.example` — `netrom.gateway` block + docs.
- Tests: `test_netrom_gateway.py` (guard), plus node / circuit / engine / config.

## Out of scope for N4a (→ later N4 slices)
- Unified application/SSID map in config + web UI (N4b).
- Web node dashboard (N4c).
- Telnet-out / AXUDP + multi-transport next-hop and true per-PORT INTERLOCK
  across transports (N4d).
- In-node `A` (OTP) verb to raise a native-landing user above IDENTIFIED.
- Per-destination ACL rules and time-of-day restrictions (BPQ-style) — N4 polish.
