"""OSINT surveillance-camera discovery (open source, keyless).

The drill-down's ground-truth panel could already show an operator an
*archived* photo of a cell (KartaView/Mapillary) and, only with a keyed
provider, a live webcam. Neither answers the question an official actually
asks during an incident -- "is there a camera pointed at this junction, and
whose is it?".

This adapter answers that from OpenStreetMap alone, via Overpass. Every
surveillance installation mapped in OSM carries ``man_made=surveillance``
plus a well-defined tag vocabulary (``surveillance``, ``surveillance:type``,
``surveillance:zone``, ``camera:type``, ``camera:mount``,
``camera:direction``, ``operator``), which is exactly the OSINT an operator
needs: what kind of camera, who runs it, and which way it points. Traffic
enforcement cameras (``highway=speed_camera``, ``enforcement=*``) are
included because in Hyderabad and Bengaluru those are the densest and
best-attributed camera network in the data.

Three properties matter and are all deliberate:

* **Keyless.** Overpass needs no account, so this works on a fresh clone with
  an empty ``.env`` -- unlike Mapillary and Windy, which the imagery adapter
  can only use when a token happens to be configured.
* **Open licence.** Everything returned is ODbL OpenStreetMap data, attributed
  as such on the panel and on the map layer.
* **Tier 2.** It goes through :class:`~twin.ingest.base.IngestAdapter` like
  every other source, so an Overpass 429 costs one empty layer, never a 500.

A small number of OSM camera nodes additionally carry a publicly published
feed URL (``contact:webcam``, ``webcam``). Those are surfaced as
``stream_url`` so the panel can offer a link out. Nothing here ever probes,
embeds or proxies a camera feed: the panel renders a link the operator
chooses to follow, exactly like the existing Google Street View deep link.
"""

import logging
import math

from .base import IngestAdapter
from .overpass import run_overpass_query

log = logging.getLogger("twin.ingest.cctv")

#: Overpass clauses. Ways are included because large installations (a mall's
#: camera array, a toll-plaza gantry) are mapped as ways; ``out center`` gives
#: them a representative point, exactly like the infrastructure adapter.
CCTV_FILTERS = (
    'node["man_made"="surveillance"]',
    'way["man_made"="surveillance"]',
    'node["highway"="speed_camera"]',
    'node["enforcement"]',
)

#: Cameras are civic infrastructure -- an installation mapped today is still
#: there next week. A weekly TTL keeps a dashboard that is clicked all day
#: down to roughly one Overpass call per city per week.
CACHE_TTL_S = 7 * 24 * 60 * 60

#: Metres. The drill-down asks "cameras covering *this* cell", and an H3
#: res-8 cell is ~460 m across, so a 400 m radius is the honest match.
DEFAULT_RADIUS_M = 400

#: Hard ceiling on one response. Measured against live Overpass: Bengaluru
#: has ~2,960 mapped surveillance elements inside the city bbox and Hyderabad
#: ~47, so a 400-camera cap silently discarded six sevenths of Bengaluru's
#: coverage -- the city where the layer is actually worth having. A few
#: thousand points is nothing to a MapLibre circle layer; the cap exists only
#: to bound the payload if a bbox is ever widened.
MAX_CAMERAS = 5000

#: Free-text compass bearings OSM allows in ``camera:direction``/``direction``.
_COMPASS = {
    "n": 0.0, "north": 0.0, "nne": 22.5, "ne": 45.0, "ene": 67.5,
    "e": 90.0, "east": 90.0, "ese": 112.5, "se": 135.0, "sse": 157.5,
    "s": 180.0, "south": 180.0, "ssw": 202.5, "sw": 225.0, "wsw": 247.5,
    "w": 270.0, "west": 270.0, "wnw": 292.5, "nw": 315.0, "nnw": 337.5,
}

#: Kinds the map layer and the panel colour-code by.
KIND_ORDER = ("traffic", "public", "outdoor", "indoor", "private", "unknown")


def build_cctv_query(bbox=None, around=None, timeout_s=45):
    """Overpass QL for camera-like nodes, either in a bbox or around a point.

    ``bbox`` is (min_lon, min_lat, max_lon, max_lat); ``around`` is
    (lat, lon, radius_m). Exactly one must be given.
    """
    if (bbox is None) == (around is None):
        raise ValueError("build_cctv_query needs exactly one of bbox/around")

    if bbox is not None:
        scope = "%f,%f,%f,%f" % (bbox[1], bbox[0], bbox[3], bbox[2])
    else:
        lat, lon, radius_m = around
        scope = "around:%d,%f,%f" % (int(radius_m), lat, lon)

    body = "\n  ".join("%s(%s);" % (clause, scope) for clause in CCTV_FILTERS)
    return "[out:json][timeout:%d];\n(\n  %s\n);\nout center tags;" % (int(timeout_s), body)


class CctvOsintAdapter(IngestAdapter):
    """Surveillance cameras from OpenStreetMap, by bbox or around a point.

    ``fetch_raw`` returns a dict, never a bare list, so an empty result ("no
    cameras are mapped here") still carries its attribution and its query
    scope, and stays distinguishable from a failed lookup -- which ``run()``
    reports through the snapshot status instead.
    """

    source_key = "cctv_osint"
    cache_ttl_s = CACHE_TTL_S
    #: Overpass is slow and flaky by nature (see twin/ingest/overpass.py) and
    #: the retry loop already lives in run_overpass_query, so one attempt here.
    max_retries = 0

    def fetch_raw(self, bbox=None, lat=None, lon=None, radius_m=DEFAULT_RADIUS_M,
                  timeout_s=None, **_):
        timeout_s = timeout_s or max(self.timeout_s, 45)
        if bbox is not None:
            query = build_cctv_query(bbox=tuple(bbox), timeout_s=timeout_s - 5)
            scope = {"bbox": list(bbox)}
        elif lat is not None and lon is not None:
            query = build_cctv_query(around=(lat, lon, radius_m), timeout_s=timeout_s - 5)
            scope = {"lat": lat, "lon": lon, "radius_m": radius_m}
        else:
            raise ValueError("cctv lookup needs either bbox or lat/lon")

        payload = run_overpass_query(self.session, query, timeout_s)

        cameras = []
        for element in payload.get("elements", []):
            camera = _element_to_camera(element)
            if camera is None:
                continue
            if lat is not None and lon is not None:
                camera["distance_m"] = _haversine_m(lat, lon, camera["lat"], camera["lon"])
            cameras.append(camera)

        if lat is not None and lon is not None:
            cameras.sort(key=lambda c: c.get("distance_m")
                         if c.get("distance_m") is not None else 1e9)

        result = {
            "cameras": cameras[:MAX_CAMERAS],
            "counts_by_kind": _counts_by_kind(cameras),
            "source": "OpenStreetMap / Overpass",
            "attribution": "(c) OpenStreetMap contributors (ODbL)",
        }
        result.update(scope)
        return result

    def neutral_value(self, bbox=None, lat=None, lon=None, radius_m=DEFAULT_RADIUS_M, **_):
        """No network: an empty, honestly-labelled camera set, not ``None``.

        The map layer and the panel both render this with no special case;
        ``run()``'s snapshot status is what tells the health pill it was a
        failure rather than genuinely camera-free ground.
        """
        return {
            "cameras": [], "counts_by_kind": {},
            "source": "OpenStreetMap / Overpass",
            "attribution": "(c) OpenStreetMap contributors (ODbL)",
            "unavailable": True,
            "bbox": list(bbox) if bbox is not None else None,
            "lat": lat, "lon": lon, "radius_m": radius_m,
        }

    def record_count(self, data):
        return len((data or {}).get("cameras") or [])

    def _cache_key(self, city_id, kwargs):
        """Key on the query scope alone.

        The base implementation hashes every kwarg, one of which is the
        Flask-SQLAlchemy handle whose repr embeds a memory address -- so the
        key would change on every process restart and a weekly TTL would
        never survive one. Same reasoning as StreetViewAdapter._cache_key.
        """
        import hashlib

        bbox = kwargs.get("bbox")
        if bbox is not None:
            raw = "cctv:bbox:" + ",".join("%.4f" % float(v) for v in bbox)
        else:
            raw = "cctv:pt:%.4f,%.4f,%d" % (
                float(kwargs.get("lat") or 0.0), float(kwargs.get("lon") or 0.0),
                int(kwargs.get("radius_m") or DEFAULT_RADIUS_M))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Tag interpretation
# --------------------------------------------------------------------------

def _element_to_camera(element):
    tags = element.get("tags") or {}
    if not _is_camera(tags):
        return None

    lat = element.get("lat") or (element.get("center") or {}).get("lat")
    lon = element.get("lon") or (element.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None

    return {
        "osm_id": "%s/%s" % (element.get("type"), element.get("id")),
        "lat": lat,
        "lon": lon,
        "kind": _classify_kind(tags),
        "camera_type": tags.get("camera:type"),
        "mount": tags.get("camera:mount"),
        "direction": _parse_direction(
            tags.get("camera:direction") or tags.get("direction")),
        "zone": tags.get("surveillance:zone"),
        "operator": tags.get("operator") or tags.get("operator:type"),
        "name": tags.get("name") or tags.get("description"),
        "enforcement": tags.get("enforcement"),
        "stream_url": _public_feed_url(tags),
        "osm_url": "https://www.openstreetmap.org/%s/%s" % (
            element.get("type"), element.get("id")),
    }


def _is_camera(tags):
    """Exclude the non-camera things ``man_made=surveillance`` also covers.

    ``surveillance:type`` is an open vocabulary that includes ``guard`` (a
    human sentry post) and ``ALPR``. A guard post is not something an
    operator can look through, so it is dropped; ALPR is kept, because a
    number-plate reader is still a camera pointed at a road.
    """
    surveillance_type = (tags.get("surveillance:type") or "").strip().lower()
    if surveillance_type in ("guard", "sentry"):
        return False
    if tags.get("man_made") == "surveillance":
        return True
    if tags.get("highway") == "speed_camera":
        return True
    # A bare `enforcement=*` is a camera only when it is tagged on the device;
    # the same key appears on enforcement *relations*, which have no position.
    return bool(tags.get("enforcement")) and surveillance_type in ("", "camera", "alpr")


def _classify_kind(tags):
    zone = (tags.get("surveillance:zone") or "").strip().lower()
    if tags.get("highway") == "speed_camera" or tags.get("enforcement") \
            or zone in ("traffic", "parking", "toll"):
        return "traffic"

    surveillance = (tags.get("surveillance") or "").strip().lower()
    if surveillance in ("public", "outdoor", "indoor", "private"):
        return surveillance
    if zone in ("town", "street", "public"):
        return "public"
    return "unknown"


def _parse_direction(value):
    """Degrees clockwise from north, or ``None``.

    OSM allows a number ("135"), a compass point ("NW"), a range ("90-180",
    a panning camera's sweep) or a list ("N;E"). A range or list is reduced
    to its first element -- the layer draws one view cone, and the first
    bearing is the honest representative of where the camera starts looking.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    for separator in (";", "..", "-"):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
            break
    if text in _COMPASS:
        return _COMPASS[text]
    try:
        return float(text) % 360.0
    except ValueError:
        return None


def _public_feed_url(tags):
    """A published feed URL for this camera, if the mapper recorded one.

    Only tags whose *documented meaning* is a viewable feed are accepted.
    ``website``/``url`` on a camera node normally names the operator's
    corporate site rather than a stream, so they are deliberately not used.
    """
    for key in ("contact:webcam", "webcam", "camera:webcam"):
        value = tags.get(key)
        if value and str(value).lower().startswith(("http://", "https://")):
            return value
    return None


def _counts_by_kind(cameras):
    counts = {}
    for camera in cameras:
        counts[camera["kind"]] = counts.get(camera["kind"], 0) + 1
    return counts


def to_feature_collection(data):
    """GeoJSON for the map layer, straight from ``fetch_raw``'s dict."""
    features = []
    for camera in (data or {}).get("cameras") or []:
        # Null-valued tags are dropped rather than serialised. Most mapped
        # cameras carry only `man_made` and a position, so emitting the full
        # ten-key shape for every one of them roughly tripled a payload that
        # is already a few thousand features on Bengaluru -- and MapLibre
        # treats a missing property and a null one identically anyway.
        properties = {k: v for k, v in camera.items()
                      if k not in ("lat", "lon") and v is not None}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [camera["lon"], camera["lat"]]},
            "properties": properties,
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "attribution": (data or {}).get("attribution"),
        "counts_by_kind": (data or {}).get("counts_by_kind") or {},
    }


def _haversine_m(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2)
    return round(2 * radius * math.asin(min(1.0, math.sqrt(a))), 1)
