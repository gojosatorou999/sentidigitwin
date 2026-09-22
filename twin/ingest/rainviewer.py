"""RainViewer: display-only radar overlay (see SYSTEMS.md section 2).

RainViewer exposes no point-value API, so it feeds no sub-score -- the
per-cell precipitation intensity idea in the original spec was cut. This
adapter caches the frame manifest only, so the frontend can build tile URLs
for layer 6 (`static/js/twin-layers.js`) and animate past/future frames.
"""

from .base import IngestAdapter, request_json

MANIFEST_URL = "https://api.rainviewer.com/public/weather-maps.json"


class RainViewerAdapter(IngestAdapter):
    """`fetch_raw()` returns the raw RainViewer manifest (frames + host)."""

    source_key = "rainviewer"
    cache_ttl_s = 10 * 60

    def fetch_raw(self, timeout_s=None, **_):
        timeout_s = timeout_s or self.timeout_s
        return request_json(self.session, "GET", MANIFEST_URL, timeout_s)

    def record_count(self, data):
        if not data:
            return 0
        past = (data.get("radar") or {}).get("past") or []
        forecast = (data.get("radar") or {}).get("nowcast") or []
        return len(past) + len(forecast)


def tile_url_template(manifest, frame_index=-1, color_scheme=4, options="1_1"):
    """A `{z}/{x}/{y}` tile URL template for one radar frame, or None.

    `frame_index` -1 is "most recent past frame". The frontend swaps this
    into a maplibre raster source; see section 8.4 layer 6.
    """
    if not manifest:
        return None
    host = manifest.get("host")
    frames = (manifest.get("radar") or {}).get("past") or []
    if not host or not frames:
        return None
    try:
        frame = frames[frame_index]
    except IndexError:
        return None
    path = frame.get("path")
    if not path:
        return None
    return "%s%s/256/{z}/{x}/{y}/%d/%s.png" % (host, path, color_scheme, options)
