"""
tests/test_event_bus.py — Unit tests for bbs.core.event_bus.PluginEventBus.

Coverage
--------
- Basic subscribe / publish round-trip
- Prefix-based topic matching  ("session" fires on "session.connected")
- Exact topics do NOT cross-contaminate each other
- Multiple subscribers called concurrently
- Subscriber exception is isolated (others still receive the event)
- Unsubscribe removes a single callback
- Duplicate subscribe is idempotent (callback fires exactly once)
- Publish with no subscribers is a clean no-op
- subscriber_count() helper
- __repr__
"""
from __future__ import annotations

import asyncio

import pytest

from bbs.core.event_bus import PluginEventBus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bus() -> PluginEventBus:
    return PluginEventBus()


# ── Basic subscribe / publish ─────────────────────────────────────────────────

async def test_basic_subscribe_and_publish():
    bus      = _bus()
    received = []

    async def cb(payload):
        received.append(payload)

    bus.subscribe("test.topic", cb)
    await bus.publish("test.topic", {"x": 1})

    assert received == [{"x": 1}]


async def test_publish_delivers_exact_payload():
    bus     = _bus()
    payloads = []

    async def cb(p):
        payloads.append(dict(p))  # copy so mutations don't affect us

    bus.subscribe("t", cb)
    await bus.publish("t", {"a": "hello", "b": [1, 2]})

    assert payloads[0] == {"a": "hello", "b": [1, 2]}


# ── Topic matching ────────────────────────────────────────────────────────────

async def test_prefix_match_fires_for_subtopic():
    """Subscribing to 'session' must fire on 'session.connected'."""
    bus  = _bus()
    hits = []

    async def cb(p):
        hits.append(p["type"])

    bus.subscribe("session", cb)
    await bus.publish("session.connected",    {"type": "connected"})
    await bus.publish("session.disconnected", {"type": "disconnected"})

    assert hits == ["connected", "disconnected"]


async def test_prefix_does_not_fire_on_unrelated_topic():
    """'session' subscriber must not fire on 'sessions' or 'other.session'."""
    bus  = _bus()
    hits = []

    async def cb(p):
        hits.append(p)

    bus.subscribe("session", cb)
    await bus.publish("sessions",           {"should": "not arrive"})
    await bus.publish("other.session",      {"should": "not arrive"})
    await bus.publish("session_connected",  {"should": "not arrive"})

    assert hits == []


async def test_exact_subscriber_does_not_get_sibling_subtopic():
    """'session.connected' subscriber must not see 'session.disconnected'."""
    bus  = _bus()
    hits = []

    async def cb(p):
        hits.append(p["type"])

    bus.subscribe("session.connected", cb)
    await bus.publish("session.connected",    {"type": "connected"})
    await bus.publish("session.disconnected", {"type": "disconnected"})

    assert hits == ["connected"]


async def test_prefix_and_exact_both_fire():
    """Both a prefix subscriber and an exact subscriber get the same event."""
    bus  = _bus()
    hits = []

    async def exact_cb(p):
        hits.append("exact")

    async def prefix_cb(p):
        hits.append("prefix")

    bus.subscribe("heard.station", exact_cb)
    bus.subscribe("heard",         prefix_cb)
    await bus.publish("heard.station", {})

    assert sorted(hits) == ["exact", "prefix"]


# ── Multiple subscribers ──────────────────────────────────────────────────────

async def test_multiple_subscribers_all_called():
    bus      = _bus()
    received = []

    for i in range(5):
        async def make_cb(idx=i):
            async def cb(p):
                received.append(idx)
            return cb
        bus.subscribe("ev", await make_cb())

    await bus.publish("ev", {})
    assert sorted(received) == list(range(5))


async def test_concurrent_delivery_all_called_with_awaiting_cb():
    """asyncio.gather fires all subscribers even when one awaits an event."""
    bus  = _bus()
    done = []
    gate = asyncio.Event()
    gate.set()  # allow slow to proceed immediately

    async def slow(p):
        await gate.wait()
        done.append("slow")

    async def fast(p):
        done.append("fast")

    bus.subscribe("ev", slow)
    bus.subscribe("ev", fast)

    await bus.publish("ev", {})
    assert sorted(done) == ["fast", "slow"]


# ── Error isolation ───────────────────────────────────────────────────────────

async def test_crashing_subscriber_does_not_block_others():
    bus     = _bus()
    good    = []

    async def bad_cb(p):
        raise RuntimeError("intentional crash")

    async def good_cb(p):
        good.append(p)

    bus.subscribe("ev", bad_cb)
    bus.subscribe("ev", good_cb)

    # Should not raise
    await bus.publish("ev", {"ok": True})

    assert good == [{"ok": True}]


async def test_crashing_subscriber_does_not_raise_to_caller():
    bus = _bus()

    async def bad_cb(p):
        raise ValueError("boom")

    bus.subscribe("ev", bad_cb)
    # publish() must return normally
    await bus.publish("ev", {})  # no exception


# ── Unsubscribe ───────────────────────────────────────────────────────────────

async def test_unsubscribe_stops_delivery():
    bus  = _bus()
    hits = []

    async def cb(p):
        hits.append(p)

    bus.subscribe("t", cb)
    await bus.publish("t", {"n": 1})

    bus.unsubscribe("t", cb)
    await bus.publish("t", {"n": 2})

    assert hits == [{"n": 1}]


async def test_unsubscribe_unknown_is_silent():
    """Unsubscribing a callback that was never registered must not raise."""
    bus = _bus()

    async def cb(p):
        pass

    bus.unsubscribe("no.such.topic", cb)  # should not raise


# ── Idempotent subscribe ──────────────────────────────────────────────────────

async def test_duplicate_subscribe_fires_once():
    bus   = _bus()
    count = []

    async def cb(p):
        count.append(1)

    bus.subscribe("t", cb)
    bus.subscribe("t", cb)  # second registration is a no-op

    await bus.publish("t", {})

    assert sum(count) == 1


# ── No subscribers ────────────────────────────────────────────────────────────

async def test_publish_with_no_subscribers_is_noop():
    bus = _bus()
    # must return without error
    await bus.publish("ghost.topic", {"data": 42})


# ── subscriber_count ─────────────────────────────────────────────────────────

async def test_subscriber_count_total():
    bus = _bus()

    async def a(p): pass
    async def b(p): pass
    async def c(p): pass

    bus.subscribe("t1", a)
    bus.subscribe("t1", b)
    bus.subscribe("t2", c)

    assert bus.subscriber_count() == 3


async def test_subscriber_count_per_topic():
    bus = _bus()

    async def a(p): pass
    async def b(p): pass

    bus.subscribe("t1", a)
    bus.subscribe("t1", b)
    bus.subscribe("t2", a)

    assert bus.subscriber_count("t1") == 2
    assert bus.subscriber_count("t2") == 1
    assert bus.subscriber_count("t3") == 0


async def test_subscriber_count_decreases_after_unsubscribe():
    bus = _bus()

    async def cb(p): pass

    bus.subscribe("t", cb)
    assert bus.subscriber_count("t") == 1

    bus.unsubscribe("t", cb)
    assert bus.subscriber_count("t") == 0


# ── __repr__ ──────────────────────────────────────────────────────────────────

def test_repr_contains_topics():
    bus = _bus()

    async def cb(p): pass

    bus.subscribe("heard.station", cb)
    bus.subscribe("session",       cb)

    r = repr(bus)
    assert "heard.station" in r
    assert "session" in r
    assert "PluginEventBus" in r


def test_repr_empty():
    assert "PluginEventBus" in repr(_bus())
