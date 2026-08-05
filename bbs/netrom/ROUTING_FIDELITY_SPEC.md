# N5 — Routing-table fidelity: the real NET/ROM two-table model

Status: **N5 IMPLEMENTED (919 tests green). N5a + N5b live-validated on
radiostation2; N5c pending live.** Compaction-proof capture. Roadmap milestone
**N5** (routing-table fidelity). Branch: `netrom-node`. Builds on the whole
N0–N4 stack.
Companion specs: `ADJACENCY_SPEC.md` (N0.5), `CONNECT_OUT_SPEC.md`,
`NODE_IDENTITY_SPEC.md`, `GATEWAY_SAFETY_SPEC.md`.

**N5a done:** receive-side quality composition (`((adv × path_q) + 128) >> 8`),
neighbour list with per-neighbour path quality, and the 1987 receive algorithm
(rules 1–9). Live on radiostation2: every NODES ingest logs `path_q=192`, and
the node's `ROUTES` now reports all neighbours at the channel path quality (192)
— e.g. SCLARA dropped 194→192 as the composed transit route fell below the
1-hop direct route, exactly as the model predicts. `route_ttl`/`hop_cost`/
`min_advert_quality` still active pending N5b/N5c. Remaining phases below.

**Primary source:** *NET/ROM Version 1.3 Documentation* (Software 2000, Inc.,
Sept 1987; Raikes WA8DED / Busch W6IXU) — `1987_netrom_version_1_3.pdf` in the
repo root. Page citations below are to that manual. This spec re-implements its
routing algorithm faithfully, keeping bbs2's modern enrichments layered on top.

## Why
Our router is a *simplified single-table approximation*. Against the primary
source it diverges on the parts that most affect routing correctness and
peer-consistency (verified live on radiostation2, and against ROCK/K6FB-5):

| Manual (1987) | bbs2 today |
|---|---|
| route qual = **advertised × link ÷ 256** (p.65 rule 5) | stores advertised quality **raw** (`_process_nodes`) |
| per-neighbour **path (link) quality** (p.64, PARMS #3/#4) | flat `_DIRECT_NEIGHBOR_QUALITY = 192` for all |
| **obsolescence-count** decay, delete at 0 (p.66) | hard `route_ttl` (last_seen) expiry |
| two tables: **neighbour list** + **destination list** (p.63) | one flat `_routes: dict[str,list[RouteEntry]]` |
| ≤ 3 routes / destination (p.65 rule 7) | `_MAX_ROUTES_PER_DEST = 3` ✓ |
| trivial-loop → quality 0 (p.65 rule 6) | none |
| PARMS: worst-qual filter, obs-init, obs-min-broadcast, max-dests | none |

Observed symptoms: our qualities read `192/194` where ROCK reads `70` (ours
aren't link-adjusted); `ROUTES` flickers to a handful while `NODES` shows ~60
(hard-TTL volatility + no obs-count decay); `C JOHN` once mis-picked a transit
first hop (raw-quality misordering).

## Goal
Make `NetromRouter` implement the **two-table NET/ROM model** with the exact
receive-side quality algorithm and obsolescence-count lifecycle, while **keeping
bbs2's enrichments** that the 1987 firmware never had — a *live AX.25 crosslink
as proof of adjacency* (N0.5), *RF direct-hearing* from the heard plugin, and
*DB persistence*. The 1987 model is the core; those three are additive signals.

## The two tables (p.63: "two dynamically allocated threaded lists")

### Neighbour list — who we can reach in one hop
```
NeighbourEntry:
    call:        str          # adjacent node callsign
    port:        str          # transport/channel id (bbs2: transport_id; 1987: HDLC/RS232)
    path_quality:int          # OUR link quality to it (0-255); default from channel (PARMS)
    use_count:   int          # number of destination-routes currently via this neighbour
    locked:      bool         # operator-set (ROUTES+), never auto-updated/deleted
    crosslink:   bool         # bbs2: a live AX.25 crosslink exists right now (N0.5 enrichment)
    last_heard:  float        # bbs2: unix ts of last broadcast/RF-direct hearing (liveness/UI)
```
- Created automatically when a NODES broadcast arrives from a new originator,
  initialised with the **channel default path quality** (p.65 rule 3).
- `path_quality` is settable per-neighbour (the `ROUTES+`/`ROUTES-` equivalent);
  0 makes us ignore the neighbour entirely (p.64).
- An **unlocked** neighbour whose `use_count` reaches 0 is deleted (p.23) —
  unless `crosslink` is up (bbs2: a live link keeps it, even circuit-less).

### Destination list — every node we know, best routes to it
```
DestEntry:
    dest_call: str
    alias:     str
    routes:    list[Route]    # ≤ 3, sorted by quality desc (p.65 rule 7)

Route:
    neighbour: str            # which neighbour-list entry to forward through
    quality:   int            # computed route quality (0-255), NOT the raw advert
    obs_count: int            # obsolescence count; 0 = locked (manual) or dead
    port:      str
    in_use:    bool           # ">" marker — currently the selected route
```
`is_direct` ⇔ `neighbour == dest_call` (a route straight to the node itself).

## Receive algorithm — process one NODES broadcast (p.65, rules 1–9)
Given a broadcast from `origin` (neighbour) carrying `(dest, alias, best_nbr,
adv_quality)` entries:

```
0. (bbs2) ignore if not a decodable NODES / wrong PID.                (rule 2)
1. if worst_quality_for_auto_updates == 0: ignore whole broadcast.    (rule 1)
2. nbr = neighbour_list.get_or_create(origin, port,
             path_quality = channel_default_quality(port))            (rule 3)
   nbr.last_heard = now
3. DIRECT route to origin:  upsert Route(neighbour=origin,
       quality = nbr.path_quality, is_direct)                         (rule 4)
4. for each advertised (dest, alias, best_nbr, adv_quality):
       rq = ((adv_quality * nbr.path_quality) + 128) >> 8             (rule 5)
       if best_nbr == our_node_call:  rq = 0    # trivial loop         (rule 6)
       if rq < worst_quality_for_auto_updates: skip                    (rule 8)
       if dest is new and len(dest_list) >= max_destinations: skip     (rule 9)
       upsert Route(dest, neighbour=origin, quality=rq,
                    obs_count = obs_initializer)                        (obs, p.66)
5. per destination keep only the 3 highest-quality routes.            (rule 7)
6. recompute use_count per neighbour; delete unlocked neighbours at 0.
```
`upsert Route` on an existing (dest, neighbour) pair refreshes quality and
**re-initialises `obs_count` to `obs_initializer`** (p.66). q0 (loop) routes are
kept only as last resort and **never re-advertised**.

Key change vs today: step 4's `rq = (adv × path_quality + 128) >> 8`. With a
1200-baud user channel (`path_quality 192 ≈ 0.75`), a neighbour advertising a
dest at 224 becomes `(224×192+128)>>8 = 168` here — link-adjusted, matching how
ROCK's numbers look, instead of our current raw `224`.

## Obsolescence-count lifecycle (p.66, PARMS #5/#6) — replaces hard TTL
- On add/update by a broadcast **or on successful use of the route**, set
  `obs_count = obs_initializer` (default **6**).
- A periodic scan **at the broadcast interval** decrements every route's
  `obs_count`; at **0 the route is deleted** (and its neighbour's `use_count`
  drops). `obs_initializer == 0` disables decay (routes permanent).
- `obs_count == 0` set *manually* = **locked**: never auto-updated or deleted.
- This replaces `prune_stale_routes()` (last_seen/TTL) with a decrementing scan.
  `route_ttl_minutes` is retired; `last_seen` stays only as a display/telemetry
  field. Net effect: a neighbour that goes briefly quiet lingers for
  `obs_initializer` cycles (stable tables, like ROCK) instead of vanishing at
  one TTL.

## Quality model (p.64) — the numbers, defined
- Quality is a fraction of 256 (255 ≈ perfect, 0 = unknown/loopback).
- **Path (link) quality** per channel — the manual's suggested defaults become
  ours: 1200-baud user channel = **192**, 1200-baud HDLC backbone = 224,
  9600-baud RS232 = 255, HF 300-baud = 128, unknown/loopback = 0.
- Multi-segment quality = **product** of segment fractions (the RX formula
  composes this hop-by-hop as it propagates).
- All quality math rounds to nearest 256th (`+128` before `>>8`).

## Config — map the NET/ROM PARMS to `netrom:` keys (defaults from pp.66-67)
```yaml
netrom:
  # existing keys retained; new/renamed for N5:
  channel_quality: 192          # PARMS #3 default path quality for the RF port (1200-baud user)
  neighbour_quality:            # optional per-neighbour path-quality overrides (ROUTES+)
    KF6ANX-4: 200
  worst_quality: 1              # PARMS #2 — ignore learned routes below this (0 = ignore ALL NODES)
  obs_initializer: 6            # PARMS #5 — obsolescence count on add/update/use (0 = no decay)
  obs_min_to_broadcast: 5       # PARMS #6 — only advertise routes with obs >= this
  max_destinations: 200         # PARMS #1 (manual default 50, max 400; raise for a modern mesh)
  nodes_interval: 30            # PARMS #7 broadcast interval — already ours (manual default 60)
```
Back-compat / retirement:
- `route_ttl_minutes` → **retired** (replaced by obs-count); read it once with a
  deprecation warning if present.
- `hop_cost` / `min_advert_quality` → **superseded** by the RX quality formula +
  `obs_min_to_broadcast`; keep parsing them but log that they're inert under N5.
- `direct_heard_ttl_minutes` → still used by the *heard-direct enrichment* (a
  neighbour we heard on RF within this window is counted reachable even if its
  `use_count`/obs would otherwise drop it — a bbs2 addition).

## Reconciling with bbs2's enrichments (keep N0.5's wins)
The 1987 firmware had none of these; layer them onto the faithful model, don't
lose them:
- **Live crosslink = proof of adjacency.** A neighbour with `crosslink=True`
  (transport `note_crosslink`) is always in the neighbour list and reachable,
  regardless of obs/use — the strongest signal (N0.5).
- **RF direct-hearing.** `note_heard_direct` (heard plugin) keeps a neighbour's
  `last_heard` fresh and can hold it reachable within `direct_heard_ttl` even if
  it stopped sending NODES — and a *received NODES broadcast is itself a direct
  hearing*, so mark the source `last_direct_heard` (this folds in the deferred
  "persistence fix": adjacency now survives a restart via the heard cache + the
  persisted neighbour table).
- `is_direct_neighbor(call)` redefines cleanly as: **`call` is in the neighbour
  list with `path_quality > 0`** (i.e. a live crosslink, or heard/broadcast
  within its obs/heard window). Inbound classification (engine), `best_neighbor_for`
  (N0b first-hop hardening), and the gateway INTERLOCK (N4a) all keep calling it.

## Node commands — match peer output (p.22, p.18)
- `ROUTES` (no arg) → the **neighbour list**, one row per neighbour:
  `> <port> <neighbour> <path_quality> <use_count> [!]`  (p.22).
- `ROUTES <neighbour>` → that one entry.
- `NODES <call>` → up to 3 **routes** to it: `> <quality> <obs> <port> <neighbour>`
  (p.18). `NODES` (no arg) → the alias:call destination list (as today).
- `MH` → **actual RF-heard stations** from the heard plugin (fix today's
  conflation where `R` and `MH` both print the neighbour set).

## Persistence / DB
- `netrom_routes` gains `obs_count`, `port`, and route `quality` is now the
  *computed* value; add a **`netrom_neighbours`** table (call, port,
  path_quality, use_count, locked, last_heard). `seed_from_db` restores both,
  so adjacency + link qualities survive a restart (closes the N0.5 persistence
  gap for good).
- **Prune the DB** on the obs scan (delete rows at obs 0) so `netrom_routes`
  stops accumulating ancient rows (we saw entries 5–58 days old — the table is a
  growing log today, not a mirror).

## Migration / rollout
1. Schema: add columns/table (idempotent migrations, like the existing
   `netrom_routes` composite-PK migration in `heard.py`).
2. On upgrade, existing `route_ttl` rows seed as `obs_count = obs_initializer`
   (they'll converge within a few cycles). Qualities recompute on the next NODES
   from each neighbour (raw → link-adjusted), so numbers drop toward the ROCK-like
   range — expected, not a regression.
3. Default `channel_quality: 192` reproduces today's constant, so a station that
   sets nothing sees the same neighbour quality but now correctly *composed*
   for transit destinations.

## Edge cases
- **worst_quality = 0** ⇒ ignore all incoming NODES (isolated node). (rule 1)
- **obs_initializer = 0** ⇒ routes never decay (permanent) — offer but warn.
- **obs_min_to_broadcast > obs_initializer** ⇒ we'd advertise only ourselves;
  clamp/​warn (p.66 note).
- **Trivial loop** (advertised best-neighbour == us) ⇒ q0, kept last-resort,
  never re-advertised. (rule 6)
- **Locked route/neighbour** (obs 0 / `!`) ⇒ untouched by auto-update + scan.
- **Live crosslink to a neighbour with no routes** ⇒ neighbour retained
  (bbs2), unlike pure 1987 use-count deletion.
- **Polite-client mode** (`advertise_self_only`) still supported — full model
  *can* re-advertise the destination list (transit node) gated on
  `obs_min_to_broadcast`; keep header-only as the default per NORCAL convention.

## Tests (offline)
- Quality formula: `((adv*pq)+128)>>8` truth values (e.g. 224×192→168, 255×255→254),
  rounding at the 256th.
- RX algorithm rules 3–9 individually: neighbour auto-create with channel
  default; direct-route = path_quality; indirect = formula; trivial-loop→0;
  3-best truncation; worst-quality filter; max-destinations cap.
- Obsolescence: init on add/update/use; decrement scan; delete at 0; locked
  (obs 0) survives; `obs_initializer=0` disables decay.
- Neighbour list: use_count inc/dec; unlocked delete at 0; crosslink pins it;
  path_quality override changes all downstream route qualities.
- `is_direct_neighbor` / `best_neighbor_for` still correct on the new model;
  INTERLOCK + inbound classifier regression-green.
- Persistence: neighbours + routes (with obs) round-trip through the DB; seed
  restores adjacency; obs-0 rows pruned.
- Node commands: `ROUTES` / `NODES <call>` render the peer-matching columns.

## Live validation (radiostation2)
1. `ROUTES` shows a stable neighbour list with **link-adjusted** path qualities
   (192-ish for our RF port), matching the `port call qual use` shape of ROCK.
2. `NODES <call>` shows up to 3 routes as `> qual obs port neighbour`.
3. A neighbour that goes briefly quiet **stays listed for ~obs_initializer
   cycles** instead of vanishing at one TTL.
4. `C <transit dest>` picks the highest **composed-quality** first hop; compare
   against ROCK's route to the same dest.
5. Restart → neighbour list + adjacency restored from DB immediately (no blind
   window).

## Suggested phasing (one milestone, staged commits)
- **N5a** — quality formula + neighbour table + per-neighbour path quality
  (routing-correctness core).
- **N5b** — obsolescence-count lifecycle replacing hard TTL + DB persistence of
  both tables + pruning.
- **N5c** — node-command restyle (`ROUTES`/`NODES`/`MH`) + optional transit
  re-advertisement gated on `obs_min_to_broadcast`.

## Files (anticipated)
- `bbs/netrom/router.py` — the two-table model, RX algorithm, quality formula,
  obs-count scan, redefined `is_direct_neighbor` / `adjacent_neighbors` /
  `best_neighbor_for` / `build_nodes_payload`.
- `bbs/netrom/neighbour.py` (new, optional) — `NeighbourEntry` + neighbour list.
- `bbs/plugins/heard/heard.py` — `netrom_neighbours` table + persist/seed;
  mark NODES source `last_direct_heard`; obs-aware pruning.
- `bbs/netrom/node.py` — `ROUTES`/`NODES <call>` column formats; split `MH`.
- `bbs/config.py` + `config/bbs.yaml.example` — PARMS-mapped keys + deprecations.
- `bbs/core/engine.py` — obs-scan task (replaces the TTL prune loop); wiring.
- Tests across router / heard / node / config.

## Out of scope for N5
- Multi-transport per-neighbour port selection at connect time (N4d).
- Transport-layer (L4) tuning — window/timeout PARMS (#8+); we ride AX.25 ARQ.
- Manual `ROUTES+/-` / `NODES+/-` operator editing over the air (a later nicety;
  N5 exposes the same effect via config `neighbour_quality` + web).
