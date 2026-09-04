"""TomTom Traffic Flow -- optional, keyed (section 4.2).

Absent `TOMTOM_API_KEY`, this adapter is simply never scheduled (see
twin/jobs.py) and the traffic layer stays hidden client-side -- C5 requires
zero mandatory paid keys for the core loop, so "no key" must be a supported
steady state, not a degraded one.
"""

from .. import config
from .base import IngestAdapter, request_json

FLOW_TILE_URL = (
    "https://api.tomtom.com/traffic/map/4/tile/flow/relative/{z}/{x}/{y}.png"
)
FLOW_SEGMENT_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative/10/json"


def is_available():
    return bool(config.TOMTOM_API_KEY)


def tile_url_template():
    """A `{z}/{x}/{y}` tile URL template, or None if no key is configured."""
    if not is_available():
        return None
    return FLOW_TILE_URL + "?key=" + config.TOMTOM_API_KEY


class TomTomTrafficAdapter(IngestAdapter):
    """`fetch_raw(lat, lon)` -> `{"traffic_index": 0..100}` for one point.

    `traffic_index` is `100 * (1 - currentSpeed / freeFlowSpeed)`: 0 means
    free-flowing, 100 means gridlocked. Used only as an optional enhancement
    sub-signal (section 4.2) -- scoring.py must renormalise, not error, when
    this adapter is never run.
    """

    source_key = "tomtom_traffic"
    cache_ttl_s = 5 * 60

    def fetch_raw(self, lat, lon, timeout_s=None, **_):
        if not is_available():
            raise RuntimeError("TOMTOM_API_KEY not configured")
        timeout_s = timeout_s or self.timeout_s
        payload = request_json(
            self.session, "GET", FLOW_SEGMENT_URL, timeout_s,
            params={"point": "%f,%f" % (lat, lon), "key": config.TOMTOM_API_KEY},
        )
        segment = payload.get("flowSegmentData", {}) or {}
        current = segment.get("currentSpeed")
        free_flow = segment.get("freeFlowSpeed")
        if not current or not free_flow:
            return {"traffic_index": None}
        index = 100.0 * (1 - (current / free_flow))
        return {"traffic_index": max(0.0, min(100.0, index))}
