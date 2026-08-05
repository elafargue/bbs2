# NET/ROM in bbs2 — implementation & design

This is the canonical overview of bbs2's NET/ROM implementation: what it does,
how it's structured, and — honestly — where it diverges from the 1987 NET/ROM
v1.3 reference (Software 2000, Ron Raikes WA8DED / Mike Busch W6IXU).

It is the entry point. The per-milestone specs in this directory
(`*_SPEC.md`, `N6_FIDELITY_GAPS.md`) are the deep dives; this document ties them
together and is the thing to read first.

**One-line verdict:** bbs2 is a faithful NET/ROM **node** on the wire-visible
routing protocol (Level 3), a pragmatic AX.25-backed subset on the transport
(Level 4), and a **gateway node** rather than an L3 store-and-forward relay — the
last being a deliberate architectural choice, not an oversight.

---

## 1. Scope — what "a NET/ROM node" means here

bbs2 participates in a NET/ROM network as a first-class node with its own alias
and SSID (e.g. `PALO:W6ELA-5`). Concretely it:

- **Learns the network** from received NODES broadcasts and maintains a routing
  table (Level 3), by-the-book per the 1987 manual as of milestone N5.
- **Advertises itself** (and, optionally, its learned routes) via periodic NODES
  broadcasts.
- **Accepts inbound L3/L4 circuits** routed to its SSID through the mesh, landing
  the caller at the node's `=>` command prompt (or straight into the BBS).
- **Connects onward** (`CONNECT <alias|call>`) — opening an AX.25 crosslink to an
  adjacent neighbour and bridging the user through, acting as a gateway.
- **Hosts local applications** — the BBS itself and `ax25d`-style services are
  reachable as `CONNECT BBS` / `CONNECT <service>`.
- **Guards the gateway** — ACL, INTERLOCK loop-guard, rate limits, circuit caps.

All NET/ROM features run on the **AGWPE transport** (Direwolf / AGW Packet
Engine); the KISS transports are UI-frame only.

---

## 2. Architecture — the layer stack

```
   user session (radio or web terminal)
        │
   ┌────▼─────────────────────────────────────────────┐
   │ NetromNode  (bbs/netrom/node.py)                  │  N2/N3/N4
   │   => command switch, two-circuit byte bridge,     │
   │   gateway safety, local-app launcher              │
   └────┬───────────────────────────────┬──────────────┘
        │ onward CONNECT                 │ N/R/MH/… lookups
   ┌────▼──────────────────┐      ┌──────▼───────────────┐
   │ NetromCircuitManager  │      │ NetromRouter          │  N0/N5
   │ (bbs/netrom/circuit)  │      │ (bbs/netrom/router)   │
   │   L3/L4 circuits over  │      │   two-table routing:  │
   │   an AX.25 crosslink   │      │   neighbours + dests, │
   │                        │      │   quality, obsolesc.  │
   └────┬──────────────────┘      └──────┬───────────────┘
        │                                 │ NODES rx/tx
   ┌────▼─────────────────────────────────▼───────────────┐
   │ AGWPE transport (bbs/transport/agwpe.py)              │
   │   AX.25 frames, NODES broadcast loop, crosslinks      │
   └───────────────────────────────────────────────────────┘
```

- **`bbs/ax25/netrom_frame.py`** — the wire codec: NODES broadcast payloads and
  the 20-byte L3 header + L4 frame types (CONNECT REQ/ACK, DISC REQ/ACK,
  INFO/INFO-ACK).
- **`bbs/netrom/router.py`** — the routing brain (Level 3). Owns the two tables,
  computes route quality, runs the obsolescence lifecycle, builds NODES
  broadcasts, and persists the tables.
- **`bbs/netrom/circuit.py`** — L4 circuits (`NetromCircuit` /
  `NetromCircuitManager`): connection setup/teardown, sequence numbers, the
  outbound window, and the byte streams the node bridges.
- **`bbs/netrom/node.py`** — the `=>` command layer, the two-circuit bridge, and
  the gateway.
- **`bbs/netrom/gateway.py`** — the gateway-safety policy + guard (N4a).
- **`bbs/plugins/node/`** — the thin BBS-menu plugin (`@`) + native node-SSID
  landing that construct a `NetromNode` on a live session.

---

## 3. Level 3 — routing (the NODES protocol) — **by-the-book (N5)**

This is the part other nodes see, and it is faithful to the manual.

### 3.1 Two-table model

Per the 1987 manual, the router keeps **two** tables (`router.py`):

- **Neighbour list** (`_neighbours: dict[str, NeighbourEntry]`) — adjacent nodes
  we hear directly, each with a **path quality** (the quality of our RF link to
  that neighbour) and a use count.
- **Destination list** (`_routes: dict[str, list[RouteEntry]]`) — every known
  destination node, with up to N alternate routes sorted by quality, each tagged
  with the adjacent neighbour (`via_call`) it goes through.

### 3.2 Route-quality composition

When a NODES broadcast advertises a route of quality *Q* via a neighbour whose
path quality is *P*, the received route quality is (manual p.65, rule 5):

```
route_quality = ((advertised × path_quality) + 128) >> 8     # a 256th, rounded
```

A **direct** route (the neighbour itself) takes the path quality verbatim. This
composition is what makes a 1-hop direct route always outrank a 2-hop transit
route through an equal link — the property the whole mesh relies on. (Pre-N5,
bbs2 stored the advertised quality verbatim and skipped this step; N5a fixed it.)

### 3.3 Obsolescence lifecycle

Routes age out by **obsolescence count**, not a hard TTL (manual p.66, PARMS):

- New/refreshed route → `obs_count = obs_initializer` (default **6**).
- Each broadcast cycle, every route's `obs_count` is decremented (N5b `decay`).
- At **0** the route is deleted; the neighbour list is reconciled.
- `obs_count == 0` on a **manually added** route means **locked** (permanent,
  never decremented) — the manual's "locked route" concept.
- Only routes with `obs_count >= obs_min_to_broadcast` (default **5**) are
  re-advertised in transit mode; locked routes always are.

### 3.4 NODES broadcast — format & fragmentation

A NODES broadcast is one or more AX.25 UI frames to destination `NODES`,
PID `0xCF`. Each frame is a 7-byte header (`0xFF` discriminator + 6-byte source
alias) followed by 21-byte entries (dest call, dest alias, best next-hop call,
quality). A frame holds at most `(256 − 7) // 21 = 11` entries; a larger table
**fragments across multiple frames**, each re-stamped with the header
(N6a — `router.build_nodes_payloads()`).

Two advertisement modes (`advertise_self_only`, config):

- **Polite-client (default, `True`)** — emit only the 7-byte header. Peers add us
  as a direct neighbour on receipt regardless of entry count. This is how
  endpoint stations announce themselves; it never pollutes the mesh with a
  re-broadcast table.
- **Transit-node (`False`)** — advertise the best route per destination at its
  **composed** quality (the receiver re-composes through *their* link to us, so
  multi-hop degradation is inherent — no artificial `hop_cost`). Obsolescence-
  gated. Reserved for high-reachability nodes by network convention.

### 3.5 Adjacency

One-hop adjacency (who we may route *through*) is decided by the router, the
single authority (N0.5). A node is adjacent iff any of: a live AX.25 crosslink is
up to it, it is in the neighbour list with path quality > 0, or it is a known
node heard directly. This keeps `ROUTES`, `MHEARD`, and onward-connect
next-hop selection consistent.

### 3.6 Trivial-loop & hygiene guards

- Non-callsign NODES destinations are rejected (N0a — routing hygiene).
- A route that would point back through the advertiser trivially (quality would
  round to 0) is dropped.
- `worst_quality` (default 1) filters routes below the floor;
  `max_destinations` (default 200) caps table size.

---

## 4. Level 4 — transport / circuits — **pragmatic AX.25-backed subset**

bbs2 carries NET/ROM L3/L4 frames **inside AX.25 connected mode** on each
point-to-point crosslink (`circuit.py`). It implements, correctly on the wire:

- CONNECT REQ/ACK (with window negotiation + user/origin callsign tail),
  DISC REQ/ACK, INFO / INFO-ACK.
- Sequence numbers V(S) / V(R) / V(A) and **outbound window flow control**: the
  writer blocks when `V(S) − V(A) >= window`, the real L3 backpressure the spec
  assumes. (Without this, a peer validly drops in-window-but-out-of-L3-window
  frames — the "missing lines in long output" bug fixed in circuit V1.1.)
- TX fragmentation at the L3 INFO MTU; inbound MORE_FOLLOWS reassembly.
- Circuit-table-full refusal via CONNECT ACK + CHOKE bit.

It deliberately **omits** parts of the manual's L4 (see §7, Gap 2): no T1
retransmit timer (AX.25 connected mode handles retransmission), no CHOKE/NAK
backpressure beyond circuit refusal, and greedy immediate ACK instead of the
delayed-ACK (T2) scheme. These do not affect interop on real (point-to-point)
AX.25 crosslinks.

---

## 5. The node interface (N2/N3) — `=>` switch, bridge, apps

A caller who connects to the node SSID (or enters via the BBS `@` menu) lands at
the `=>` prompt. Commands (BPQ-style shortest-unambiguous-prefix; full verbs
shown in the banner):

| Verb | Does |
|------|------|
| `CONNECT <node\|call\|app>` | Connect onward to a node/BBS, or launch a local app |
| `NODES [pattern]` | List known nodes (best route per dest) |
| `ROUTES [node]` | Neighbour list; or the routes to a given node |
| `USERS` | Active gateway circuits |
| `INFO` | Node info |
| `MHEARD` | Stations heard directly (from the heard plugin) |
| `PORTS` | Transports |
| `BYE` | Disconnect |

**Two-circuit bridge:** on `CONNECT`, the node opens an outbound circuit to the
next hop and bridges bytes byte-for-byte between the user's circuit and the
onward circuit until either end closes, then returns to `=>` (ReConnect). Line
buffering + inbound INFO dedup make this work for web (character-mode) terminals.

**Node identity (N3):** the node SSID is a first-class identity. A user landing
on it gets the node prompt, not the BBS menu; the **BBS and services become
applications** reachable as `CONNECT BBS` / `CONNECT <service>`. The `@` BBS-menu
entry remains for users who start in the BBS.

---

## 6. Gateway safety (N4a) & INTERLOCK

Onward connects are governed by a node-wide `GatewayGuard` with a
`GatewayPolicy` (`gateway.py`):

- **ACL** — allow/deny onward destinations.
- **INTERLOCK loop-guard** — the node refuses to route a circuit back out the
  same crosslink it arrived on (`Connection.netrom_via`), breaking the classic
  A→B→A loop. Composed with per-hop bridging, this is why bbs2 is safe as a
  gateway even without full L3 transit.
- **Rate limits & circuit caps** — per-user and node-wide ceilings on concurrent
  gateway circuits, with a refusal history for observability.

---

## 7. Divergences from the 1987 manual

Full detail in **`N6_FIDELITY_GAPS.md`**; summary:

| # | Area | Manual | bbs2 | Disposition |
|---|------|--------|------|-------------|
| 1 | NODES frame fragmentation | multi-frame, 11/frame | multi-frame, ≤11/frame | ✅ done (N6a) |
| 2 | L4 transport (T1/CHOKE/T2) | full L4 timers + choke | AX.25-backed subset | won't-fix (by design) |
| 3 | L3 transit / end-to-end L4 | store-and-forward relay | per-hop bridge (gateway) | deliberate divergence |

**Gap 3 is the one worth understanding.** Real NET/ROM runs L4 end-to-end between
the two endpoint nodes and forwards L3 packets hop-by-hop with TTL decrement at
intermediate nodes. bbs2 instead **terminates a circuit on each hop and bridges
the bytes** at the node layer. User-visible result is identical (you connect to
us, we connect you onward), it interoperates cleanly, and it's arguably safer —
but bbs2 cannot be a pure middle hop carrying someone else's end-to-end L4
circuit. TTL exists in our headers (`_DEFAULT_TTL = 25`) but the
decrement-and-forward loop is intentionally not run.

---

## 8. Persistence & observability

- **Routing tables** are owned and persisted by the router
  (`netrom_routes` + `netrom_neighbours`, additive schema with `obs_count`),
  restored on restart via `seed_from_db` so adjacency survives reboots (N5b/b2).
- **Broadcast cadence** (beacon + NODES timestamps) is persisted so a restart
  respects the interval instead of re-broadcasting immediately — politer on air.
- The **heard plugin** owns `stations` + the topology graph; the router owns the
  composed routing table. `MHEARD` reads recent RF-heard stations from heard.
- The **web dashboard** shows live node sessions + the gateway-safety strip
  (N4c); the **Services** page shows reserved SSIDs (BBS + node) read-only.

---

## 9. Configuration (`netrom:` block)

Key knobs (see `config/bbs.yaml.example` for the full annotated set):

| Key | Default | Meaning |
|-----|---------|---------|
| `node_ssid` / alias | — | the node's first-class identity |
| `advertise_self_only` | `true` | polite-client vs transit-node mode |
| `channel_quality` | 192 | path quality assigned to directly-heard neighbours |
| `worst_quality` | 1 | drop routes below this |
| `max_destinations` | 200 | destination-table cap |
| `obs_initializer` | 6 | starting obsolescence count |
| `obs_min_to_broadcast` | 5 | min obs to re-advertise (transit mode) |
| `gateway:` | — | ACL / rate / caps for onward connects |
| `hop_cost`, `min_advert_quality` | — | **retired** (parsed, inert — superseded by composed quality) |

---

## 10. Milestone history & where to read more

| Milestone | What | Spec |
|-----------|------|------|
| N0 | Routing hygiene + outbound next-hop API | — |
| N0.5 | Router as the single adjacency authority | `ADJACENCY_SPEC.md` |
| N1 | Outbound crosslink `connect_out` | `CONNECT_OUT_SPEC.md` |
| N2 | Node command layer + session bridge | `CONNECT_OUT_SPEC.md` |
| N3 | First-class node identity + BBS-as-application | `NODE_IDENTITY_SPEC.md` |
| N4 | Gateway safety (ACL/INTERLOCK/rate/caps) + web | `GATEWAY_SAFETY_SPEC.md` |
| N5 | Routing-table fidelity (two-table model) | `ROUTING_FIDELITY_SPEC.md` |
| N6 | Fidelity audit + NODES fragmentation | `N6_FIDELITY_GAPS.md` |

All milestones were live-validated against the NORCAL 145.05 mesh
(radiostation2) except N6a, which is behind the default-off transit mode and so
does not change on-air behaviour until enabled.

---

## 11. Testing

The NET/ROM stack is covered by `tests/test_netrom_router.py`,
`test_netrom_circuit.py`, `test_netrom_node.py`, `test_netrom_node_engine.py`,
`test_netrom_gateway.py`, `test_netrom_web.py`, plus `test_agwpe.py` (transport)
and `test_config_node_ssid.py`. The full suite is green. Wire-protocol behaviour
(quality composition, obsolescence, NODES fragmentation, the window guard,
INTERLOCK) is unit-tested; on-air validation remains the final gate for
protocol changes.
