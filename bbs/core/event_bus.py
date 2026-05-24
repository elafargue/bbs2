"""
bbs/core/event_bus.py — Lightweight async publish/subscribe bus for
inter-plugin communication.

Usage
-----
Publish an event from any asyncio context (plugin, engine, transport)::

    await bus.publish("heard.station", {
        "callsign": "W6OAK",
        "dest":     "APRS",
        "transport":"kiss_tcp",
        "via":      "WOODY*",
        "timestamp": 1748000000,
    })

Subscribe to events from a plugin's ``set_event_bus()`` or ``initialize()``::

    bus.subscribe("heard.station",      self._on_heard)
    bus.subscribe("session.connected",  self._on_connected)
    bus.subscribe("session.disconnected", self._on_disconnected)
    bus.subscribe("bulletin.new_message", self._on_bulletin)

All callbacks must be async coroutines that accept a single ``dict`` argument.

Topic matching
--------------
A subscriber registered on ``"session"`` receives events whose topic starts
with ``"session."`` (prefix match).  Exact-topic registration receives only
that exact topic.  Both can be combined:

    bus.subscribe("session", catch_all_session_events)
    bus.subscribe("session.connected", just_connect_events)

Delivery
--------
All matching subscribers are awaited concurrently via ``asyncio.gather``.
A subscriber that raises logs an error but does not affect other subscribers.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

SubscriberCallback = Callable[[dict[str, Any]], Awaitable[None]]


class PluginEventBus:
    """
    Async publish/subscribe event bus.

    All public methods must be called from the asyncio event loop.
    The bus itself is state-free beyond its subscriber registry — it holds
    no queues and performs no background tasks.
    """

    def __init__(self) -> None:
        # Mapping of registered topic → list of callbacks.
        # Using defaultdict so subscribe() never KeyErrors.
        self._subscribers: dict[str, list[SubscriberCallback]] = defaultdict(list)

    # ── Subscription management ───────────────────────────────────────────────

    def subscribe(self, topic: str, callback: SubscriberCallback) -> None:
        """
        Register *callback* to be called when an event is published on *topic*
        (or any sub-topic, e.g. subscribing to ``"session"`` receives both
        ``"session.connected"`` and ``"session.disconnected"``).

        Registering the same callback for the same topic twice is a no-op.
        """
        if callback not in self._subscribers[topic]:
            self._subscribers[topic].append(callback)
            logger.debug("Bus: subscribed %s to %r", _cb_name(callback), topic)

    def unsubscribe(self, topic: str, callback: SubscriberCallback) -> None:
        """
        Remove a previously registered callback.  Silent if not registered.
        """
        try:
            self._subscribers[topic].remove(callback)
            logger.debug("Bus: unsubscribed %s from %r", _cb_name(callback), topic)
        except ValueError:
            pass

    # ── Publishing ────────────────────────────────────────────────────────────

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """
        Deliver *payload* concurrently to all subscribers whose registered
        topic is an exact match or a prefix of *topic*.

        Example: ``publish("session.connected", {...})`` fires callbacks
        registered under both ``"session"`` and ``"session.connected"``.

        Subscriber exceptions are logged and swallowed — a crashing subscriber
        never prevents other subscribers from receiving the event.
        """
        targets: list[SubscriberCallback] = []
        for sub_topic, callbacks in self._subscribers.items():
            # Exact match OR the published topic extends the registered prefix.
            if topic == sub_topic or topic.startswith(sub_topic + "."):
                targets.extend(callbacks)

        if not targets:
            return

        async def _call_safe(cb: SubscriberCallback) -> None:
            try:
                await cb(payload)
            except Exception:
                logger.exception(
                    "Event bus: subscriber %s raised on topic %r",
                    _cb_name(cb),
                    topic,
                )

        # Build the list before calling gather so we can close any orphaned
        # coroutines if the loop is already shutting down (RuntimeError from
        # asyncio internals).  Calling .close() on an already-stepped coroutine
        # is a no-op, so this is safe for all items in the list.
        coros = [_call_safe(cb) for cb in targets]
        try:
            await asyncio.gather(*coros)
        except RuntimeError:
            for coro in coros:
                coro.close()

    # ── Introspection ─────────────────────────────────────────────────────────

    def subscriber_count(self, topic: str | None = None) -> int:
        """Return total subscriber count, optionally filtered by exact topic."""
        if topic is not None:
            return len(self._subscribers.get(topic, []))
        return sum(len(cbs) for cbs in self._subscribers.values())

    def __repr__(self) -> str:
        topics = sorted(
            t for t, cbs in self._subscribers.items() if cbs
        )
        return f"<PluginEventBus topics={topics!r}>"


def _cb_name(cb: SubscriberCallback) -> str:
    """Best-effort human-readable name for a callback (for logging)."""
    qualname = getattr(cb, "__qualname__", None)
    module   = getattr(cb, "__module__", "?")
    if qualname:
        return f"{module}.{qualname}"
    return repr(cb)
