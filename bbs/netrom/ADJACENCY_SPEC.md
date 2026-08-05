# N0.5 — Adjacency consolidation: the router as the single neighbor authority

Status: **IMPLEMENTED offline (800 tests green); pending live validation.** A
*hardening refactor* of N0, before N3. Branch: `netrom-node`. Compaction-proof
capture — kept as the record of what/why.

Companion to `CONNECT_OUT_SPEC.md` (N1 outbound crosslink, N2 node layer). This
spec addresses the one architectural weakness found in the end-to-end review
after N2: **"adjacency" (who can I reach in one AX.25 hop) has no single
authority**, and the resulting disagreement between signals is the root cause of
several bugs and one band-aid.

## Problem — three disagreeing notions of adjacency
Today "is X a directly-reachable NET/ROM neighbor?" is answered three different
ways, in three places, and they disagree:

| Signal | Lives in | Consumed by | Weakness |
|--------|----------|-------------|----------|
| `router.adjacent_neighbors` = the set of route `via_call`s | `router.py` | **inbound** classifier (`engine._is_netrom_neighbor` → `set_netrom_neighbor_check`) | TTL-expires; transit-polluted; incomplete right after a restart |
| `heard_direct_within(call, s)` (RF beacons/IDs) | **heard plugin** | **outbound** `best_neighbor_for` (via `set_direct_heard_check`) | authority lives in an *optional UI plugin*; not consulted inbound |
| auto-added source route / `is_direct` (`via_call == dest_call`, q192) | `router.py` (in-memory) | `best_neighbor_for` direct-preference | **not persisted** — `heard.on_netrom_nodes` saves only advertised (transit) `frame.entries`, so the direct-self route dies on restart |

Observed consequences (all the same underlying fact — "we hear JOHN directly" —
wearing different hats):
- **Outbound** `C JOHN` took a slow multi-hop transit instead of the direct
  ~3 s SABM (only transit routes seeded post-restart). Patched with signal #2.
- **Inbound** JOHN (`KF6ANX-4`) connecting back was **misclassified as a direct
  BBS user** (started a BBS session, would emit a pid=0xF0 banner onto an
  inter-node link) because signal #1 didn't include it. *Not patched* — a fix
  here would be a *third* patch to a *third* signal for the *same* fact.
- The **cold-start late-PID fallback** (`agwpe._promote_to_netrom_crosslink`
  with `mute()`/`suppress_close()`/`set_pid()` gymnastics and banner-leak risk)
  exists *only* because signal #1 is unreliable.

Two structural smells fall out: a **layering inversion** (routing/classification
core depends on the optional heard plugin) and a **persistence mismatch**
(in-memory knows direct neighbors; the DB stores only transit).

## Goal
Make **`NetromRouter` the single authority** for both routing and adjacency,
**enriched** by all available sources, degrading gracefully when a source
(notably the heard plugin) is absent. Collapse ~3 signals + 2 open bugs + 1
band-aid into one coherent module — *reducing* surface area.

Design principles:
1. **One authority, one predicate.** Inbound classification, outbound next-hop,
   and the cold-start path all call the *same* router methods.
2. **Enrichment, not dependency.** Sources *push into* the router; the router
   never reaches up into a plugin. The heard plugin is **optional** — if it's
   disabled the router still works, just with less enrichment.
3. **A live crosslink is proof of adjacency** — the strongest signal, and free.
4. **Known-node gating** for classification: hearing a random ham's beacon
   directly must NOT make them a "NET/ROM crosslink" — only a *known node* that
   is *directly reachable* counts.

## The enriched model (all inside `NetromRouter`)
Keep the existing `_routes: dict[str, list[RouteEntry]]` (the routing table).
Add two small **enrichment maps**, fed by push:

```
self._heard_direct: dict[str, float]   # call → unix ts we last heard it DIRECT (RF)
self._crosslinks:   set[str]           # calls we currently have a live AX.25 crosslink to
self._direct_heard_ttl: float          # from netrom.direct_heard_ttl_minutes (default 60m)
```

These are ephemeral/derived — **no new persistence**. `_heard_direct` is
re-seeded at startup from the heard plugin's existing DB-backed cache (pushed
in, see wiring); `_crosslinks` is live-only; `_routes` persists as today via the
heard DB.

### Enrichment inputs (push API)
```
def note_heard_direct(self, call: str, when: float | None = None) -> None
    # RF direct hearing (no digipeater). Called by the heard plugin on every
    # direct on_heard, and once per seeded station at startup. Optional source.

def note_crosslink(self, call: str, up: bool) -> None
    # A live AX.25 crosslink to `call` opened (up=True) or closed (up=False).
    # Called by the transport on crosslink establish/teardown. Definitive.
```
Plus the existing NODES ingest (`on_netrom_frame` → `_process_nodes`), unchanged.

### The single predicate + next-hop
```
def is_direct_neighbor(self, call: str) -> bool:
    """A directly-reachable NET/ROM node — the ONE adjacency authority."""
    c = call.upper()
    if c in self._crosslinks:                 # a live crosslink is proof
        return True
    if not self._is_known_node(c):            # gate: must be a NET/ROM node
        return False                          #   (a dest in the table / NODES source)
    now = time.time()
    if now - self._heard_direct.get(c, 0) <= self._direct_heard_ttl:
        return True                           # heard on the air recently (RF)
    return self._has_fresh_direct_route(c)    # via_call == dest_call, within TTL

def best_neighbor_for(self, dest, *, min_quality=1) -> str | None:
    routes = self.get_routes(dest)            # resolves alias→callsign
    dest_call = routes[0].dest_call.upper() if routes else dest.upper()
    # 1. Direct to the destination itself if we can reach it in one hop.
    if _is_routable_callsign(dest_call) and self.is_direct_neighbor(dest_call):
        return dest_call
    if not routes:
        return None
    # 2. Otherwise the best transit route whose FIRST HOP we can actually
    #    reach directly (first-hop hardening — avoids a RETRYOUT on a via_call
    #    we only hear through a digipeater).
    reachable = [r for r in routes
                 if r.quality >= min_quality and self.is_direct_neighbor(r.via_call)]
    if reachable:
        return reachable[0].via_call.upper()  # routes are quality-sorted
    # 3. Best-effort fallback: highest-quality route even if we can't confirm
    #    the first hop (may RETRYOUT, but better than refusing a known route).
    best = routes[0]
    return best.via_call.upper() if best.quality >= min_quality else None
```

`adjacent_neighbors` is **redefined** as the coherent set
`{c for c in known-nodes if is_direct_neighbor(c)} ∪ self._crosslinks` — not the
raw `via_call` set. (Kept for compatibility / display; callers that classify
should call `is_direct_neighbor`.)

`_is_known_node(c)`: `c` is a dest key in `_routes` (NODES auto-adds the source
as a dest, so every node we've heard NODES from qualifies), OR `c in
self._crosslinks`. `_has_fresh_direct_route(c)`: `_routes[c]` has an entry with
`via_call == dest_call` and `last_seen` within TTL.

## Wiring changes

### Retire the pull callback; heard pushes instead (dependency inversion)
- **Remove** `NetromRouter.set_direct_heard_check()` and the engine lambda that
  reaches into the heard plugin.
- **Heard plugin** gains a push: it already maintains `_direct_heard` (seeded
  from DB, updated on direct `on_heard`). Add
  `set_direct_heard_observer(cb)`; call `cb(call, ts)` (a) for each entry when
  the cache is seeded at startup, and (b) on every direct `on_heard`. The engine
  wires `heard.set_direct_heard_observer(router.note_heard_direct)` **only when
  the heard plugin is enabled**. Heard disabled → router simply never gets these
  pushes (graceful).

### Active-crosslink notifications (new)
- Transport base + AGWPE gain `set_netrom_crosslink_observer(cb)`; engine wires
  it to `router.note_crosslink`.
- AGWPE calls it `up=True` when a crosslink session+manager is created (inbound
  `'C'` classified as crosslink, **and** outbound `connect_out` confirmation),
  and `up=False` on crosslink teardown (`'d'`, `start()` finally, idle reaper).

### Inbound classifier uses the one predicate
- `engine._is_netrom_neighbor` → just `router.is_direct_neighbor` (passed via the
  existing `set_netrom_neighbor_check`). The transit-polluted `adjacent_neighbors`
  lookup is gone from the hot path.

### Cold-start fallback — keep as a safety net, note it's now rare
`_promote_to_netrom_crosslink` stays (first contact from an unheard node still
needs it), but with `is_direct_neighbor` reliable it fires far less. Do NOT
remove the `mute()`/`suppress_close()` machinery yet — it's the last line of
defense; revisit deleting it after N0.5 proves out on the air.

## Startup / ordering
Engine `run()` order: build router → `router.seed_from_db()` (routes) → if heard
enabled: seed heard cache, then `heard.set_direct_heard_observer(
router.note_heard_direct)` (which also replays the seeded cache into the router)
→ wire transports (`set_netrom_neighbor_check(router.is_direct_neighbor)`,
`set_netrom_crosslink_observer(router.note_crosslink)`). So immediately after a
restart the router knows both its seeded routes AND its recently-heard-direct
neighbors — the exact gap that made `C JOHN` go transit.

## Edge cases
- **Heard disabled**: no `note_heard_direct` pushes; `is_direct_neighbor` still
  answers from live crosslinks + fresh direct NODES routes. Fully functional.
- **Random ham heard direct** (not a node): `_is_known_node` False →
  `is_direct_neighbor` False → inbound = BBS user. Correct.
- **First contact from an unheard node**: `is_direct_neighbor` False → classified
  BBS → cold-start fallback promotes on the first pid=0xCF `'D'`. Safety intact.
- **Crosslink up but RF hearing stale**: `is_direct_neighbor` True via
  `_crosslinks` (the live link is proof). This is what makes reuse robust.
- **Crosslink teardown**: `note_crosslink(call, False)` removes it; adjacency
  then relies on heard-direct / fresh routes again.
- **TTL**: `netrom.direct_heard_ttl_minutes` (existing, default 60) governs
  `_heard_direct` freshness.

## Tests (offline)
- `is_direct_neighbor` truth table: live crosslink; known-node + fresh
  heard-direct; known-node + fresh direct route; known-node stale on all →
  False; unknown-node heard-direct → False; TTL expiry.
- `best_neighbor_for`: direct-to-dest when reachable; transit via a
  direct-reachable first hop; **skips a transit route whose first hop is not
  reachable** in favor of one whose first hop is; best-effort fallback when none
  are confirmed reachable; None when unknown/below quality.
- `note_heard_direct` / `note_crosslink` update the predicate; crosslink down
  reverts.
- **Graceful without heard**: no observer wired → predicate still works from
  crosslinks + routes.
- Engine wiring: inbound classifier calls `is_direct_neighbor`; heard-optional.
- Regression: existing router/circuit/node/agwpe suites stay green (adjust the
  couple of tests that asserted the old `adjacent_neighbors == via_call set`
  semantics or `set_direct_heard_check`).

## Live validation (radiostation2)
1. Restart → `C JOHN` connects **direct** (heard-direct pushed in at seed), not
   transit.
2. JOHN connecting **inbound** is classified as a **crosslink** (no BBS banner
   on the inter-node link; `_run_session` "BBS session" log does NOT appear for
   it).
3. A genuinely multi-hop `C <far>` still works and picks a **heard-direct first
   hop** (watch Direwolf: SABM to a neighbor we actually hear, no RETRYOUT).
4. A normal ham connecting to the BBS is still a BBS user.

## Out of scope for N0.5
- N3 (`W6ELA-5` node SSID identity / BBS-as-app).
- N4 (ACL, loop guards, Telnet/AXUDP, dashboard).
- `MH` → true RF-heard listing (separate small item).
- Deleting the cold-start fallback (revisit after this proves out).

## Files touched
- `bbs/netrom/router.py` — enrichment maps + `note_heard_direct` /
  `note_crosslink` / `is_direct_neighbor` / reworked `best_neighbor_for` /
  redefined `adjacent_neighbors`; remove `set_direct_heard_check`.
- `bbs/plugins/heard/heard.py` — `set_direct_heard_observer`; emit on seed + on
  direct `on_heard`. (Keeps `_direct_heard` cache as the RF source of truth.)
- `bbs/transport/base.py` + `bbs/transport/agwpe.py` —
  `set_netrom_crosslink_observer`; fire `note_crosslink` up/down at
  crosslink lifecycle points; classifier already uses the injected predicate.
- `bbs/core/engine.py` — wire predicate + observers; drop the direct-heard
  lambda.
- Tests across `test_netrom_router.py`, `test_heard.py`, `test_agwpe.py`.
