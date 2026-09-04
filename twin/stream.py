"""SSE broker (section 7 `/api/twin/stream`, Phase 7).

An in-process pub/sub: `publish()` is called by twin/jobs.py after every
`compute_state` run and by the report-approval hook
(twin.ingest.internal_reports.register_approval_hook); `sse_stream()` is a
generator the route wraps in a streaming Flask response.

This is intentionally a plain in-memory broker, not Redis pub/sub -- the
twin has no message-bus dependency anywhere else, and C2 already restricts
this project to what runs identically on SQLite and PostgreSQL with no extra
infrastructure. The tradeoff (documented in section 15): each connection
pins one worker for its lifetime, so a sync Gunicorn deployment needs a
`gevent`/`eventlet` worker class, or the client's 60s poll fallback carries
the load instead.
"""

import json
import logging
import queue
import threading
import time

log = logging.getLogger("twin.stream")

_lock = threading.Lock()
_subscribers = {}
_next_id = 0

#: Sent on the wire to keep intermediating proxies from closing an idle
#: connection, and to give the client a way to detect a dead stream.
HEARTBEAT_INTERVAL_S = 15
#: Cap on a subscriber's backlog; a full queue means a wedged client, and it
#: is better to drop that client's oldest events than to block publish() for
#: every other subscriber (publish must stay non-blocking, C1's spirit).
QUEUE_MAXSIZE = 50


def subscribe(city=None, zone=None):
    global _next_id
    with _lock:
        _next_id += 1
        sub_id = _next_id
        _subscribers[sub_id] = (city, zone, queue.Queue(maxsize=QUEUE_MAXSIZE))
    return sub_id


def unsubscribe(sub_id):
    with _lock:
        _subscribers.pop(sub_id, None)


def subscriber_count():
    with _lock:
        return len(_subscribers)


def publish(event_type, city=None, **payload):
    """Fan out one event to every matching subscriber. Never blocks (a full
    subscriber queue is dropped from, not waited on) and never raises."""
    event = {"type": event_type, "city": city, "ts": time.time(), **payload}
    with _lock:
        targets = list(_subscribers.items())

    for sub_id, (sub_city, _sub_zone, q) in targets:
        if sub_city and city and sub_city != city:
            continue
        try:
            q.put_nowait(event)
        except queue.Full:
            log.warning("twin stream subscriber %s backlog full; dropping event", sub_id)


def _format_sse(event):
    return "event: %s\ndata: %s\n\n" % (
        event.get("type", "message"), json.dumps(event, default=str))


def sse_stream(city=None, zone=None, max_duration_s=None):
    """Generator yielding SSE-formatted text. Wrap in a Flask streaming
    Response with mimetype `text/event-stream`.
    """
    sub_id = subscribe(city=city, zone=zone)
    _, _, q = _subscribers.get(sub_id, (None, None, None))
    started = time.monotonic()
    try:
        yield ": connected\n\n"
        while True:
            elapsed = time.monotonic() - started
            if max_duration_s and elapsed > max_duration_s:
                break
            # Cap the wait at whatever's left of max_duration_s so the
            # duration limit is actually responsive, not just checked once
            # per HEARTBEAT_INTERVAL_S (15s) -- a caller passing a small
            # max_duration_s (tests; a proxy-imposed connection cap) must
            # not block for up to a full heartbeat past it.
            wait_s = HEARTBEAT_INTERVAL_S
            if max_duration_s:
                wait_s = max(0.0, min(wait_s, max_duration_s - elapsed))
            try:
                event = q.get(timeout=wait_s)
                yield _format_sse(event)
            except queue.Empty:
                if max_duration_s and (time.monotonic() - started) > max_duration_s:
                    break
                yield ": heartbeat\n\n"
    except GeneratorExit:
        pass
    finally:
        unsubscribe(sub_id)
