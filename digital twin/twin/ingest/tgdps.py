"""TGDPS -- Hyderabad ground-station rainfall (section 4.2, Tier 2 optional).

The README says "reuse the existing proxy" -- Sentinel AI already has a TGDPS
integration elsewhere in the app; this module does not reimplement it. It
registers a callable the host provides, exactly like
``twin.ingest.internal_reports.register_report_model``, so the twin never
imports host-app-specific TGDPS client code (C6).

If the host never calls :func:`register_tgdps_client`, the adapter degrades
to "source absent" -- section 4.2's contract ("Open-Meteo only") -- rather
than raising.
"""

import logging

from .base import IngestAdapter

log = logging.getLogger("twin.ingest.tgdps")

_client = None


def register_tgdps_client(fetch_callable):
    """`fetch_callable(bbox) -> [{"lat","lon","rain_mm_1h","station_name"}, ...]`."""
    global _client
    _client = fetch_callable
    log.info("twin: TGDPS client registered")


def is_registered():
    return _client is not None


class TGDPSAdapter(IngestAdapter):
    """Ground-station rainfall for Hyderabad, if the host has wired a client."""

    source_key = "tgdps"
    cache_ttl_s = 15 * 60

    def fetch_raw(self, bbox, timeout_s=None, **_):
        if _client is None:
            raise RuntimeError("TGDPS client not registered; see register_tgdps_client()")
        return _client(bbox)

    def record_count(self, data):
        return len(data) if data else 0
