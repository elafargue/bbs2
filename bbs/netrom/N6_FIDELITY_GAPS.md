# N6 — NET/ROM fidelity gaps (post-N5 audit)

Status: **design capture.** After N5 landed, we audited the implementation
against the 1987 NET/ROM v1.3 manual (Software 2000 / WA8DED). This note records
the places where bbs2 is *not* literally by-the-book, so they don't get lost. It
is deliberately honest: some gaps are bugs-in-waiting, one is a pragmatic subset,
and one is a conscious architectural divergence we do **not** intend to close.

Branch: `netrom-node`. Builds on N0–N5.

The verdict from the audit, in one line: **strong on the wire-visible routing
protocol (Level 3), a pragmatic subset on the transport (Level 4), with one
deliberate divergence (bridge vs. transit relay).**

---

## Gap 1 — NODES broadcast frame fragmentation  ⟶ **N6a, DONE**

**Manual:** a node's routing-table broadcast is sent as a *series* of AX.25 UI
frames, each carrying the 7-byte header (`0xFF` + 6-byte source alias) followed
by as many 21-byte destination entries as fit in one frame. `(256 − 7) // 21 =`
**11 entries per frame**; a larger table is split across multiple frames, each
re-stamped with the header.

**bbs2 (pre-N6):** `router.build_nodes_payload()` returns a *single* payload
containing every entry. In polite-client mode (`advertise_self_only=True`, the
default) this is a harmless 7-byte header, so the gap is invisible today. But in
transit mode (`advertise_self_only=False`) a table over ~11 destinations
overflows one AX.25 info field; the TNC splits it at L2 and the header-less
second fragment is undecodable by the peer's NET/ROM stack — the same failure
class already documented at `agwpe.py:491` for oversize L3 INFO frames.

**Fix (N6a — done):** `router.build_nodes_payloads() -> list[bytes]` chunks the
entry list (shared `_nodes_entries()`) into ≤11-entry frames, each re-stamped
with the header; the AGWPE NODES loop sends one UI frame per chunk and stamps the
broadcast timestamp once per cycle. `build_nodes_payload()` (single-frame) is
retained for tests / small callers. The `NetromNodesBuilder` transport contract
is now `Callable[[], list[bytes]]`. Transit mode is now safe at scale.

---

## Gap 2 — Level 4 transport is a pragmatic subset (relies on AX.25)  ⟶ won't-fix (by design)

bbs2 rides NET/ROM L3/L4 frames *inside* AX.25 connected mode on each
point-to-point crosslink, and leans on AX.25 for reliability. Correct on the wire
for every frame it emits (CONNECT REQ/ACK, DISC REQ/ACK, INFO/INFO-ACK,
V(S)/V(R)/V(A), window negotiation, outbound window flow-control), but it
intentionally omits parts of the manual's L4:

- **No T1 retransmit timer.** AX.25 connected mode retransmits; the manual gives
  L4 its own retry timer. (`circuit.py:39`)
- **No CHOKE/NAK flow control.** `FLAG_CHOKE` is used only to *refuse* a circuit,
  not for the manual's choke-based backpressure. (`circuit.py:46`)
- **Greedy immediate ACK** instead of the delayed-ack (T2) scheme.

These do not affect interop on real AX.25 links (all NET/ROM crosslinks are
point-to-point) and are the standard "let AX.25 do the hard part" simplification.
Recorded for completeness; not planned.

---

## Gap 3 — Bridge, not L3 transit relay  ⟶ deliberate architectural divergence

**Manual:** L4 is **end-to-end** between the origin and destination nodes.
Intermediate nodes forward L3 packets hop-by-hop toward the destination,
decrementing **TTL**, running store-and-forward transit; the two endpoints share
one L4 circuit.

**bbs2:** terminates an L4 circuit on *each* crosslink hop and **bridges the
user's bytes between hops at the node layer** (the two-circuit bridge in
`node.py`). `circuit.py:dispatch()` drops frames for unknown circuits — it never
relays a third-party frame not addressed to us. TTL exists in our headers
(`_DEFAULT_TTL = 25`) but we never run the decrement-and-forward loop, because we
are never a transit relay.

This is the classic **gateway node** model: a user connects to us, we connect
them onward. User-visible behaviour is identical, it interoperates cleanly, and
it is arguably safer (INTERLOCK/gateway guards apply per hop). It is **not** the
manual's L3 store-and-forward transit, and we do not intend to make it so unless
a concrete need appears — true L3 transit forwarding with TTL is a large lift for
little practical gain given the bridge already delivers the onward connection.

---

## Summary table

| # | Area | Manual | bbs2 | Disposition |
|---|------|--------|------|-------------|
| 1 | NODES frame fragmentation | multi-frame, 11 entries/frame | multi-frame (≤11/frame) | **N6a — done** |
| 2 | L4 transport (T1/CHOKE/T2) | full L4 timers + choke | AX.25-backed subset | won't-fix (by design) |
| 3 | L3 transit / end-to-end L4 | store-and-forward relay | per-hop bridge (gateway) | deliberate divergence |

Level 3 routing/advertisement itself (two-table model, composed quality,
obsolescence lifecycle, PARMS, NODES wire format, trivial-loop guard,
neighbour-list adjacency) is by-the-book as of N5 — no gaps recorded there beyond
Gap 1's framing.
