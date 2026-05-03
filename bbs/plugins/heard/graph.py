"""
bbs/plugins/heard/graph.py — Shared graph-building utilities for the Heard plugin.

Imported by both the BBS plugin (heard.py) and the web API route
(server/routes/heard.py).  Contains only pure functions with no
framework dependencies so there is no circular-import risk.
"""
from __future__ import annotations


def confirmed_edges(src: str, via: str, bbs_call: str) -> list[tuple[str, str]]:
    """
    Extract confirmed (source → dest) hop pairs from a via path string.

    A digipeater sets the H-bit (*) only after it has relayed the frame,
    so all hops up to and including the last '*' are confirmed; everything
    after the last '*' is speculative and discarded.

    In practice, depending on the transport, '*' may appear on every digi
    that forwarded the frame (kernel_ax25) or only the last one (AGWPE).
    The "up to and including the last *" rule handles both cases.

    Empty via  → direct reception → single edge (src, bbs_call).
    No '*' in via → cannot confirm any relay → empty list.
    """
    if not via:
        return [(src, bbs_call)]
    hops = [h.strip() for h in via.split(",") if h.strip()]
    last_star = max(
        (i for i, h in enumerate(hops) if h.endswith("*")),
        default=-1,
    )
    if last_star < 0:
        return []
    confirmed = [h.rstrip("*") for h in hops[: last_star + 1]]
    chain = [src] + confirmed + [bbs_call]
    return [(chain[i], chain[i + 1]) for i in range(len(chain) - 1)]
