# N1 — Outbound NET/ROM crosslink (`connect_out`) — implementation spec

Status: **N1 IMPLEMENTED & live-validated (2026-08-03, commit `6723fb7`).**
`W6ELA-8 → KF6ANX-4`: `SABM → UA → "*** CONNECTED With Station"`, then a clean
idle-reap DISC at 60s. This spec remains the authoritative, compaction-proof
capture of the N1 design; **the N2 (node command layer) spec is appended at the
bottom of this file.** Part of the NET/ROM node roadmap
(`~/.claude/plans/netrom-node-w6ela5.md`, milestones **N1**, **N2**).

Branch: `netrom-node` (off `main`; already has N0a `18318f3`, N0b `d9da5d6`).

## Goal
Let bbs2 **originate** an AX.25 crosslink to a NET/ROM neighbor (today it only
*accepts* them), wrap it in a `NetromCircuitManager`, and reuse an existing
crosslink if we already have one. The returned manager is what the future node
command layer (N2) calls `originate_circuit(dest_node, user)` on.

## Verified Direwolf protocol (source of truth: local `wb2osz/direwolf` @ 1.8-beta1, `src/server.c`)
- **Outbound connect** = app→engine `C` frame: `call_from` = our (registered)
  callsign, `call_to` = neighbor, port = agw_port, no data. Handler at
  `server.c:2304` (`case 'C'`): `callsigns[SOURCE]=call_from`,
  `callsigns[DEST]=call_to` → `dlq_connect_request(...)`. `pid=0xf0` on the
  connect is fine — **NET/ROM rides as separate `D` frames at `pid=0xCF`**, which
  `_make_netrom_writer` already sets per-frame. Do NOT need the `c`/`v` variants.
- **Registration**: the `call_from` must be `X`-registered (`server.c:2246`
  `case 'X'` → `dlq_register_callsign`). bbs2 already registers `self._local_call`
  in `start()`, so connect-out from it is covered. (When the node moves to
  `W6ELA-5` in N3, that SSID must be registered too.)
- **Confirmation** = engine→app `C` frame (`server_link_established`,
  `server.c:1122-1140`): **`call_from=remote`, `call_to=us` for BOTH directions**
  — address order does NOT distinguish inbound from outbound. The ONLY
  discriminators are:
  - payload `*** CONNECTED With Station <call>` → **we** initiated (`server.c:1137`)
  - payload `*** CONNECTED To Station <call>`  → **inbound** (`server.c:1133`)
- **Failure / timeout** = engine→app `d` frame (`server_link_terminated`,
  `server.c:1187`): `*** DISCONNECTED RETRYOUT With <call>` (timeout) or
  `*** DISCONNECTED From Station <call>` (normal disconnect).
- **Connected data** = `D` frames both ways.

## Discrimination rule (the crux)
Because Direwolf's `C` confirmation has `call_from=remote` for BOTH inbound and
outbound, we cannot tell them apart by address. Solution: **track pending
outbound connects** and match the confirmation to them; corroborate with the
`"CONNECTED With"` string.

```
key = (agw_port, remote_call.upper())
on 'C':  if key in self._pending_connects  → OUR outbound confirmation
         else                              → inbound (existing behavior, unchanged)
on 'd':  if key in self._pending_connects  → connect failed/RETRYOUT → reject
         else                              → inbound teardown (existing, unchanged)
```

## Implementation shape (4 small, individually-tested steps)

### 1. `self._sock_writer` lifecycle (out-of-loop sends)
Today the AGWPE socket `StreamWriter` is a local in `start()` passed into
`_read_loop(reader, writer)` (`agwpe.py:558/621`); every send happens *inside*
the read loop. `connect_out` fires from *outside* it. So:
- set `self._sock_writer = writer` (and reuse `self._drain_lock`) after
  `open_connection` in `start()`;
- **clear it to `None` on TCP drop / before reconnect**, and **fail all pending
  connect futures** at that point (they'll never confirm).
- `connect_out` raises cleanly if `self._sock_writer is None` (not connected).

### 2. `connect_out()` + pending futures
```
self._pending_connects: dict[_SessionKey, asyncio.Future[NetromCircuitManager]]

async def connect_out(neighbor, *, timeout=30.0) -> NetromCircuitManager:
    key = (agw_port, neighbor.upper())
    # reuse: already have a crosslink to this neighbor → multiplex
    sess = self._sessions.get(key)
    if sess and sess.netrom_manager is not None:
        return sess.netrom_manager
    # coalesce: a connect already in flight to this neighbor
    if key in self._pending_connects:
        return await self._pending_connects[key]
    fut = loop.create_future(); self._pending_connects[key] = fut
    send  _build_frame(agw_port, "C", self._local_call, neighbor)  via self._sock_writer (+drain under _drain_lock)
    try:    return await asyncio.wait_for(fut, timeout)
    except TimeoutError:  self._pending_connects.pop(key, None); raise
    # (fut resolved/failed by the 'C'/'d' dispatch below)
```

### 3. dispatch `C` / `d` discrimination
- In the `C` handler (`agwpe.py:785`): **before** the existing inbound logic,
  `if key in self._pending_connects:` build the OUTBOUND crosslink —
  - `sess = _AGWPESession(neighbor, self._local_call, port, writer, drain_lock, write_timeout)`
  - `sess.netrom_manager = NetromCircuitManager(local_call=self._local_call,
    via_node=neighbor, ax25_writer=self._make_netrom_writer(sess),
    on_user_connect=self._on_connect, info_mtu=self._netrom_info_mtu,
    link_idle_timeout=self._netrom_link_idle_timeout)`
  - `self._sessions[key] = sess`
  - `self._pending_connects.pop(key).set_result(sess.netrom_manager)`
  - **return** — do NOT start a BBS session task (this crosslink is for
    originating; inbound circuits arriving on it are still handled by the
    manager's `on_user_connect`).
- In the `d` handler (`agwpe.py:917`): `if key in self._pending_connects:`
  `self._pending_connects.pop(key).set_exception(ConnectionError("RETRYOUT/…"))`
  then return (nothing else to tear down — the session was never created).

### 4. `base.py` interface
`async def connect_netrom(self, neighbor: str) -> Optional[NetromCircuitManager]:`
default returns `None`; AGWPE overrides with `connect_out`. (Name TBD —
`connect_netrom` reads well at the call site in N2.)

## Edge cases to cover
- **Reuse** returns the existing manager (inbound or outbound crosslink).
- **Coalesce** concurrent `connect_out` to the same neighbor onto one future.
- **Timeout** cleans up the pending entry and raises.
- **`d`/RETRYOUT** rejects the pending future.
- **TCP reconnect while pending** fails all pending futures + clears `_sock_writer`.
- **Duplicate `C`** interplay: the existing inbound duplicate-`C` teardown
  (`agwpe.py:794`) must not fire for an outbound confirmation — the pending
  check runs first and returns, so it won't.
- The connect's `from` must be registered — assert/ensure `self._local_call`
  (later the node SSID) is in the registered set.

## Tests (fake AGWPE socket — the main offline quality lever)
A fake that lets a test drive the byte stream deterministically:
- `connect_out` sends a well-formed `C` (from/to/port correct), then a simulated
  `C` confirmation (`call_from=neighbor`, `"CONNECTED With Station"`) resolves it
  → returns a `NetromCircuitManager`; a subsequent `originate_circuit` works.
- Simulated `d`/RETRYOUT → `connect_out` raises `ConnectionError`.
- Reuse: second `connect_out` to a neighbor with an existing crosslink returns
  the same manager without sending a new `C`.
- Coalesce: two concurrent `connect_out` calls share one pending future / one `C`.
- Timeout when no confirmation arrives.
- `base.py` default returns `None`.

## Return contract
`connect_out` → a **connected** `NetromCircuitManager` for the crosslink to
`neighbor`. Caller (N2) then does `await mgr.originate_circuit(dest_node, user)`
and bridges the user session to the returned circuit. Idle teardown is already
handled by the reaper (`link_idle_timeout`).

## Out of scope for N1
The node command layer, `C <alias>` parsing, and the two-circuit session bridge
— those are **N2**. Connect-via-digipeaters (`v`) is unnecessary: NET/ROM does
its own L3 routing, so we only ever connect to a direct adjacent neighbor and let
L3 forward.

## Live validation (after code)
On radiostation2, trigger an outbound connect to a known neighbor and watch the
monitor for `SABM → UA → "*** CONNECTED With Station <neighbor>"`, then a clean
idle-reap after the reaper window. Per repo practice: wire-protocol changes
prove themselves only on the air.

**DONE.** See status line at top; validated `W6ELA-8 → KF6ANX-4` (alias JOHN).

---
---

# N2 — Node command layer & session bridge — implementation spec

Status: **IMPLEMENTED & live-validated (2026-08-03).** `bbs/netrom/node.py` +
`bbs/plugins/node/`; entry is the `@` BBS-menu item. Live over telnet:
`@` → `MH` (neighbors) → `C ROCK` → **direct 1-hop crosslink to K6FB-5**, bridged
into its K-net node menu (proper multi-line output), remote `bye` → ReConnect to
`=>`, `quit` → back to the BBS menu.

Refinements found during live testing (all landed):
- **Bridge does line-ending translation, NOT raw passthrough** (see bridge
  section) — the original "raw" note was wrong for web/TCP users.
- **`MH`** lists `router.adjacent_neighbors` (nodes whose NODES we hear directly),
  not `is_direct` dest routes (which aren't persisted → empty after a restart).
- **connect (originate_circuit) timeout raised 30s → 60s** — a multi-hop transit
  CONNECT ACK can take ~45s; 30s dropped the circuit just before the ACK arrived.
- **N0 direct-heard preference** (companion change): `best_neighbor_for` prefers a
  direct crosslink to a destination the heard plugin reports we've heard directly
  on the air (`heard_direct_within`, seeded from the DB so it survives restarts),
  instead of a slow transit route — this is what makes `C <node>` fast + reliable
  right after a restart. Config: `netrom.direct_heard_ttl_minutes` (default 60).

Roadmap milestone **N2** (`~/.claude/plans/netrom-node-w6ela5.md`).
Branch: `netrom-node`.

## Goal
Introduce **`NetromNode`** — the switch/command layer that sits *above* the
transport + circuit layers. It presents a `=>` command prompt on a connected
session and lets a user:
  (a) reach local applications (the BBS, ax25d-style services), and
  (b) **connect onward** to other nodes/BBSes via an outbound NET/ROM circuit,
      using the N1 stack (`connect_netrom` → `originate_circuit`) proven above.

N2 delivers: the command loop + standard verb vocabulary, the `C <alias|call>`
resolve→route→crosslink→originate→**two-circuit bridge** flow, and ReConnect on
far-end close. It is **transport-agnostic** (deals only in "a link to neighbor X"
+ circuits), so AXUDP / Telnet-out slot in later (N4) with no switch-core change.

## What N1 handed us (the building blocks — all proven on air)
- `transport.connect_netrom(neighbor) -> Optional[NetromCircuitManager]`
  (AGWPE override does the outbound AX.25 connect; reuse + coalesce built in;
  raises `ConnectionError`/`asyncio.TimeoutError` on failure; base default None).
- `mgr.originate_circuit(dest_node_call, user_call, *, proposed_window=4,
  timeout=30) -> NetromCircuit`; raises `asyncio.TimeoutError` /
  `ConnectionRefusedError`. The returned `NetromCircuit` exposes
  `.reader` (`asyncio.StreamReader`) and `.writer` (`_NetromCircuitWriter`,
  duck-typed StreamWriter: `write`/`drain`/`close`/`wait_closed`), and
  `await circuit.wait_closed()`.
- Idle-crosslink reaper handles teardown of the crosslink when its last circuit
  closes (and now self-reaps a bare crosslink), so N2 never manages crosslink
  lifetime directly — it just closes its circuit.

## Existing pieces to reuse (verified in tree)
- **Router resolution** (`bbs/netrom/router.py`):
  - `get_route(dest)` / `get_routes(dest)` — accept **callsign OR alias**,
    case-insensitive; return `RouteEntry` (fields: `dest_call`, `alias`,
    `via_call` = our adjacent neighbor, `quality`, `is_direct`).
  - `best_neighbor_for(dest, *, min_quality=1) -> str | None` — the **adjacent
    neighbor callsign** to crosslink to (prefers a direct link to dest; else the
    best transit route). This is the N0b next-hop API — exactly the `neighbor`
    arg for `connect_netrom`.
  - `is_direct_neighbor(call)`, `adjacent_neighbors`.
- **Terminal** (`bbs/core/terminal.py`): wraps a `Connection`'s reader/writer.
  `await term.readline(max_len, timeout)` for line-mode command input;
  `send/sendln/prompt/note/warn/error`, `paginate` for listings. Build one on
  the node session for the `=>` prompt.
- **Byte-pump model** (`bbs/services/bridge.py::run_service`): two pump tasks +
  symmetric teardown via `asyncio.wait(..., FIRST_COMPLETED)`. The N2 bridge is
  the same shape but between two Connection-like endpoints, **with line-ending
  translation to the user's terminal EOL** (see the bridge section — the
  original "raw passthrough" idea was wrong for web/TCP users).
- **Connection dispatch** (`bbs/core/engine.py::_on_connection`): services match
  by called SSID (EXEC/REFUSE/PASS) → BBS session. N3 will land `=>` natively on
  the node SSID here; N2 uses an interim entry point (below).

## `NetromNode` shape
```
class NetromNode:
    def __init__(self, *, term, user_call, node_call, node_alias,
                 router, transport,          # crosslink-capable transport (AGWPE for now)
                 min_auth_for_connect, cfg): ...

    async def command_loop(self):
        # print node banner, then loop:
        #   line = await term.readline(...); verb,arg = parse(line)
        #   dispatch to handler; 'B'/'BYE'/'Q' → return (caller disconnects)

    async def cmd_connect(self, target): ...   # the crux — see below
    async def cmd_nodes(self, pattern): ...     # from router.routing_table
    async def cmd_routes(self, target): ...     # router.get_routes()
    async def cmd_users(self): ...              # active gateway circuits on this node
    async def cmd_mheard(self): ...             # heard plugin data
    async def cmd_info(self): ...               # node identity banner
    async def cmd_ports(self): ...              # transports (minimal)
    async def cmd_help(self): ...
```

`transport` for N2 is the single crosslink-capable transport (AGWPE). Multi-
transport next-hop selection (choose the transport that owns `neighbor`) is N4;
N2 assumes the router's neighbors are all reachable via the one AGWPE transport
(true today — the router is fed exclusively by AGWPE-observed NODES).

## Command vocabulary (BPQ-style, uppercase-prefix abbreviation)
A verb matches on its shortest **unambiguous** uppercase prefix (`C`, `CO`,
`CON`, … → CONNECT). Small static dispatch table.

| Verb | Aliases | N2 scope |
|------|---------|----------|
| `CONNECT <alias\|call>` | `C` | **primary** — gateway to another node |
| `NODES [pat]` | `N` | list known nodes (router table) |
| `ROUTES [call]` | `R` | routes/alternates to a dest |
| `USERS` | `U` | active gateway circuits on this node |
| `INFO` | `I` | node identity/banner |
| `MHEARD` | `MH` | recently heard stations (heard plugin) |
| `PORTS` | `P` | transports/ports (minimal) |
| `BYE` | `B`,`Q`,`QUIT` | disconnect |
| `HELP` | `?`,`H` | command help |

`N`/`R`/`MH` are backed by existing router + heard data — bbs2's differentiator.

## `C <alias|call>` flow (the crux)
```
async def cmd_connect(self, target):
    # 1. Resolve target → destination node + outbound neighbor
    route = self.router.get_route(target)                 # alias or callsign
    if route is None:  → term "Unknown node: {target}";  return
    dest_call = route.dest_call
    if dest_call.upper() == self.node_call.upper():       # local-loop guard
        → term "That's this node.";  return               # (N3: route to local app)
    neighbor = self.router.best_neighbor_for(target, min_quality=self.cfg.min_quality)
    if neighbor is None:  → term "No route to {dest_call}"; return

    # 2. Auth gate (a gateway is abusable — minimal check now, full ACL in N4)
    if not self._may_connect():  → term "Not authorized to connect out."; return

    # 3. Establish/reuse the crosslink (N1 — reuse+coalesce are built in)
    term "Connecting to {alias} ({dest_call}) via {neighbor} ..."
    try:    mgr = await self.transport.connect_netrom(neighbor)
    except (ConnectionError, asyncio.TimeoutError):
            → term "Link to {neighbor} failed."; return
    if mgr is None:  → term "No crosslink transport for {neighbor}."; return

    # 4. Originate the L3 circuit to dest THROUGH that crosslink
    try:    circuit = await mgr.originate_circuit(dest_call, self.user_call,
                                                  timeout=self.cfg.connect_timeout)
    except asyncio.TimeoutError:      → term "{alias} did not answer."; return
    except ConnectionRefusedError:    → term "{alias} refused the connection."; return

    # 5. Bridge until one side closes
    term "*** Connected to {alias}"
    near_closed = await self._bridge(circuit)

    # 6. ReConnect: far-end closed → back to =>  (near-end close ends the node too)
    if not near_closed:
        term "*** Reconnected to {node_alias}"
    # else: user vanished — command_loop's readline will EOF and exit
```
**neighbor vs dest_call is the whole point of N0b:** `connect_netrom(neighbor)`
opens the AX.25 crosslink to the *adjacent* node; `originate_circuit(dest_call)`
puts `dest_call` in the L3 header so the neighbor L3-routes it onward. Direct
node → they're equal; transit → they differ.

## Two-circuit bridge (byte-accurate, line-ending-translated)
Endpoints: **A = user session** (`term` reader/writer, i.e. the inbound
`Connection`) and **B = outbound circuit** (`circuit.reader` / `circuit.writer`).
```
async def _bridge(self, circuit) -> bool:   # returns True iff NEAR (user) closed
    a_reader, a_writer = self._conn.reader, self._conn.writer
    b_reader, b_writer = circuit.reader, circuit.writer
    async def a_to_b():   # user → far end
        while (data := await a_reader.read(CHUNK)): b_writer.write(self._to_far(data)); await b_writer.drain()
    async def b_to_a():   # far end → user
        while (data := await b_reader.read(CHUNK)): a_writer.write(self._to_user(data)); await a_writer.drain()
    down = task(a_to_b); up = task(b_to_a)
    done, _ = await asyncio.wait({down, up}, return_when=FIRST_COMPLETED)
    near_closed = down in done         # a_to_b finished first ⇒ user EOF'd
    # teardown: close the far circuit, cancel pumps, drain B→A tail to the user
    circuit.writer.close()             # NETROM DISC REQ to far end (crosslink self-reaps)
    for t in (up, down):
        if not t.done(): t.cancel()
    await asyncio.gather(up, down, return_exceptions=True)
    return near_closed
```
Notes:
- **Line-ending translation** (CORRECTED from the original "raw passthrough" —
  which was wrong for non-AX.25 users). The far end (a NET/ROM node) speaks bare
  `CR`; a web/TCP user wants `CRLF`. So `_to_user()` maps the far end's `CR`/`CRLF`
  to the user terminal's EOL (else its output collapses onto one line), and
  `_to_far()` normalizes the user's `CRLF`/`LF` to bare `CR` (else the far node
  re-prompts on a stray `LF`). For an AX.25/RF user (EOL = `CR`) both are
  near no-ops. Found on the first web live test. `CHUNK` = 4096.
- While bridged the `=>` interpreter is **suspended** — every user byte goes to
  the far end (including things that look like commands). N2 has **no local
  escape**; return to `=>` happens only on far-end disconnect (BPQ default).
  A local force-return escape (`+++`-style) is a documented later nicety.
- The bridge owns `a_reader` exclusively while active; `term.readline()` (which
  reads the same underlying `StreamReader`) resumes only after the bridge ends.
- `circuit.writer.close()` initiates the NETROM DISC; the circuit's
  `_remove_circuit` arms the idle reaper, which reaps the now-circuit-less
  crosslink after `link_idle_timeout` (so a series of `C`/ReConnect reuses the
  same crosslink and only reaps once idle — cheap).

## ReConnect
- **Default: always ReConnect** — on far-end close, print `*** Reconnected to
  <node_alias>` and drop back to `=>`. `BYE` exits the node loop (the caller then
  decides: back to the BBS menu, or disconnect — see the exit contract above).
- Per-connect `stay/drop` override (BPQ `s`/`d`) is a small follow-up; capture
  the flag now (`cmd.reconnect: bool`) but default True.

## Entry point — BBS → node (a PERMANENT feature, not just a scaffold)
The node is reachable in N2 via a **BBS main-menu command** (e.g. `@` / a "Node"
menu item) that constructs a `NetromNode` on the **live session** (reuse
`session.conn` + a `Terminal`) and runs `command_loop()`.

This started as the interim way to test the node before N3's SSID work, but it is
**worth keeping permanently**: a user connects to the BBS, reads mail / bulletins,
then decides to hop onto the node interface and **travel onward** to another
node/BBS — all within one AX.25 session. "BBS as the front door, with the whole
NET/ROM network reachable from inside it" is a genuinely nice UX and a bbs2 edge.
So:
- **N2**: `@` from the BBS menu → `=>`; on `BYE`, **return to the BBS menu**
  (the caller decides what "exit the node loop" means — see below).
- **N3** *adds* (does not remove) a **native** `=>` landing when a user connects
  directly to the node SSID (`W6ELA-5`); there the BBS/services become
  "applications" reachable via `C BBS` etc. The BBS-menu path and the native-SSID
  path coexist and share the same `NetromNode`.

**Exit contract:** `command_loop()` simply *returns* on `BYE`/`Q`. The **caller**
maps that to the right action — BBS-menu entry → back to the BBS menu; native
node-SSID landing (N3) → disconnect the session. `NetromNode` itself never closes
the connection, keeping it entry-agnostic.

Bonus: this keeps N2 self-contained and independently testable + live-validatable
with **zero SSID plumbing**; the SSID/identity work (engine registration +
config) stays cleanly in N3.

## Access control (minimal now, full in N4)
- N2: a single gate — `min_auth_for_connect` (default: **identified callsign
  required** to use `C`; local listing verbs open). Reuse `session.auth.level`.
- Full per-callsign ACL, INTERLOCK "don't route back out the port it came in
  on", rate limits, circuit caps → **N4**.
- N2 does include: the **local-loop guard** (refuse `C` to our own node
  call/alias) and a **node-wide gateway-circuit cap** (budget ≈ 2× users, since
  each gateway = inbound + outbound circuit).

## Files
- **New `bbs/netrom/node.py`** — `NetromNode` (command loop, verb dispatch +
  abbrev, `cmd_connect` flow, `_bridge`, ReConnect, auth gate, loop guard).
- `bbs/core/session.py` (or the menu module) — interim entry command.
- `bbs/core/engine.py` — construct/inject the router + crosslink-capable
  transport into the node path; wire the entry command.
- `config/bbs.yaml.example` — new `netrom:` node keys (min_quality,
  connect_timeout, min_auth_for_connect, gateway cap) documented.

## Tests (offline lever — fake transport/manager/circuit)
Mirror the N1 fake-socket approach. A fake transport whose `connect_netrom`
returns a fake `NetromCircuitManager` whose `originate_circuit` returns a fake
circuit backed by in-memory `StreamReader`s + a capture writer, so the bridge is
driven deterministically:
- **verb parsing/abbrev**: `C`/`CO`/`CONNECT` → connect; ambiguity handling.
- **resolve→connect→originate happy path**: `C JOHN` → `get_route` +
  `best_neighbor_for` called with the right args → `connect_netrom(neighbor)` →
  `originate_circuit(dest_call, user)`.
- **bridge byte-accuracy**: feed bytes both directions, assert exact
  passthrough, no reordering/translation.
- **far-end close → ReConnect**: circuit EOF → bridge returns, user sees
  `*** Reconnected`, prompt resumes; near-end close → circuit closed + node ends.
- **error paths**: unknown node, no route (`best_neighbor_for` None), refused
  (`ConnectionRefusedError`), timeout (`asyncio.TimeoutError`), link fail
  (`ConnectionError`), unauthorized, local-loop guard, cap reached.

## Live validation (after code)
On radiostation2: reach the node (`@` from the BBS), `C JOHN` → exchange bytes
with KF6ANX-4's node prompt → have the far end disconnect → land back at `=>` →
`BYE`. Watch the monitor for the outbound crosslink (reuse if still up from a
prior `C`) and the L3 CONNECT REQ/ACK. Then confirm the idle reaper drops the
crosslink after the window once no circuits remain.

## Out of scope for N2 (→ N3 / N4)
- Binding `=>` to `W6ELA-5` (`netrom.node_ssid`) + BBS/services as applications,
  NODES advertising the node SSID (**N3**).
- Full per-callsign ACL, INTERLOCK loop guard, rate limits (**N4**).
- Telnet-out / AXUDP gateways; multi-transport next-hop selection (**N4**).
- Web node dashboard (`NODES`/`ROUTES`/`MHEARD` + map/graph) (**N4**).
- Local bridge-escape sequence; per-connect stay/drop override (small follow-ups).

## Open questions to settle during build
- Exact column layout for `N`/`R`/`MH` (reuse heard/router `__str__` vs custom).
- Whether the interim entry is a menu item vs a hidden command (UX call).
- Multi-hop `C` (transit neighbor ≠ dest) — verify on air against a 2-hop dest.
