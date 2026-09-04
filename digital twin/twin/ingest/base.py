"""IngestAdapter ABC -- the one place C1 and C7 are enforced.

Every adapter in this package is a subclass of :class:`IngestAdapter`. The
base class, not the subclass, owns: the timeout, the disk TTL cache, the
retry, the ``TwinDataSnapshot`` audit row, and the guarantee that ``run()``
never raises. A subclass implements only ``fetch_raw()`` -- the part that
actually knows the shape of one external API -- and can trust that a bug in
some other adapter can never take the twin down.

Contract for subclasses::

    class MyAdapter(IngestAdapter):
        source_key = "my_source"

        def fetch_raw(self, **kwargs):
            # do the HTTP call(s); raise on failure -- run() catches it.
            # return a JSON-serialisable object.
            ...

        def neutral_value(self, **kwargs):
            # optional: what to return when there is no cache AND the
            # network is down. Defaults to None, meaning "the caller must
            # treat this sub-score as unmeasured" (see twin/scoring.py).
            return None

``run()`` returns ``(data, snapshot)``. ``snapshot.status`` is one of:

- ``"ok"``       -- fresh data from the network.
- ``"degraded"`` -- network failed; served the last cached value instead.
- ``"failed"``   -- network failed and no cache existed; ``data`` is
                    whatever ``neutral_value()`` returns (often ``None``).
"""

import hashlib
import json
import logging
import os
import time
from abc import ABC, abstractmethod

import requests

from .. import config
from .. import models as m

log = logging.getLogger("twin.ingest")


class IngestAdapter(ABC):
    #: TwinDataSnapshot.source_key / twin/config.py ALL_SOURCES entry.
    source_key = None

    #: Seconds a successful response stays valid as a cache fallback.
    cache_ttl_s = 900

    #: Per-attempt HTTP timeout (C1: <= 8s on the request path). Seed/weekly
    #: scripts that are not on a live request path may pass a larger value
    #: explicitly to run(); this default is what jobs.py uses.
    timeout_s = config.HTTP_TIMEOUT_S

    #: Retries for a transient failure (connection reset, 5xx, timeout).
    max_retries = 2
    retry_backoff_s = 1.5

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", "sentinel-twin/0.1 (+digital-twin-module)")

    # -- override in subclasses --------------------------------------------

    @abstractmethod
    def fetch_raw(self, **kwargs):
        """Do the network call(s). Raise on failure. Return JSON-able data."""
        raise NotImplementedError

    def neutral_value(self, **kwargs):
        """What to serve when there is no cache and the network is down."""
        return None

    def record_count(self, data):
        """How many records `data` represents, for the snapshot audit row."""
        if data is None:
            return 0
        if isinstance(data, (list, tuple)):
            return len(data)
        if isinstance(data, dict):
            return 1
        return 1

    # -- the part subclasses never need to touch -----------------------------

    def run(self, db, city_id=None, timeout_s=None, write_snapshot=True, **kwargs):
        """Fetch with cache-fallback-neutral degradation. Never raises.

        `db` is forwarded into `fetch_raw`'s kwargs automatically (as `db=`)
        so adapters that need a database session -- internal_reports.py, in
        particular -- can declare a `db` parameter without the caller having
        to pass it twice. Adapters that don't need it simply ignore it via
        `**_`.
        """
        assert self.source_key, "%s.source_key must be set" % type(self).__name__
        timeout_s = timeout_s if timeout_s is not None else self.timeout_s
        kwargs.setdefault("db", db)

        cache_key = self._cache_key(city_id, kwargs)
        started = time.monotonic()
        started_at = m.utcnow()
        error_message = None
        status = "ok"
        data = None

        # A cache entry still inside its TTL is not a fallback -- it is the
        # normal, intended result of caching, and must short-circuit the
        # network call entirely. Without this, a fast job cadence (e.g.
        # twin_compute_state every 5 min) re-hits a 15-min-TTL source every
        # single run regardless of the TTL, which is exactly what produced
        # real 429s from Open-Meteo while this module was being built. Only
        # a stale/missing cache (or cache_ttl_s=0, e.g. one-time elevation
        # seed calls) reaches the network at all.
        fresh_cached, fresh_age_s = self._read_cache(cache_key, max_age_s=self.cache_ttl_s)
        if self.cache_ttl_s > 0 and fresh_cached is not None:
            snapshot = self._write_snapshot_safe(
                db, city_id, "ok", 0, self.record_count(fresh_cached), None,
                fresh_cached, started_at, write_snapshot,
            )
            return fresh_cached, snapshot

        try:
            data = self._fetch_with_retry(timeout_s, kwargs)
            self._write_cache(cache_key, data)
        except Exception as exc:  # noqa: BLE001 - C1: never propagate
            error_message = "%s: %s" % (type(exc).__name__, exc)
            log.warning("%s ingest failed (%s); trying cache", self.source_key, error_message)
            stale_cached, _age_s = self._read_cache(cache_key, max_age_s=None)
            if stale_cached is not None:
                data = stale_cached
                status = "degraded"
            else:
                data = self.neutral_value(**kwargs)
                status = "failed"

        latency_ms = int((time.monotonic() - started) * 1000)
        snapshot = self._write_snapshot_safe(
            db, city_id, status, latency_ms, self.record_count(data), error_message,
            data, started_at, write_snapshot,
        )

        return data, snapshot

    def _write_snapshot_safe(self, db, city_id, status, latency_ms, records, error_message,
                              data, started_at, write_snapshot):
        """Write the TwinDataSnapshot audit row (C7) without letting an audit
        failure discard already-fetched, otherwise-good `data`, and without
        hiding the true status from the caller when persistence fails.

        Confirmed live while building this: a real, successful Overpass
        fetch (4,233 infrastructure records) was thrown away because the
        *snapshot insert* hit a transient SQLite lock -- the fetch worked,
        only the bookkeeping about the fetch failed, and the bookkeeping
        failure took the data down with it. Auditing must never be able to
        destroy good data; at worst it should mean the audit trail has a gap.

        Always returns a TwinDataSnapshot instance with the correct
        `.status` etc. so callers like `engine.py` that read
        `snapshot.status` never mistake "the audit row failed to persist"
        for "the fetch failed" -- it is simply not added to the session /
        committed when persistence itself failed.
        """
        snapshot = m.TwinDataSnapshot(
            source_key=self.source_key, city_id=city_id, status=status,
            latency_ms=latency_ms, records_ingested=records,
            error_message=error_message, payload_digest=_digest(data),
            started_at=started_at, finished_at=m.utcnow(),
        )
        if not write_snapshot:
            return snapshot
        try:
            db.session.add(snapshot)
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log.warning("%s: failed to persist TwinDataSnapshot audit row (%s); "
                       "the fetched data itself is still returned", self.source_key, exc)
        return snapshot

    def _fetch_with_retry(self, timeout_s, kwargs):
        last_exc = RuntimeError("%s: max_retries < 0" % self.source_key)
        for attempt in range(self.max_retries + 1):
            try:
                return self.fetch_raw(timeout_s=timeout_s, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_s * (attempt + 1))
        raise last_exc

    # -- disk TTL cache -------------------------------------------------------

    def _cache_key(self, city_id, kwargs):
        raw = json.dumps(
            {"source": self.source_key, "city_id": city_id, "kwargs": kwargs},
            sort_keys=True, default=str,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, cache_key):
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        return os.path.join(config.CACHE_DIR, "%s_%s.json" % (self.source_key, cache_key))

    def _write_cache(self, cache_key, data):
        path = self._cache_path(cache_key)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"cached_at": m.utcnow().isoformat(), "data": data}, fh)
        except OSError as exc:  # pragma: no cover - disk full etc.
            log.warning("failed to write twin cache %s: %s", path, exc)

    def _read_cache(self, cache_key, max_age_s=None):
        """Returns (data, age_seconds), or (None, None) on any miss.

        `max_age_s=None` means "read regardless of age, up to the generous
        8x-TTL ceiling below" -- the fallback-on-failure path. A numeric
        `max_age_s` (the fast-path freshness check in run()) additionally
        rejects anything older than that, even within the ceiling.
        """
        path = self._cache_path(cache_key)
        if not os.path.exists(path):
            return None, None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                envelope = json.load(fh)
        except (OSError, ValueError):
            return None, None

        cached_at = envelope.get("cached_at")
        age = None
        if cached_at:
            from datetime import datetime
            try:
                age = (m.utcnow() - datetime.fromisoformat(cached_at)).total_seconds()
            except ValueError:
                age = None

        if age is not None:
            ceiling = max(self.cache_ttl_s, 60) * 8
            if age > ceiling:
                # Even a stale-flagged fallback should not be ancient. The
                # ceiling floors at 60s so a cache_ttl_s=0 adapter (a
                # one-time seed call) still gets a sane "better than
                # nothing" window rather than rejecting everything.
                return None, None
            if max_age_s is not None and age > max_age_s:
                return None, None

        return envelope.get("data"), age


def _digest(data):
    if data is None:
        return None
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def request_json(session, method, url, timeout_s, **kwargs):
    """Shared HTTP helper: raises on non-2xx, returns parsed JSON."""
    response = session.request(method, url, timeout=timeout_s, **kwargs)
    response.raise_for_status()
    return response.json()
