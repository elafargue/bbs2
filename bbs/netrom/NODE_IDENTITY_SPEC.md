# N3 — `W6ELA-5` node identity + applications

Status: **IMPLEMENTED & live-validated (2026-08-04).** Confirmed on the air on
radiostation2 (`W6ELA-5`): a NET/ROM user landed natively at `=>` via a K6FB-5
crosslink, ran `C BBS` and ReConnected to the node, the neighbor crosslink
classified correctly on the node SSID, NODES sourced from `W6ELA-5` (mesh
relearned `PALO:W6ELA-5`), and the idle reaper dropped the crosslink at 15 min.
Compaction-proof capture. Roadmap milestone **N3**
(`~/.claude/plans/netrom-node-w6ela5.md`). Branch: `netrom-node`.
Builds directly on N1 (`connect_out`), N2 (`NetromNode`), and N0.5 (router as the
single adjacency authority). Companion specs: `CONNECT_OUT_SPEC.md`,
`ADJACENCY_SPEC.md`.

## Goal
Turn "a BBS with a NET/ROM node interface" into "a **real NET/ROM node** with the
BBS as one of its applications." A user connecting to the **node SSID**
(`W6ELA-5`) lands natively at the `=>` prompt; the BBS (`W6ELA-1`) and the
ax25d-style services become **applications** reachable both by direct-connect and
by `C BBS` / `C <app>` from the prompt. The node advertises **itself** (PALO) on
its own SSID in NODES.

Guiding principle (BPQ): **SSID → role is pure config.** Nothing hardcodes
"BBS = -1" or "node = -5".

## SSID-as-config model
```yaml
bbs:
  callsign: W6ELA
  ssid: 1               # the built-in BBS, now "an application" at W6ELA-1
netrom:
  alias: PALO
  node_ssid: 5          # NEW: the node identity SSID (=> lands here). Unset =
                        #      node runs on the BBS SSID (today's behavior).
services:
  routes:
    W6ELA-9: { … }      # ax25d apps (existing)
```
- **Default `node_ssid` unset ⇒ no behavior change**: the node stays reachable
  only via the `@` BBS-menu entry (N2), and the BBS keeps `W6ELA-1`. Setting
  `node_ssid: 5` is the **opt-in** move to a first-class node identity.
- Three role classes, all keyed by the **called SSID**: the **node** (`=>`), the
  **BBS** app, and **service** apps.

## Inbound dispatch — route by called SSID (`conn.local_addr`)
`engine._on_connection` already routes by called SSID via `ServiceDispatcher`
(EXEC external program / REFUSE / PASS→BBS). N3 inserts a **node** branch:

```
called = conn.local_addr.upper()          # which of our SSIDs the caller dialed
if called == node_ssid_callsign:          # e.g. W6ELA-5
    → run NetromNode natively (=> prompt); BYE ⇒ disconnect
elif services.match(conn) is EXEC/REFUSE: → external service / refuse (existing)
else:                                     → the BBS application (existing PASS)
```
Order: **node SSID first**, then service dispatch, then BBS. (A NET/ROM crosslink
arriving *on* the node SSID from an adjacent neighbor is still classified by the
transport's `is_direct_neighbor` check as today — that path is unchanged; this
branch is for **user** connects to the node SSID.)

## The node as a native landing (reuse N2's `NetromNode`)
- Construct the same `NetromNode` as the `@` plugin does, on the live `conn`.
- **Exit contract** (already designed in N2): `command_loop()` returns on `BYE`;
  the **caller** maps it. Native node-SSID landing ⇒ **disconnect**; the `@`
  BBS-menu entry ⇒ **back to the BBS menu**. `NetromNode` itself never closes the
  connection — both entry points coexist and share one `NetromNode`.
- Banner/prompt identity uses the **node** call/alias (`PALO:W6ELA-5}`).

## Local applications — `C BBS` / `C <app>`
`NetromNode.cmd_connect` today only does NET/ROM onward connects. N3 makes it
resolve **local applications first**:

```
async def cmd_connect(target):
    app = self._apps.get(target.upper())     # BBS / service alias → local app
    if app is not None:
        await self._run_local_app(app)       # run on the current conn, like a
        return                                # bridge; on exit return to =>
    # …else the existing NET/ROM resolve → connect_netrom → originate → bridge
```
- **App registry** injected at bind time: `{"BBS": <bbs app>, "<svc>": <service
  route>, …}`. The BBS app = run a `BBSSession` on the current `conn`; a service
  app = `run_service(conn, route, argv)`. Both already exist; N3 just invokes
  them from the node and returns to `=>` when they end (same "run then
  ReConnect" shape as the two-circuit bridge).
- Names: `BBS` (always, when the BBS is enabled), plus each service route's
  called-SSID and/or a friendly alias. Keep it small for N3; a fuller app-menu
  is polish.
- Auth: reuse the session's `auth`; a service's `min_auth` still applies.

## Node identity in NODES / routing (the network-visible change)
Today the router self-advertises **PALO on `W6ELA-1`** and NODES broadcasts leave
from the transport's `_local_call` (`W6ELA-1`). For a first-class node:
- The router's **node identity** for self-advertisement becomes the **node SSID**
  (`W6ELA-5`) when `node_ssid` is set: its NODES self-entry and
  `build_nodes_payload` source alias advertise `PALO:W6ELA-5`.
- The **transport broadcasts NODES from the node SSID**. Add
  `transport.set_netrom_node_call(node_call)` (defaults to the BBS callsign);
  the NODES `M`-frame `call_from` uses it.
- Effect on the mesh: neighbors relearn `PALO` as `W6ELA-5`; the old `W6ELA-1`
  PALO route ages out (route TTL). **`W6ELA-1` stays reachable as the BBS app**
  throughout — it's just no longer the node.

## Outbound crosslink source callsign
A NET/ROM node's crosslinks originate **from the node callsign**. Today
`connect_out` sources from the transport's `_local_call` (`W6ELA-1`). For N3:
- `set_netrom_node_call(W6ELA-5)` also makes `connect_out` originate the AGWPE
  `'C'` from `W6ELA-5` (and `_make_netrom_writer` source outbound L3 `'D'` from
  `W6ELA-5`).
- **Registration**: `W6ELA-5` must be `X`-registered with Direwolf (like the BBS
  callsign and service SSIDs). Register it alongside the extra callsigns.
- Inbound crosslinks *to* `W6ELA-5` already work: the session sources outbound
  frames from `call_to` (the dialed SSID), so no change there beyond registering
  the SSID.

## AGWPE registration
Register `W6ELA-5` with Direwolf so inbound connects route to us. Reuse the
`set_extra_callsigns` path (it already `X`-registers extra SSIDs and the monitor
toggle is guarded to fire once). The node SSID is just another registered
callsign; the `_on_connection` dispatch (above) does the role routing.

## Config keys (under `netrom:` unless noted)
- `node_ssid` (int, optional) — the node identity SSID. Unset ⇒ node on the BBS
  SSID (current behavior). Set ⇒ `=>` lands natively on `<callsign>-<node_ssid>`.
- existing: `alias` (PALO), all the N0/N0.5/N2 keys.
- `bbs.ssid` (existing) — the BBS application SSID.
- `config/bbs.yaml.example` documents the node/BBS/services SSID split.

## Migration / rollout
1. Ship with `node_ssid` **unset** by default — zero change for every live
   station on upgrade (node still reached via `@`, BBS on `W6ELA-1`).
2. Operator opts in: set `node_ssid: 5`, register `W6ELA-5`, restart. Announce a
   transition window; keep `W6ELA-1` reachable as `C BBS` and by direct connect
   throughout. Neighbors relearn `PALO:W6ELA-5` within a NODES cycle; the old
   `-1` PALO route ages out on TTL.

## Edge cases
- `node_ssid == bbs.ssid` (misconfig) — reject at config load (a role can't be
  both node and BBS); log and fall back to node-on-BBS-SSID.
- `C BBS` when the BBS is disabled — "BBS not available."
- Local-app name collides with a known NET/ROM node alias — local app wins (it's
  more specific / intentional); document the precedence.
- A neighbor crosslink arrives on the node SSID — handled by the transport's
  `is_direct_neighbor` classifier (N0.5), NOT the user-node branch. Verify the
  branch order doesn't shadow it (crosslink classification is on `'C'` in the
  transport; the `_on_connection` node branch only sees connections that reached
  the engine as user sessions).
- `=>` native landing + a user typing `C <our own node>` — local-loop guard
  (N2) already covers "that's this node."

## Tests (offline)
- Called-SSID dispatch: connect to node SSID → `NetromNode` runs; to BBS SSID →
  BBS; to a service SSID → service; precedence order.
- `C BBS` / `C <svc>` runs the local app and returns to `=>` on exit; unknown
  local app falls through to NET/ROM resolution.
- Exit contract: native landing BYE ⇒ conn closed; `@` entry BYE ⇒ back to menu.
- NODES self-advertisement uses the node SSID when `node_ssid` set; unchanged
  when unset.
- `connect_out` sources from the node SSID when set (assert the `'C'`
  `call_from`).
- Config: `node_ssid == bbs.ssid` rejected; unset = current behavior.

## Live validation (radiostation2)
1. Set `node_ssid: 5`, register `W6ELA-5`, deploy + restart.
2. Connect to **`W6ELA-5`** → land at `=>` (no BBS menu). `C BBS` → the BBS;
   exit → back to `=>`. `C <remote node>` → onward NET/ROM (direct/transit).
3. Connect to **`W6ELA-1`** → still the BBS (now "an app"); `@` still enters the
   node from the menu.
4. Watch NODES: neighbors learn `PALO:W6ELA-5`; Direwolf shows outbound
   crosslinks originating from `W6ELA-5`.
5. Confirm inbound neighbor crosslinks still classify correctly (N0.5) on the
   node SSID.

## Out of scope for N3 (→ N4)
- Per-callsign / auth-level **access control** on the gateway and apps.
- Loop/abuse guards (INTERLOCK, rate limits, circuit caps beyond N2's).
- Telnet-out / AXUDP transports.
- A rich application **menu** UI (N3 does the plumbing + `C <app>`; a pretty
  app list is polish).
- Web node dashboard.
- **Unified application / SSID map (config + web UI).** Today the SSID→role map
  lives in three places: `bbs.ssid` → the BBS, `netrom.node_ssid` → the node,
  and `services.routes.{SSID}` → external EXEC programs. Conceptually (BPQ) the
  BBS and the node are *also* applications-on-SSIDs, so an operator would like
  one place — the `bbs.yaml` and the web **Services** config UI — to see and own
  the whole map. N3 keeps them separate (the node SSID is registered + dispatched
  independently of the services table; `_build_netrom_apps` exposes the BBS +
  services as the node's `C <app>` registry). N4 options, cheapest first:
  1. a **read-only unified SSID map** (startup log line + a web panel listing
     BBS / node / each service by SSID), no schema change; or
  2. **one applications table** with a `type: bbs|node|exec` discriminator —
     teach `ServiceDispatcher` internal types and extend `Services.vue`. This is
     the fuller BPQ model but conflates internal handlers with external EXEC and
     is a real refactor. *(Decision 2026-08-04: land N3 wire behavior first,
     live-validate, then do this in N4.)*

## Files (anticipated)
- `bbs/core/engine.py` — node-SSID branch in `_on_connection`; register the node
  SSID; build the local-app registry; wire `set_netrom_node_call`.
- `bbs/netrom/node.py` — local-app resolution in `cmd_connect`; `_run_local_app`;
  node-identity banner; app registry on the constructor.
- `bbs/plugins/node/node.py` — pass the app registry through `bind` /
  `handle_session`.
- `bbs/transport/base.py` + `bbs/transport/agwpe.py` —
  `set_netrom_node_call`; source `connect_out` + NODES broadcast from it;
  register the node SSID.
- `bbs/netrom/router.py` — node identity (self-advertisement) uses the node SSID.
- `bbs/config.py` + `config/bbs.yaml.example` — `netrom.node_ssid` + docs.
- Tests across engine / node / agwpe / router / config.
